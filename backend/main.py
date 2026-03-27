"""
NaviCraft — AI-powered playlist generator for Navidrome.
Main FastAPI application.
"""

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse, Response
from pydantic import BaseModel, Field
from typing import Optional

from config import config
import database as db
import scanner
import navidrome
import ai_engine
import scheduler as sched
import popularity

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("navicraft")

# Track ongoing scan
_scan_lock = asyncio.Lock()
_scan_progress = {"phase": "idle", "current": 0, "total": 0, "message": ""}

# Rate limiting for /api/generate
_last_generate_time = 0.0
_GENERATE_COOLDOWN = 10  # seconds


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("NaviCraft starting up — AI: %s, Music: %s", config.ai_provider, config.music_dir)

    # Init database
    db.init_db()

    # Start background scheduler
    sched.start_scheduler()

    # Run initial incremental scan in background
    asyncio.create_task(_initial_scan())

    yield

    sched.stop_scheduler()
    logger.info("NaviCraft shutdown.")


async def _initial_scan():
    """Run an incremental scan on startup."""
    await asyncio.sleep(2)  # Let server start first
    logger.info("Running startup scan...")
    try:
        async with _scan_lock:
            def progress(phase, current, total, msg):
                _scan_progress.update(phase=phase, current=current, total=total, message=msg)

            await scanner.scan_library(full_scan=False, progress_cb=progress)

        # Sync Navidrome IDs
        try:
            await navidrome.sync_navidrome_ids()
        except Exception:
            logger.warning("Navidrome ID sync failed (Navidrome may not be available yet)")

        _scan_progress.update(phase="idle", message="Ready")

        # Enrich new tracks with popularity data
        try:
            await popularity.enrich_popularity(batch_size=500)
        except Exception:
            logger.warning("Startup popularity enrichment failed")
    except Exception:
        logger.exception("Startup scan failed")
        _scan_progress.update(phase="error", message="Startup scan failed")


app = FastAPI(title="NaviCraft", version="2.0.0", lifespan=lifespan)


# =========================================================================
# Models
# =========================================================================

class GenerateRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=2000)
    max_songs: int = Field(default=25, ge=5, le=100)
    target_duration_min: Optional[int] = Field(default=None, ge=5, le=600, description="Target duration in minutes")
    auto_create: bool = Field(default=False)
    provider: Optional[str] = Field(default=None, description="AI provider override: 'claude' or 'gemini'")


class CreatePlaylistRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    song_ids: list[str] = Field(..., min_length=1, description="Navidrome song IDs")


# =========================================================================
# Health & Status
# =========================================================================

@app.get("/api/health")
async def health():
    return {"status": "ok", "ai_provider": config.ai_provider}


@app.get("/api/ai/providers")
async def ai_providers():
    """Return which AI providers are configured and the active default."""
    available = []
    if config.claude_api_key:
        available.append({"id": "claude", "name": "Claude", "model": config.claude_model})
    if config.gemini_api_key:
        available.append({"id": "gemini", "name": "Gemini", "model": config.gemini_model})
    return {"available": available, "default": config.ai_provider}


@app.get("/api/navidrome/test")
async def test_navidrome():
    try:
        return await navidrome.test_connection()
    except Exception as e:
        raise HTTPException(502, detail=f"Cannot connect to Navidrome: {e}")


# =========================================================================
# Library & Index
# =========================================================================

@app.get("/api/library/stats")
async def library_stats():
    """Get library stats from the local index."""
    with db.get_db() as conn:
        stats = db.get_library_stats(conn)
        genres = db.get_genres(conn)
        moods = db.get_moods(conn)
        year_range = db.get_year_range(conn)
        last_scan = db.get_last_scan(conn)

    return {
        **stats,
        "genres": genres,
        "moods": moods,
        "year_range": year_range,
        "last_scan": last_scan,
    }


@app.get("/api/library/genres")
async def library_genres():
    with db.get_db() as conn:
        return db.get_genres(conn)


@app.get("/api/library/search")
async def library_search(q: str = Query(..., min_length=1)):
    with db.get_db() as conn:
        return db.search_tracks(conn, q)


# =========================================================================
# Popularity
# =========================================================================

_enrichment_running = False


@app.post("/api/popularity/enrich")
async def trigger_enrichment():
    """Manually trigger a batch of popularity enrichment."""
    global _enrichment_running
    if _enrichment_running:
        return {"status": "already_running", "message": "Enrichment is already in progress"}

    with db.get_db() as conn:
        remaining = db.count_tracks_without_popularity(conn)
    if remaining == 0:
        return {"status": "complete", "message": "All tracks already enriched"}

    async def run():
        global _enrichment_running
        _enrichment_running = True
        try:
            await popularity.enrich_popularity(batch_size=500)
        finally:
            _enrichment_running = False

    asyncio.create_task(run())
    return {"status": "started", "remaining": remaining}


