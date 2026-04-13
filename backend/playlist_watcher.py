"""
Playlist watcher for Navidrome integration.

Polls Navidrome for playlists matching the [navicraft, ...] pattern,
generates AI playlists from the prompt, and populates them in-place.

Format:  "any prompt text [navicraft]"
         "any prompt text [navicraft, songs: 30]"
         "any prompt text [navicraft, duration: 90]"
         "any prompt text [navicraft, duration: 90, songs: 40]"

The text before [navicraft ...] becomes the AI prompt.
After generation the playlist is renamed to the AI-chosen name
and populated with the matched songs.
"""

import asyncio
import logging
import re
import time
from config import config
import database as db
import navidrome
import ai_engine

logger = logging.getLogger("navicraft.watcher")

# Regex: capture everything before [navicraft ...], then the bracket contents
_NAVICRAFT_RE = re.compile(
    r"^(.*?)\s*\[navicraft(?:\s*,\s*(.*?))?\]\s*$",
    re.IGNORECASE,
)

# Parameter extractors inside the bracket
_DURATION_RE = re.compile(r"duration\s*:\s*(\d+)", re.IGNORECASE)
_SONGS_RE = re.compile(r"songs\s*:\s*(\d+)", re.IGNORECASE)

# Track playlists currently being processed to avoid double-triggers
_in_progress: set[str] = set()

# Track playlists we've already processed (by ID) to avoid re-processing
# after rename (in case getPlaylists returns stale data)
_processed: set[str] = set()

# Last watcher run status for the status endpoint
_watcher_status = {
    "last_check": None,
    "last_generated": None,
    "playlists_found": 0,
    "total_generated": 0,
    "last_error": None,
}


def get_watcher_status() -> dict:
    """Return current watcher status for the API."""
    return {
        **_watcher_status,
        "enabled": config.navicraft_watcher_enabled,
        "interval_seconds": config.navicraft_watcher_interval,
        "in_progress": len(_in_progress),
    }


def parse_navicraft_tag(playlist_name: str) -> dict | None:
    """Parse a playlist name for the [navicraft, ...] tag.

    Returns dict with keys: prompt, max_songs, target_duration_min
    or None if the name doesn't match.
    """
    match = _NAVICRAFT_RE.match(playlist_name)
    if not match:
        return None

    prompt = match.group(1).strip()
    if not prompt:
        return None

    params_str = match.group(2) or ""

    max_songs = 25  # default
    target_duration_min = None

    dur_match = _DURATION_RE.search(params_str)
    if dur_match:
        target_duration_min = max(5, min(600, int(dur_match.group(1))))

    songs_match = _SONGS_RE.search(params_str)
    if songs_match:
        max_songs = max(5, min(100, int(songs_match.group(1))))

    return {
        "prompt": prompt,
        "max_songs": max_songs,
        "target_duration_min": target_duration_min,
    }


# Patterns for detecting popularity-driven requests (mirrors main.py)
_POPULARITY_PATTERNS = re.compile(
    r"\b(?:"
    r"best\s+of"
    r"|top\s+hits"
    r"|greatest\s+hits"
    r"|biggest\s+hits"
    r"|most\s+popular"
    r"|top\s+\d+\s+(?:songs?|tracks?|hits?)"
    r"|best\s+(?:songs?|tracks?)"
    r")\b",
    re.IGNORECASE,
)


