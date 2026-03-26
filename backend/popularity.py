"""
MusicBrainz popularity enrichment.

Fetches song ratings from the MusicBrainz API to determine which tracks
in the library are popular/well-known. Runs as a background job after scans.

MusicBrainz API: https://musicbrainz.org/doc/MusicBrainz_API
- Rate limit: 1 request per second (we use 1.1s delay)
- No API key required
- Looks up recordings by artist + title, uses the community rating
"""

import asyncio
import logging
import httpx
from urllib.parse import quote

import database as db
from config import config

logger = logging.getLogger(__name__)

MB_BASE = "https://musicbrainz.org/ws/2"
MB_USER_AGENT = "NaviCraft/1.0 (https://github.com/chonzytron/navicraft)"
MB_DELAY = 1.1  # seconds between requests (MusicBrainz rate limit)


async def _lookup_recording(client: httpx.AsyncClient, artist: str, title: str) -> dict | None:
    """
    Look up a recording on MusicBrainz by artist + title.
    Returns {rating, rating_count} or None if not found.
    """
    query = f'recording:"{title}" AND artist:"{artist}"'
    try:
        resp = await client.get(
            f"{MB_BASE}/recording",
            params={
                "query": query,
                "fmt": "json",
                "limit": "5",
            },
        )
        if resp.status_code == 503:
            # Rate limited, wait and return None
            await asyncio.sleep(3)
            return None
        resp.raise_for_status()
        data = resp.json()

        recordings = data.get("recordings", [])
        if not recordings:
            return None

        # Pick the best match — prefer the one with the highest score
        best = recordings[0]
        score = best.get("score", 0)

        # MusicBrainz returns a relevance score (0-100) for the search match
        # Use a combination of: search relevance score and the recording's
        # community rating (if available) to derive a popularity value.
        rating_info = best.get("rating", {})
        mb_rating = rating_info.get("value")  # 0-5 scale, or None
        votes_count = rating_info.get("votes-count", 0)

        # Derive a 0-100 popularity score:
        # - Base: search score itself (how well-known/indexed in MusicBrainz)
        # - Boost: if it has a community rating with votes, factor that in
        popularity = score
        if mb_rating is not None and votes_count > 0:
            # Rating is 0-5, normalize to 0-100 and blend with score
            rating_normalized = (mb_rating / 5.0) * 100
            # Weight: 60% search score, 40% community rating
            popularity = int(score * 0.6 + rating_normalized * 0.4)

        return {
            "popularity": min(100, max(0, popularity)),
            "mb_rating": mb_rating,
            "mb_rating_count": votes_count,
        }

    except httpx.HTTPStatusError as e:
        logger.debug("MusicBrainz lookup failed for '%s - %s': %s", artist, title, e)
        return None
    except Exception as e:
        logger.debug("MusicBrainz error for '%s - %s': %s", artist, title, e)
        return None


async def enrich_popularity(batch_size: int = 200):
    """
    Fetch popularity scores from MusicBrainz for tracks that don't have one yet.
    Processes in batches to respect rate limits.
    """
    with db.get_db() as conn:
        tracks = db.get_tracks_without_popularity(conn, limit=batch_size)

    if not tracks:
        logger.info("Popularity: all tracks already enriched")
        return {"enriched": 0, "skipped": 0, "total_remaining": 0}

    logger.info("Popularity: enriching %d tracks via MusicBrainz", len(tracks))

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
                # No useful metadata, set a default low score
                with db.get_db() as conn:
                    db.update_popularity(conn, track["id"], 30, None, 0)
                skipped += 1
                continue

            result = await _lookup_recording(client, artist, title)
            await asyncio.sleep(MB_DELAY)

            if result:
                with db.get_db() as conn:
                    db.update_popularity(
                        conn,
                        track["id"],
                        result["popularity"],
                        result["mb_rating"],
                        result["mb_rating_count"],
                    )
                enriched += 1
            else:
                # Not found — set a neutral score so we don't re-query
                with db.get_db() as conn:
                    db.update_popularity(conn, track["id"], 40, None, 0)
                skipped += 1

            if (enriched + skipped) % 50 == 0:
                logger.info("Popularity: %d enriched, %d skipped so far", enriched, skipped)

    # Check remaining
    with db.get_db() as conn:
        remaining = db.count_tracks_without_popularity(conn)

    logger.info("Popularity enrichment done: %d enriched, %d skipped, %d remaining", enriched, skipped, remaining)
    return {"enriched": enriched, "skipped": skipped, "total_remaining": remaining}