@app.post("/api/popularity/re-enrich")
async def re_enrich_popularity():
    """Reset all popularity scores and trigger re-enrichment from scratch."""
    with db.get_db() as conn:
        count = db.reset_popularity(conn)
    # The scheduler will pick up the un-enriched tracks automatically
    return {"status": "reset", "tracks_to_enrich": count, "message": "Scores reset. Background enrichment will re-process all tracks."}


@app.get("/api/popularity/status")
async def popularity_status():
    """Check how many tracks still need popularity enrichment."""
    with db.get_db() as conn:
        remaining = db.count_tracks_without_popularity(conn)
        total = db.execute_count(conn, "SELECT COUNT(*) as cnt FROM tracks WHERE title IS NOT NULL")
    enriched = total - remaining
    return {
        "total": total,
        "enriched": enriched,
        "remaining": remaining,
        "percent": round(enriched / total * 100, 1) if total > 0 else 0,
        "running": _enrichment_running,
    }


# =========================================================================
# Scanning
# =========================================================================

@app.post("/api/scan")
async def trigger_scan(full: bool = False):
    """Trigger a library scan. Returns immediately; poll /api/scan/status for progress."""
    if _scan_lock.locked():
        raise HTTPException(409, detail="Scan already in progress")

    async def run_scan():
        async with _scan_lock:
            def progress(phase, current, total, msg):
                _scan_progress.update(phase=phase, current=current, total=total, message=msg)

            stats = await scanner.scan_library(full_scan=full, progress_cb=progress)

            # Sync Navidrome IDs after scan
            try:
                _scan_progress.update(phase="syncing", message="Syncing Navidrome IDs...")
                await navidrome.sync_navidrome_ids()
            except Exception:
                logger.warning("Navidrome ID sync failed after scan")

            # Enrich new tracks with popularity data
            try:
                _scan_progress.update(phase="enriching", message="Fetching popularity data...")
                await popularity.enrich_popularity(batch_size=200)
            except Exception:
                logger.warning("Popularity enrichment failed after scan")

            _scan_progress.update(phase="idle", message="Ready")
            return stats

    asyncio.create_task(run_scan())
    return {"status": "started", "full": full}


@app.get("/api/scan/status")
async def scan_status():
    return {**_scan_progress, "scanning": _scan_lock.locked()}


# =========================================================================
# Playlist Generation (Two-Pass AI)
# =========================================================================