async def _generate_for_playlist(playlist_id: str | None, parsed: dict, save: bool = True):
    """Run the two-pass AI generation pipeline and populate the playlist.

    When save=True and playlist_id is set, updates the Navidrome playlist.
    When save=False, returns the result without touching Navidrome.
    """
    prompt = parsed["prompt"]
    max_songs = parsed["max_songs"]
    target_duration_min = parsed["target_duration_min"]

    logger.info("Generating playlist for prompt: '%s' (songs=%d, duration=%s)",
                prompt, max_songs, target_duration_min)

    # --- Pass 1: Extract intent ---
    with db.get_db() as conn:
        stats = db.get_library_stats(conn)
        library_summary = {
            "song_count": stats["song_count"],
            "artist_count": stats["artist_count"],
            "album_count": stats["album_count"],
            "genres": [g["genre"] for g in db.get_genres(conn)],
            "mood_tags": db.get_mood_tag_summary(conn),
            "theme_tags": db.get_theme_tag_summary(conn),
            "top_artists": db.get_top_artists(conn, limit=150),
            "year_range": db.get_year_range(conn),
        }

    if stats["song_count"] == 0:
        raise ValueError("Library index is empty. Run a scan first.")

    filters = await ai_engine.pass1_extract_intent(prompt, library_summary)

    # Detect popularity mode
    popularity_mode = bool(filters.get("popularity_mode")) or bool(_POPULARITY_PATTERNS.search(prompt))
    if popularity_mode:
        filters.pop("moods", None)
        filters.pop("bpm_min", None)
        filters.pop("bpm_max", None)

    # --- Filter candidates ---
    effective_limit = min(config.max_candidates, max(max_songs * 5, 150))

    with db.get_db() as conn:
        candidates = db.filter_tracks(conn, filters, limit=effective_limit,
                                       max_songs=max_songs, popularity_order=popularity_mode)

    min_needed = max_songs

    # Progressive relaxation (same logic as main.py)
    if len(candidates) < min_needed and any(filters.get(k) for k in ("moods", "bpm_min", "bpm_max", "keywords")):
        relaxed = {k: v for k, v in filters.items() if k not in ("moods", "bpm_min", "bpm_max", "keywords")}
        with db.get_db() as conn:
            candidates = db.filter_tracks(conn, relaxed, limit=effective_limit,
                                           max_songs=max_songs, popularity_order=popularity_mode)

    if len(candidates) < min_needed:
        broad_keys = ("genres", "artists", "exclude_genres", "exclude_artists", "exclude_keywords")
        broad_filters = {k: filters[k] for k in broad_keys if filters.get(k)}
        with db.get_db() as conn:
            candidates = db.filter_tracks(conn, broad_filters, limit=effective_limit,
                                           max_songs=max_songs, popularity_order=popularity_mode)

    if len(candidates) < min_needed:
        with db.get_db() as conn:
            candidates = db.filter_tracks(conn, {}, limit=effective_limit,
                                           max_songs=max_songs, popularity_order=popularity_mode)

    logger.info("Watcher: %d candidates for prompt '%s'", len(candidates), prompt[:60])

    # --- Pass 2: Select songs ---
    ai_result = await ai_engine.pass2_select_songs(
        prompt=prompt,
        candidates=candidates,
        max_songs=max_songs,
        target_duration_min=target_duration_min,
        filters=filters,
    )

    # --- Match selections to Navidrome IDs ---
    candidate_map = {c["id"]: c for c in candidates}
    song_ids = ai_result.get("song_ids") or [s.get("id") for s in ai_result.get("songs", [])]

    matched_songs = []
    for raw_id in song_ids:
        try:
            sid = int(raw_id)
        except (TypeError, ValueError):
            continue
        track = candidate_map.get(sid)
        if track:
            matched_songs.append(track)

    # Duration enforcement
    total_duration = sum(t.get("duration") or 0 for t in matched_songs)
    if target_duration_min and matched_songs:
        target_secs = target_duration_min * 60
        tolerance_secs = 5 * 60
        max_secs = target_secs + tolerance_secs
        min_secs = target_secs - tolerance_secs

        if total_duration > max_secs:
            trimmed = []
            running = 0.0
            for t in matched_songs:
                dur = t.get("duration") or 0
                if running + dur > max_secs:
                    if abs(running - target_secs) > abs(running + dur - target_secs):
                        trimmed.append(t)
                        running += dur
                    break
                trimmed.append(t)
                running += dur
                if running >= min_secs:
                    break
            matched_songs = trimmed
            total_duration = sum(t.get("duration") or 0 for t in matched_songs)
        elif total_duration < min_secs:
            used_ids = {t["id"] for t in matched_songs}
            remaining = [c for c in candidates if c["id"] not in used_ids]
            remaining.sort(key=lambda c: c.get("popularity") or 0, reverse=True)
            for c in remaining:
                dur = c.get("duration") or 0
                if total_duration + dur > max_secs:
                    continue
                matched_songs.append(c)
                total_duration += dur
                if total_duration >= min_secs:
                    break

    # Get Navidrome IDs for the matched songs
    nd_ids = [t["navidrome_id"] for t in matched_songs if t.get("navidrome_id")]
    if not nd_ids:
        raise ValueError("No songs could be matched to Navidrome IDs. Run a library scan to sync.")

    playlist_name = ai_result.get("name") or prompt[:80]

    if save and playlist_id:
        # Update the playlist: rename and add songs
        await navidrome.update_playlist(playlist_id, name=playlist_name, song_ids_to_add=nd_ids)
        logger.info("Watcher: populated playlist '%s' with %d songs (total duration: %ds)",
                    playlist_name, len(nd_ids), int(total_duration))

    return {
        "name": playlist_name,
        "description": ai_result.get("description", ""),
        "navidrome_song_ids": nd_ids,
        "songs": [
            {
                "title": t.get("title", ""),
                "artist": t.get("artist", ""),
                "album": t.get("album", ""),
                "duration": t.get("duration"),
                "navidrome_id": t.get("navidrome_id", ""),
            }
            for t in matched_songs if t.get("navidrome_id")
        ],
        "total_songs": len(nd_ids),
        "total_duration": int(total_duration),
    }


