"""
Multi-source popularity enrichment for tracks.

Sources (in order of priority):
1. Last.fm — listener count and playcount (best real-world popularity signal)
2. MusicBrainz — community ratings and catalog presence
3. Local heuristic — library ownership patterns (many albums by same artist = fan)

Runs as a background job after library scans. Each track is scored once and
the result is cached in the database. Scores are re-evaluated only when
explicitly requested (e.g., full re-enrichment).
"""

import asyncio
import logging
import math
import httpx

import database as db
from config import config

logger = logging.getLogger(__name__)

# --- MusicBrainz ---
MB_BASE = "https://musicbrainz.org/ws/2"
MB_USER_AGENT = "NaviCraft/1.0 (https://github.com/chonzytron/navicraft)"
MB_DELAY = 1.1  # seconds between requests

# --- Last.fm ---
LASTFM_BASE = "https://ws.audioscrobbler.com/2.0/"
LASTFM_DELAY = 0.25  # 5 req/sec allowed


async def _lookup_lastfm(client: httpx.AsyncClient, artist: str, title: str) -> dict | None:
    """
    Look up a track on Last.fm. Returns listener count and playcount.
    These are far better popularity signals than any rating system —
    they represent actual listening behavior across millions of users.
    """
    try:
        resp = await client.get(
            LASTFM_BASE,
            params={
                "method": "track.getInfo",
                "api_key": config.lastfm_api_key,
                "artist": artist,
                "track": title,
                "format": "json",
            },
        )
        if resp.status_code == 429:
            await asyncio.sleep(2)
            return None
        resp.raise_for_status()
        data = resp.json()

        track_info = data.get("track")
        if not track_info:
            return None

        listeners = int(track_info.get("listeners", 0))
        playcount = int(track_info.get("playcount", 0))

        return {
            "listeners": listeners,
            "playcount": playcount,
        }

    except (httpx.HTTPStatusError, ValueError, KeyError) as e:
        logger.debug("Last.fm lookup failed for '%s - %s': %s", artist, title, e)
        return None
    except Exception as e:
        logger.debug("Last.fm error for '%s - %s': %s", artist, title, e)
        return None


async def _lookup_musicbrainz(client: httpx.AsyncClient, artist: str, title: str) -> dict | None:
    """
    Look up a recording on MusicBrainz by artist + title.
    Returns search score and community rating.
    """
    query = f'recording:"{title}" AND artist:"{artist}"'
    try:
        resp = await client.get(
            f"{MB_BASE}/recording",
            params={
                "query": query,
                "fmt": "json",
                "limit": "3",
            },
        )
        if resp.status_code == 503:
            await asyncio.sleep(3)
            return None
        resp.raise_for_status()
        data = resp.json()

        recordings = data.get("recordings", [])
        if not recordings:
            return None

        best = recordings[0]
        score = best.get("score", 0)
        rating_info = best.get("rating", {})
        mb_rating = rating_info.get("value")  # 0-5 scale
        votes_count = rating_info.get("votes-count", 0)

        return {
            "score": score,
            "mb_rating": mb_rating,
            "mb_rating_count": votes_count,
        }

    except (httpx.HTTPStatusError, ValueError) as e:
        logger.debug("MusicBrainz lookup failed for '%s - %s': %s", artist, title, e)
        return None
    except Exception as e:
        logger.debug("MusicBrainz error for '%s - %s': %s", artist, title, e)
        return None


def _compute_local_heuristic(artist: str, album: str,
                             artist_counts: dict, album_counts: dict) -> int:
    """
    Local library heuristic: if the user owns many tracks by an artist
    or a full album, that signals they're a fan. Boost those tracks.

    Returns a bonus score (0-15) to add to the final popularity.
    """
    bonus = 0

    # Artist depth: owning 10+ tracks by an artist = dedicated fan
    artist_tracks = artist_counts.get(artist, 0)
    if artist_tracks >= 20:
        bonus += 10
    elif artist_tracks >= 10:
        bonus += 7
    elif artist_tracks >= 5:
        bonus += 4

    # Album completeness: owning 8+ tracks from one album suggests full album
    album_key = (artist, album) if artist and album else None
    if album_key:
        album_tracks = album_counts.get(album_key, 0)
        if album_tracks >= 8:
            bonus += 5
        elif album_tracks >= 4:
            bonus += 2

    return min(bonus, 15)