@app.post("/api/generate")
async def generate_playlist(req: GenerateRequest):
    """Generate a playlist using the two-pass AI strategy. Streams SSE progress events."""
    global _last_generate_time
    now = time.time()
    if now - _last_generate_time < _GENERATE_COOLDOWN:
        remaining = int(_GENERATE_COOLDOWN - (now - _last_generate_time))
        raise HTTPException(429, detail=f"Please wait {remaining}s before generating again")
    _last_generate_time = now

    # Check we have indexed songs
    with db.get_db() as conn:
        stats = db.get_library_stats(conn)
    if stats["song_count"] == 0:
        raise HTTPException(404, detail="Library index is empty. Run a scan first.")

    async def event_stream():
        def sse(event: str, data: dict) -> str:
            return f"event: {event}\ndata: {json.dumps(data)}\n\n"

        try:
            # --- Pass 1 ---
            yield sse("progress", {"phase": "pass1", "message": "Analyzing your prompt..."})

            with db.get_db() as conn:
                library_summary = {
                    "song_count": stats["song_count"],
                    "artist_count": stats["artist_count"],
                    "album_count": stats["album_count"],
                    "genres": [g["genre"] for g in db.get_genres(conn)],
                    "moods": db.get_moods(conn),
                    "top_artists": db.get_top_artists(conn, limit=150),
                    "year_range": db.get_year_range(conn),
                }

            filters = await ai_engine.pass1_extract_intent(req.prompt, library_summary, req.provider)
            yield sse("progress", {"phase": "filtering", "message": "Searching library..."})

            # --- Filter candidates ---
            with db.get_db() as conn:
                candidates = db.filter_tracks(conn, filters, limit=config.max_candidates)

            if len(candidates) < 30:
                yield sse("progress", {"phase": "broadening", "message": f"Only {len(candidates)} matches, broadening search..."})
                broad_filters = {"genres": filters.get("genres", [])}
                with db.get_db() as conn:
                    candidates = db.filter_tracks(conn, broad_filters, limit=config.max_candidates)

            if len(candidates) < 20:
                with db.get_db() as conn:
                    candidates = db.filter_tracks(conn, {}, limit=config.max_candidates)

            logger.info("Sending %d candidates to Pass 2", len(candidates))

            # --- Pass 2 ---
            yield sse("progress", {"phase": "pass2", "message": f"Selecting from {len(candidates)} candidates..."})

            ai_result = await ai_engine.pass2_select_songs(
                prompt=req.prompt,
                candidates=candidates,
                max_songs=req.max_songs,
                target_duration_min=req.target_duration_min,
                provider=req.provider,
            )

            yield sse("progress", {"phase": "matching", "message": "Building playlist..."})

            # --- Match selections ---
            candidate_map = {c["id"]: c for c in candidates}
            matched_songs = []
            for s in ai_result.get("songs", []):
                track = candidate_map.get(s["id"])
                if track:
                    matched_songs.append(track)

            total_duration = sum(t.get("duration") or 0 for t in matched_songs)

            result = {
                "name": ai_result.get("name", "AI Playlist"),
                "description": ai_result.get("description", ""),
                "songs": matched_songs,
                "total_matched": len(matched_songs),
                "total_suggested": len(ai_result.get("songs", [])),
                "total_duration": round(total_duration),
                "filters_used": filters,
                "candidates_found": len(candidates),
                "created": False,
                "navidrome_id": None,
            }

            # --- Auto-create ---
            navidrome_pl_id = None
            if req.auto_create and matched_songs:
                nd_ids = [t["navidrome_id"] for t in matched_songs if t.get("navidrome_id")]
                if nd_ids:
                    try:
                        yield sse("progress", {"phase": "saving", "message": "Saving to Navidrome..."})
                        pl = await navidrome.create_playlist(result["name"], nd_ids)
                        result["created"] = True
                        result["navidrome_id"] = pl["id"]
                        navidrome_pl_id = pl["id"]
                    except Exception as e:
                        logger.warning("Auto-create failed: %s", e)
                        result["auto_create_error"] = str(e)
                else:
                    result["auto_create_error"] = "No Navidrome IDs matched. Run a Navidrome sync."

            yield sse("result", result)

        except ValueError as e:
            yield sse("error", {"detail": str(e)})
        except Exception:
            logger.exception("Playlist generation failed")
            yield sse("error", {"detail": "Playlist generation failed. Check logs."})

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# =========================================================================
# Playlist Management
# =========================================================================

@app.post("/api/playlists")
async def create_playlist(req: CreatePlaylistRequest):
    try:
        pl = await navidrome.create_playlist(req.name, req.song_ids)
        return pl
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@app.get("/api/playlists")
async def list_playlists():
    try:
        return await navidrome.get_playlists()
    except Exception as e:
        raise HTTPException(502, detail=str(e))


@app.delete("/api/playlists/{playlist_id}")
async def delete_playlist(playlist_id: str):
    """Delete a playlist from Navidrome."""
    try:
        await navidrome.delete_playlist(playlist_id)
        return {"status": "deleted"}
    except Exception as e:
        raise HTTPException(500, detail=str(e))


# =========================================================================
# M3U Export
# =========================================================================

class ExportM3URequest(BaseModel):
    name: str = Field(..., min_length=1)
    songs: list[dict] = Field(..., min_length=1)


@app.post("/api/export/m3u")
async def export_m3u(req: ExportM3URequest):
    """Generate a .m3u8 playlist file for download."""
    lines = ["#EXTM3U", f"#PLAYLIST:{req.name}"]
    for s in req.songs:
        duration = int(s.get("duration") or 0)
        artist = s.get("artist", "Unknown")
        title = s.get("title", "Unknown")
        file_path = s.get("file_path", "")
        lines.append(f"#EXTINF:{duration},{artist} - {title}")
        lines.append(file_path)

    content = "\n".join(lines) + "\n"
    safe_name = "".join(c for c in req.name if c.isalnum() or c in " -_").strip() or "playlist"

    return Response(
        content=content,
        media_type="audio/x-mpegurl",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.m3u"'},
    )


# =========================================================================
# Serve Frontend
# =========================================================================

app.mount("/assets", StaticFiles(directory="/app/frontend/assets", check_dir=False), name="assets")


@app.get("/{full_path:path}")
async def serve_frontend(full_path: str):
    return FileResponse("/app/frontend/index.html")
