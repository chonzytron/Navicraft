"""
Shared helpers for playlist generation — used by both the /api/generate
HTTP endpoint and the Navidrome [navicraft] playlist watcher.

Consolidates logic that was previously duplicated:
  - popularity-mode detection (regex patterns)
  - library summary construction with short-TTL cache
  - progressive filter relaxation
  - duration window enforcement
"""

from __future__ import annotations

import logging
import math
import re
import sqlite3
import time
from typing import AsyncIterator, Optional

from config import config
import database as db
import ai_engine

logger = logging.getLogger("navicraft.generation")


# --- Popularity mode detection -------------------------------------------

# Phrases that indicate a popularity-driven ("best of" / "top hits") request.
# Used as a deterministic fallback when the AI doesn't set popularity_mode.
POPULARITY_PATTERNS = re.compile(
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


def detect_popularity_mode(prompt: str) -> bool:
    """Detect popularity-driven intent from the raw prompt."""
    return bool(POPULARITY_PATTERNS.search(prompt))


def apply_popularity_mode(filters: dict, prompt: str) -> bool:
    """Resolve popularity_mode from AI flag or regex fallback, strip
    mood/bpm filters when active, and return the final flag.

    Mutates `filters` in-place so downstream code sees a cleaned-up dict.
    """
    popularity_mode = bool(filters.get("popularity_mode")) or detect_popularity_mode(prompt)
    if popularity_mode:
        filters.pop("moods", None)
        filters.pop("bpm_min", None)
        filters.pop("bpm_max", None)
    return popularity_mode


# --- Library summary (with short-TTL cache) ------------------------------
#
# The summary rarely changes between back-to-back generate calls — genres,
# top artists and mood-tag counts only shift when scans / mood scans finish.
# Caching for a short window avoids re-running three aggregate queries per
# request, which noticeably speeds up the watcher (bursts of generations
# when the user creates several [navicraft] playlists in quick succession).

_LIBRARY_SUMMARY_TTL = 60.0  # seconds
_cached_summary: Optional[dict] = None
_cached_at: float = 0.0


def _fetch_library_summary(conn: sqlite3.Connection) -> dict:
    stats = db.get_library_stats(conn)
    return {
        "song_count": stats["song_count"],
        "artist_count": stats["artist_count"],
        "album_count": stats["album_count"],
        "genres": [g["genre"] for g in db.get_genres(conn)],
        "mood_tags": db.get_mood_tag_summary(conn),
        "theme_tags": db.get_theme_tag_summary(conn),
        # Pass 1 only uses the first 40 artist names; no point fetching more.
        "top_artists": db.get_top_artists(conn, limit=40),
        "year_range": db.get_year_range(conn),
    }


def get_library_summary(force_refresh: bool = False) -> dict:
    """Return a cached library summary; refresh if stale or forced."""
    global _cached_summary, _cached_at
    now = time.time()
    if not force_refresh and _cached_summary is not None and (now - _cached_at) < _LIBRARY_SUMMARY_TTL:
        return _cached_summary
    with db.get_db() as conn:
        summary = _fetch_library_summary(conn)
    _cached_summary = summary
    _cached_at = now
    return summary


def invalidate_library_summary():
    """Drop the cached summary (call after scans / mood scans that change stats)."""
    global _cached_summary, _cached_at
    _cached_summary = None
    _cached_at = 0.0


# --- Progressive filter relaxation ---------------------------------------


# Filter keys that actually produce SQL WHERE conditions. Used to compare steps
# so we never re-run a query identical to the previous step (the raw Pass 1 dict
# carries many null/empty keys that would defeat a naive equality check).
_EFFECTIVE_KEYS = (
    "genres", "year_min", "year_max", "artists", "moods", "bpm_min", "bpm_max",
    "keywords", "exclude_genres", "exclude_artists", "exclude_keywords",
)
_KEEP_NEGATIVE = ("exclude_genres", "exclude_artists", "exclude_keywords")


def _effective(filters: dict) -> dict:
    """Reduce a filter dict to only the keys that change the SQL result."""
    return {k: filters[k] for k in _EFFECTIVE_KEYS if filters.get(k)}


def relaxation_steps(filters: dict, popularity_mode: bool = False) -> list[tuple[str, dict]]:
    """Return the ordered (phase_name, filters) steps to attempt.

    Each step is skipped when its effective filter set matches the previous step,
    so identical queries are never re-run.

    Popularity mode ("best of" / "top hits"): the decade (year) or named artist IS
    the request, so we only drop incidental genre/keyword filters and never fall
    through to an unfiltered query when a defining constraint exists — returning
    fewer on-theme songs beats returning off-theme popular ones.

    Standard mode order:
      'initial'        — all filters.
      'relax_mood_bpm' — drop moods / bpm / keywords (data-dependent, most likely
                         to be empty, e.g. when mood scanning hasn't run).
      'relax_keep_era' — when a year range is set, keep the era + artists and drop
                         genre: staying in the requested decade is usually more
                         on-theme than the same genre across all eras.
      'relax_broad'    — genres + artists (+ negatives), drops year.
      'relax_all'      — no filters (last resort).
    """
    steps: list[tuple[str, dict]] = [("initial", filters)]

    def add(name: str, f: dict):
        if _effective(f) != _effective(steps[-1][1]):
            steps.append((name, f))

    if popularity_mode:
        # The genre / decade / artist IS the request, so keep all of them and only
        # drop incidental keyword filters (moods/bpm are already stripped upstream).
        # We never fall through to a fully unfiltered query when a real filter
        # exists — returning fewer on-theme hits beats off-theme popular ones. If
        # there's nothing to keep, `add`'s dedupe leaves just the (empty) initial
        # step, which is already the unfiltered query.
        keep_keys = ("genres", "year_min", "year_max", "artists", *_KEEP_NEGATIVE)
        add("relax_incidental", {k: filters[k] for k in keep_keys if filters.get(k)})
        return steps

    if any(filters.get(k) for k in ("moods", "bpm_min", "bpm_max", "keywords")):
        add("relax_mood_bpm", {
            k: v for k, v in filters.items()
            if k not in ("moods", "bpm_min", "bpm_max", "keywords")
        })

    if filters.get("year_min") or filters.get("year_max"):
        era_keys = ("year_min", "year_max", "artists", *_KEEP_NEGATIVE)
        add("relax_keep_era", {k: filters[k] for k in era_keys if filters.get(k)})

    if filters.get("genres") or filters.get("artists"):
        broad_keys = ("genres", "artists", *_KEEP_NEGATIVE)
        add("relax_broad", {k: filters[k] for k in broad_keys if filters.get(k)})

    add("relax_all", {})
    return steps


def broadening_message(phase: str, count: int) -> str:
    """Human-readable SSE message for a given relaxation phase."""
    if phase == "relax_mood_bpm":
        return f"Only {count} matches, relaxing mood/tempo filters..."
    if phase == "relax_keep_era":
        return f"Only {count} matches, keeping the era and broadening genre..."
    if phase in ("relax_broad", "relax_incidental"):
        return f"Only {count} matches, broadening search..."
    return f"Only {count} matches, dropping all filters..."


# --- Duration enforcement -----------------------------------------------


def enforce_duration(
    matched_songs: list[dict],
    candidates: list[dict],
    target_duration_min: int,
) -> tuple[list[dict], float]:
    """Trim or pad matched_songs to fit target_duration_min ± 5min.

    When over: drop songs from the end, stopping at the song whose inclusion
      brings the total closest to the target.
    When under: pad from unused candidates ordered by popularity desc.

    Returns (updated_songs, total_duration_seconds).
    """
    total_duration = sum(t.get("duration") or 0 for t in matched_songs)
    if not target_duration_min or not matched_songs:
        return matched_songs, total_duration

    target_secs = target_duration_min * 60
    tolerance_secs = 5 * 60
    max_secs = target_secs + tolerance_secs
    min_secs = target_secs - tolerance_secs

    if total_duration > max_secs:
        trimmed: list[dict] = []
        running = 0.0
        for t in matched_songs:
            dur = t.get("duration") or 0
            if running + dur > max_secs:
                # Include if it brings us closer to target than excluding.
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

    return matched_songs, total_duration


# --- Candidate limit -----------------------------------------------------


def candidate_limit_for(max_songs: int) -> int:
    """Scale candidate pool size to the requested playlist size (5x, floor 150)
    so small playlists don't blast Pass 2 with 500 candidates while large
    playlists still get diversity headroom."""
    return min(config.max_candidates, max(max_songs * 5, 150))


# Rough average song length (minutes) used to estimate how many songs a
# duration-targeted playlist needs. Deliberately a little low so we over- rather
# than under-provision the candidate pool.
_AVG_SONG_MIN = 3.5


def effective_song_count(max_songs: int, target_duration_min: Optional[int]) -> int:
    """Number of songs to size the candidate pool, Pass 2 target, and per-artist
    diversity cap around. When a duration is given it overrides the count, so a
    long playlist (e.g. 300 min) isn't starved by the default 25-song sizing."""
    if target_duration_min:
        est = math.ceil(target_duration_min / _AVG_SONG_MIN)
        return max(max_songs, est)
    return max_songs


# --- Full two-pass generation (shared by HTTP endpoint + watcher) --------


async def run_generation(
    prompt: str,
    max_songs: int,
    target_duration_min: Optional[int] = None,
    provider: Optional[str] = None,
) -> AsyncIterator[tuple[str, dict]]:
    """Run the complete two-pass generation pipeline.

    Async generator yielding ("progress", data) events as each phase completes,
    and finally ("result", data) with the selected songs. Both the /api/generate
    SSE endpoint and the Navidrome watcher drive this — the endpoint forwards the
    progress events as SSE; the watcher ignores them and keeps the result.

    Raises ValueError on unrecoverable problems (empty library, unparseable Pass 2).
    """
    yield "progress", {"phase": "pass1", "message": "Analyzing your prompt..."}

    library_summary = get_library_summary()
    if library_summary.get("song_count", 0) == 0:
        raise ValueError("Library index is empty. Run a scan first.")

    filters = await ai_engine.pass1_extract_intent(prompt, library_summary, provider)

    # Trust the AI's popularity_mode flag; fall back to regex on the raw prompt.
    popularity_mode = apply_popularity_mode(filters, prompt)
    if popularity_mode:
        logger.info("Popularity mode active — mood/bpm filters stripped")

    yield "progress", {
        "phase": "pass1_done",
        "message": "Intent extracted",
        "filters": {
            "genres": filters.get("genres") or [],
            "artists": filters.get("artists") or [],
            "moods": filters.get("moods") or [],
            "year_min": filters.get("year_min"),
            "year_max": filters.get("year_max"),
            "bpm_min": filters.get("bpm_min"),
            "bpm_max": filters.get("bpm_max"),
            "keywords": filters.get("keywords") or [],
            "exclude_genres": filters.get("exclude_genres") or [],
            "exclude_artists": filters.get("exclude_artists") or [],
            "exclude_keywords": filters.get("exclude_keywords") or [],
            "popularity_mode": popularity_mode,
        },
    }

    yield "progress", {"phase": "filtering", "message": "Searching library..."}

    eff_songs = effective_song_count(max_songs, target_duration_min)
    effective_limit = candidate_limit_for(eff_songs)

    candidates: list[dict] = []
    for phase_name, step_filters in relaxation_steps(filters, popularity_mode):
        if phase_name != "initial":
            yield "progress", {
                "phase": "broadening",
                "message": broadening_message(phase_name, len(candidates)),
            }
        with db.get_db() as conn:
            candidates = db.filter_tracks(
                conn, step_filters,
                limit=effective_limit,
                max_songs=eff_songs,
                popularity_order=popularity_mode,
            )
        if len(candidates) >= eff_songs:
            break

    logger.info("Sending %d candidates to Pass 2", len(candidates))

    # Candidate-pool preview: which artists/tracks are heading into Pass 2.
    seen_artists: set[str] = set()
    sample_artists: list[str] = []
    for c in candidates:
        a = (c.get("artist") or "").strip()
        if not a or a.lower() in seen_artists:
            continue
        seen_artists.add(a.lower())
        sample_artists.append(a)
        if len(sample_artists) >= 15:
            break
    unique_artist_total = len({
        (c.get("artist") or "").strip().lower()
        for c in candidates if c.get("artist")
    })
    sample_tracks = [
        {"title": c.get("title") or "", "artist": c.get("artist") or ""}
        for c in candidates[:6]
    ]
    yield "progress", {
        "phase": "filtering_done",
        "message": f"Found {len(candidates)} candidates",
        "candidates_found": len(candidates),
        "unique_artists": unique_artist_total,
        "sample_artists": sample_artists,
        "sample_tracks": sample_tracks,
    }

    yield "progress", {"phase": "pass2", "message": f"Selecting from {len(candidates)} candidates..."}

    ai_result = await ai_engine.pass2_select_songs(
        prompt=prompt,
        candidates=candidates,
        max_songs=eff_songs,
        provider=provider,
        target_duration_min=target_duration_min,
        filters=filters,
    )

    song_ids = ai_engine.extract_song_ids(ai_result)
    yield "progress", {
        "phase": "pass2_done",
        "message": f"AI selected {len(song_ids)} songs",
        "selected_count": len(song_ids),
        "playlist_name": ai_result.get("name") or "",
    }

    candidate_map = {c["id"]: c for c in candidates}
    matched_songs = [candidate_map[sid] for sid in song_ids if sid in candidate_map]

    matched_songs, total_duration = enforce_duration(
        matched_songs, candidates, target_duration_min
    )

    yield "result", {
        "name": ai_result.get("name") or "AI Playlist",
        "description": ai_result.get("description", ""),
        "songs": matched_songs,
        "total_duration": round(total_duration),
        "total_suggested": len(song_ids),
        "candidates_found": len(candidates),
        "filters": filters,
        "popularity_mode": popularity_mode,
    }