def _blend_scores(lastfm: dict | None, mb: dict | None, local_bonus: int) -> int:
    """
    Combine all sources into a single 0-100 popularity score.

    Priority:
    1. Last.fm listeners (best signal — real usage data)
    2. MusicBrainz rating + catalog presence
    3. Local heuristic (library ownership pattern)

    If Last.fm has data, it dominates. MusicBrainz fills in gaps.
    Local bonus always applies as a small boost.
    """
    score = 40  # neutral baseline for unknown tracks

    if lastfm and lastfm["listeners"] > 0:
        # Map listener count to 0-100 using log scale
        # ~1k listeners = ~40, ~50k = ~60, ~500k = ~75, ~5M = ~90
        listeners = lastfm["listeners"]
        if listeners > 0:
            log_score = math.log10(max(listeners, 1)) / math.log10(10_000_000) * 100
            score = min(95, max(20, int(log_score)))

    elif mb and mb.get("score", 0) > 0:
        # Fall back to MusicBrainz
        mb_score = mb["score"]
        mb_rating = mb.get("mb_rating")
        mb_votes = mb.get("mb_rating_count", 0)

        if mb_rating is not None and mb_votes > 0:
            rating_norm = (mb_rating / 5.0) * 100
            score = int(mb_score * 0.5 + rating_norm * 0.5)
        else:
            # Just catalog presence — a high search score means it's well-indexed
            score = int(mb_score * 0.7)

    # Apply local bonus (capped at 100)
    score = min(100, score + local_bonus)

    return max(0, min(100, score))


async def enrich_popularity(batch_size: int = 200):
    """
    Fetch popularity scores for tracks that don't have one yet.
    Uses Last.fm (if API key configured) + MusicBrainz + local heuristics.
    Processes in batches to respect rate limits.
    """
    with db.get_db() as conn:
        tracks = db.get_tracks_without_popularity(conn, limit=batch_size)

    if not tracks:
        logger.info("Popularity: all tracks already enriched")
        return {"enriched": 0, "skipped": 0, "total_remaining": 0}

    logger.info("Popularity: enriching %d tracks", len(tracks))

    # Pre-compute local heuristics (one query, used for all tracks)
    with db.get_db() as conn:
        artist_counts = db.get_artist_track_counts(conn)
        album_counts = db.get_album_track_counts(conn)

    has_lastfm = bool(config.lastfm_api_key)
    if has_lastfm:
        logger.info("Popularity: using Last.fm + MusicBrainz + local heuristics")
    else:
        logger.info("Popularity: using MusicBrainz + local heuristics (no LASTFM_API_KEY set)")

    enriched = 0
    skipped = 0

    async with httpx.AsyncClient(
        timeout=15,
        headers={"User-Agent": MB_USER_AGENT, "Accept": "application/json"},
    ) as client:
        for track in tracks:
            artist = track.get("artist", "")
            title = track.get("title", "")

            if not artist or not title:
                local_bonus = _compute_local_heuristic(
                    artist, track.get("album", ""), artist_counts, album_counts
                )
                with db.get_db() as conn:
                    db.update_popularity(conn, track["id"], 25 + local_bonus, None, 0)
                skipped += 1
                continue

            # Source 1: Last.fm
            lastfm_result = None
            if has_lastfm:
                lastfm_result = await _lookup_lastfm(client, artist, title)
                await asyncio.sleep(LASTFM_DELAY)

            # Source 2: MusicBrainz (always, but skip if Last.fm gave strong data)
            mb_result = None
            need_mb = (
                lastfm_result is None
                or lastfm_result.get("listeners", 0) == 0
            )
            if need_mb:
                mb_result = await _lookup_musicbrainz(client, artist, title)
                await asyncio.sleep(MB_DELAY)

            # Source 3: Local heuristic
            local_bonus = _compute_local_heuristic(
                artist, track.get("album", ""), artist_counts, album_counts
            )

            # Blend all sources
            popularity = _blend_scores(lastfm_result, mb_result, local_bonus)

            # Store results
            with db.get_db() as conn:
                db.update_popularity(
                    conn,
                    track["id"],
                    popularity,
                    mb_result.get("mb_rating") if mb_result else None,
                    mb_result.get("mb_rating_count", 0) if mb_result else 0,
                    lastfm_result.get("listeners") if lastfm_result else None,
                    lastfm_result.get("playcount") if lastfm_result else None,
                )
            enriched += 1

            if (enriched + skipped) % 50 == 0:
                logger.info("Popularity: %d enriched, %d skipped so far", enriched, skipped)

    # Check remaining
    with db.get_db() as conn:
        remaining = db.count_tracks_without_popularity(conn)

    logger.info(
        "Popularity enrichment done: %d enriched, %d skipped, %d remaining",
        enriched, skipped, remaining
    )
    return {"enriched": enriched, "skipped": skipped, "total_remaining": remaining}
