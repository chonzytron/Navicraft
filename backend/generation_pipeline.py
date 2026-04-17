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
import re
import sqlite3
import time
from typing import Optional

from config import config
import database as db

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


def relaxation_steps(filters: dict) -> list[tuple[str, dict]]:
    """Return the ordered (phase_name, filters) steps to attempt.

    Step 'initial': apply all filters.
    Step 'relax_mood_bpm': drop moods / bpm / keywords (data-dependent filters
      most likely to cause emptiness, e.g. when mood scanning hasn't run).
    Step 'relax_broad': keep only genres + artists + negative filters (drop year range).
    Step 'relax_all': no filters at all (last-resort fallback).
    """
    steps: list[tuple[str, dict]] = [("initial", filters)]

    if any(filters.get(k) for k in ("moods", "bpm_min", "bpm_max", "keywords")):
        relaxed = {k: v for k, v in filters.items() if k not in ("moods", "bpm_min", "bpm_max", "keywords")}
        steps.append(("relax_mood_bpm", relaxed))

    broad_keys = ("genres", "artists", "exclude_genres", "exclude_artists", "exclude_keywords")
    broad_filters = {k: filters[k] for k in broad_keys if filters.get(k)}
    # Only add broad step if it's different from the previous step
    if not steps or steps[-1][1] != broad_filters:
        steps.append(("relax_broad", broad_filters))

    steps.append(("relax_all", {}))
    return steps


def broadening_message(phase: str, count: int) -> str:
    """Human-readable SSE message for a given relaxation phase."""
    if phase == "relax_mood_bpm":
        return f"Only {count} matches, relaxing mood/tempo filters..."
    if phase == "relax_broad":
        return f"Only {count} matches, broadening search..."
    return f"Only {count} matches, dropping all filters..."


async def filter_with_relaxation(
    filters: dict,
    max_songs: int,
    effective_limit: int,
    popularity_mode: bool,
) -> list[dict]:
    """Run filter_tracks with progressive relaxation passes.
    Used by the playlist watcher (no progress events needed).
    For SSE progress streaming, iterate relaxation_steps() directly.
    """
    candidates: list[dict] = []
    for _phase, step_filters in relaxation_steps(filters):
        with db.get_db() as conn:
            candidates = db.filter_tracks(
                conn, step_filters,
                limit=effective_limit,
                max_songs=max_songs,
                popularity_order=popularity_mode,
            )
        if len(candidates) >= max_songs:
            break
    return candidates


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