async def check_navidrome_playlists():
    """Poll Navidrome for playlists matching the [navicraft, ...] pattern.

    Called periodically by the scheduler.
    """
    if not config.navicraft_watcher_enabled:
        return

    if not config.navidrome_url or not config.navidrome_password:
        return

    _watcher_status["last_check"] = time.time()

    try:
        playlists = await navidrome.get_playlists()
    except Exception as e:
        logger.warning("Watcher: failed to fetch playlists: %s", e)
        _watcher_status["last_error"] = str(e)
        return

    found = 0
    for pl in playlists:
        pl_id = pl["id"]
        pl_name = pl.get("name", "")
        song_count = pl.get("songCount", 0)

        # Skip already processed or in-progress playlists
        if pl_id in _processed or pl_id in _in_progress:
            continue

        # Only process empty playlists with the [navicraft] tag
        if song_count > 0:
            continue

        parsed = parse_navicraft_tag(pl_name)
        if not parsed:
            continue

        found += 1
        _in_progress.add(pl_id)

        try:
            result = await _generate_for_playlist(pl_id, parsed, save=True)
            _processed.add(pl_id)
            _watcher_status["last_generated"] = {
                "playlist_id": pl_id,
                "name": result["name"],
                "songs": result["total_songs"],
                "duration": result["total_duration"],
                "prompt": parsed["prompt"],
                "time": time.time(),
            }
            _watcher_status["total_generated"] = _watcher_status.get("total_generated", 0) + 1
            _watcher_status["last_error"] = None
        except Exception as e:
            logger.exception("Watcher: failed to generate playlist for '%s'", pl_name)
            _watcher_status["last_error"] = f"Failed for '{parsed['prompt'][:50]}': {e}"
            # Mark as processed to avoid retrying the same broken prompt endlessly
            _processed.add(pl_id)
        finally:
            _in_progress.discard(pl_id)

    _watcher_status["playlists_found"] = found

    # Prune the processed set to avoid unbounded growth (keep last 500)
    if len(_processed) > 500:
        _processed.clear()
