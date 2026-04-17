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

import collections
import logging
import re
import time
from config import config
import navidrome
import ai_engine
import generation_pipeline as gen

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
# after rename (in case getPlaylists returns stale data).
# Uses OrderedDict as an ordered set so we can evict oldest entries.
_processed: collections.OrderedDict[str, float] = collections.OrderedDict()
_PROCESSED_MAX = 500

# Guard against overlapping watcher invocations
_watcher_running = False

# Rate limiting for watcher-triggered AI calls (seconds between generations)
_last_generate_time = 0.0
_GENERATE_COOLDOWN = 10

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


async def generate_playlist(
    playlist_id: str | None,
    parsed: dict,
    save: bool = True,
    provider: str | None = None,
):
    """Run the two-pass AI generation pipeline and optionally populate a playlist.

    When save=True and playlist_id is set, updates the Navidrome playlist.
    When save=False, returns the result without touching Navidrome.
    provider overrides the default AI provider when set.
    """
    prompt = parsed["prompt"]
    max_songs = parsed["max_songs"]
    target_duration_min = parsed["target_duration_min"]

    logger.info("Generating playlist for prompt: '%s' (songs=%d, duration=%s, provider=%s)",
                prompt, max_songs, target_duration_min, provider or config.ai_provider)

    # --- Pass 1: Extract intent ---
    library_summary = gen.get_library_summary()

    if library_summary.get("song_count", 0) == 0:
        raise ValueError("Library index is empty. Run a scan first.")

    filters = await ai_engine.pass1_extract_intent(prompt, library_summary, provider)
    popularity_mode = gen.apply_popularity_mode(filters, prompt)

    # --- Filter candidates (with progressive relaxation) ---
    effective_limit = gen.candidate_limit_for(max_songs)
    candidates = await gen.filter_with_relaxation(
        filters=filters,
        max_songs=max_songs,
        effective_limit=effective_limit,
        popularity_mode=popularity_mode,
    )

    logger.info("Watcher: %d candidates for prompt '%s'", len(candidates), prompt[:60])

    # --- Pass 2: Select songs ---
    ai_result = await ai_engine.pass2_select_songs(
        prompt=prompt,
        candidates=candidates,
        max_songs=max_songs,
        provider=provider,
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

    matched_songs, total_duration = gen.enforce_duration(
        matched_songs, candidates, target_duration_min
    )

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


def _mark_processed(playlist_id: str):
    """Add a playlist ID to the processed set, evicting oldest if at capacity."""
    _processed[playlist_id] = time.time()
    while len(_processed) > _PROCESSED_MAX:
        _processed.popitem(last=False)


async def check_navidrome_playlists():
    """Poll Navidrome for playlists matching the [navicraft, ...] pattern.

    Called periodically by the scheduler.
    """
    global _watcher_running, _last_generate_time

    if not config.navicraft_watcher_enabled:
        return

    if not config.navidrome_url or not config.navidrome_password:
        return

    # Guard against overlapping invocations (scheduler fires while previous is still running)
    if _watcher_running:
        return
    _watcher_running = True

    try:
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

            # Rate limit: wait between AI generations
            now = time.time()
            if now - _last_generate_time < _GENERATE_COOLDOWN:
                logger.info("Watcher: rate limited, skipping '%s' until next cycle", pl_name[:60])
                continue

            found += 1
            _in_progress.add(pl_id)

            try:
                result = await generate_playlist(pl_id, parsed, save=True)
                _last_generate_time = time.time()
                _mark_processed(pl_id)
                _watcher_status["last_generated"] = {
                    "playlist_id": pl_id,
                    "name": result["name"],
                    "songs": result["total_songs"],
                    "duration": result["total_duration"],
                    "prompt": parsed["prompt"],
                    "time": time.time(),
                }
                _watcher_status["total_generated"] += 1
                _watcher_status["last_error"] = None
            except Exception as e:
                logger.exception("Watcher: failed to generate playlist for '%s'", pl_name)
                _watcher_status["last_error"] = f"Failed for '{parsed['prompt'][:50]}': {e}"
                # Mark as processed to avoid retrying the same broken prompt endlessly
                _mark_processed(pl_id)
            finally:
                _in_progress.discard(pl_id)

        _watcher_status["playlists_found"] = found
    finally:
        _watcher_running = False
