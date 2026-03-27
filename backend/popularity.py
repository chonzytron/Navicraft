"""
Multi-source popularity enrichment for tracks.

Sources (weighted by data quality):
1. Spotify — popularity score (0-100) from real streaming data (best signal)
2. Last.fm — listener count + playcount (real-world usage from millions of users)
3. MusicBrainz — community ratings + release count (how many compilations/releases)
4. Track position heuristic — early album tracks (1-3) are more likely singles/hits

Scoring philosophy:
- Each source contributes a weighted sub-score based on how much data it has
- More data points = higher confidence = higher weight in the blend
- Unknown tracks get a neutral baseline (50) so they're not buried

Pipeline strategy:
- Phase 1: Spotify + Last.fm lookups concurrently (both fast, ~5-30 req/s)
- Phase 2: MusicBrainz only for tracks not well-covered by Phase 1 (1 req/s)
"""

import asyncio
import base64
import logging
import math
import time
import httpx

import database as db
from config import config

logger = logging.getLogger(__name__)

# Prevents concurrent enrichment runs (startup, post-scan, and scheduler can all call this)
_enrichment_lock = asyncio.Lock()

# --- MusicBrainz ---
MB_BASE = "https://musicbrainz.org/ws/2"
MB_USER_AGENT = "NaviCraft/1.0 (https://github.com/chonzytron/navicraft)"
MB_DELAY = 1.1  # seconds between requests

# --- Last.fm ---
LASTFM_BASE = "https://ws.audioscrobbler.com/2.0/"
LASTFM_DELAY = 0.25  # 5 req/sec allowed

# --- Spotify ---
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_SEARCH_URL = "https://api.spotify.com/v1/search"
SPOTIFY_DELAY = 0.2  # ~5 req/s — conservative to avoid 429s

# Spotify token cache
_spotify_token: str | None = None
_spotify_token_expires: float = 0


async def _get_spotify_token(client: httpx.AsyncClient) -> str | None:
    """Get or refresh Spotify access token using Client Credentials flow."""
    global _spotify_token, _spotify_token_expires

    if _spotify_token and time.time() < _spotify_token_expires - 60:
        return _spotify_token

    try:
        auth = base64.b64encode(
            f"{config.spotify_client_id}:{config.spotify_client_secret}".encode()
        ).decode()
        resp = await client.post(
            SPOTIFY_TOKEN_URL,
            headers={
                "Authorization": f"Basic {auth}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials"},
        )
        resp.raise_for_status()
        data = resp.json()
        _spotify_token = data["access_token"]
        _spotify_token_expires = time.time() + data.get("expires_in", 3600)
        logger.info("Spotify token acquired (expires in %ds)", data.get("expires_in", 3600))
        return _spotify_token
    except Exception as e:
        logger.warning("Spotify token acquisition failed: %s", e)
        return None


async def _lookup_spotify(client: httpx.AsyncClient, artist: str, title: str,
                          token: str) -> dict | None:
    """
    Look up a track on Spotify. Returns the popularity score (0-100),
    or {"rate_limited": True} if the API is throttling us.
    Does NOT sleep/retry on 429 — the caller tracks consecutive hits
    and disables Spotify for the batch after SPOTIFY_429_LIMIT misses.
    """
    try:
        query = f"track:{title} artist:{artist}"
        resp = await client.get(
            SPOTIFY_SEARCH_URL,
            params={"q": query, "type": "track", "limit": 3},
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code == 429:
            return {"rate_limited": True}
        if resp.status_code == 401:
            return None
        resp.raise_for_status()
        data = resp.json()

        tracks = data.get("tracks", {}).get("items", [])
        if not tracks:
            return None

        artist_lower = artist.lower()
        for track in tracks:
            track_artists = [a["name"].lower() for a in track.get("artists", [])]
            if any(artist_lower in a or a in artist_lower for a in track_artists):
                return {"popularity": track.get("popularity", 0)}

        return {"popularity": tracks[0].get("popularity", 0)}

    except (httpx.HTTPStatusError, ValueError, KeyError) as e:
        logger.debug("Spotify lookup failed for '%s - %s': %s", artist, title, e)
        return None
    except Exception as e:
        logger.debug("Spotify error for '%s - %s': %s", artist, title, e)
        return None


async def _lookup_lastfm(client: httpx.AsyncClient, artist: str, title: str) -> dict | None:
    """
    Look up a track on Last.fm. Returns listener count and playcount.
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
    Returns search score, community rating, and release count.
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

        # Count how many releases this recording appears on.
        # Songs on many releases (compilations, best-ofs, soundtracks) = hits.
        release_count = len(best.get("releases", []))

        return {
            "score": score,
            "mb_rating": mb_rating,
            "mb_rating_count": votes_count,
            "release_count": release_count,
        }

    except (httpx.HTTPStatusError, ValueError) as e:
        logger.debug("MusicBrainz lookup failed for '%s - %s': %s", artist, title, e)
        return None
    except Exception as e:
        logger.debug("MusicBrainz error for '%s - %s': %s", artist, title, e)
        return None


def _spotify_score(spotify: dict) -> tuple[float, float]:
    """
    Derive a score (0-100) and confidence (0-1) from Spotify data.

    Spotify popularity is already 0-100 and based on real streaming data,
    so it's the most reliable signal. We use it nearly as-is.
    """
    pop = spotify.get("popularity", 0)
    if pop <= 0:
        return 0.0, 0.1  # Track exists on Spotify but has ~0 plays

    # Spotify popularity is already well-calibrated
    score = float(pop)

    # High confidence for any Spotify data — it's the best signal
    if pop >= 50:
        confidence = 1.0
    elif pop >= 20:
        confidence = 0.9
    elif pop >= 5:
        confidence = 0.7
    else:
        confidence = 0.5

    return score, confidence


def _lastfm_score(lastfm: dict) -> tuple[float, float]:
    """
    Derive a score (0-100) and confidence weight (0-1) from Last.fm data.

    Listener count is the primary signal.
    Replay ratio (playcount / listeners) gives a quality bonus:
    a song people play many times is stickier than one they try once.
    """
    listeners = lastfm.get("listeners", 0)
    playcount = lastfm.get("playcount", 0)

    if listeners <= 0:
        return 0.0, 0.0

    # Map listeners to 0-95 via log scale
    # ~500 = ~38, ~5k = ~53, ~50k = ~67, ~500k = ~81, ~5M = ~95
    log_score = math.log10(max(listeners, 1)) / math.log10(10_000_000) * 95
    base = min(95, max(15, log_score))

    # Replay ratio bonus: avg plays per listener above 1x indicates replay value
    if listeners > 0:
        replay_ratio = playcount / listeners
        # Typical ratio is 3-10 for popular tracks
        # Give up to +5 bonus for high replay ratio
        replay_bonus = min(5, max(0, (replay_ratio - 1) * 0.8))
        base = min(100, base + replay_bonus)

    # Confidence: high if we have substantial listener data
    if listeners >= 100_000:
        confidence = 1.0
    elif listeners >= 10_000:
        confidence = 0.9
    elif listeners >= 1_000:
        confidence = 0.75
    elif listeners >= 100:
        confidence = 0.5
    else:
        confidence = 0.3

    return base, confidence


def _musicbrainz_score(mb: dict) -> tuple[float, float]:
    """
    Derive a score (0-100) and confidence weight (0-1) from MusicBrainz data.

    Uses three signals:
    - Community rating (0-5 scale, if votes exist)
    - Release count (songs on many releases = well-known)
    - Search relevance score (well-indexed = more notable)
    """
    mb_rating = mb.get("mb_rating")
    votes = mb.get("mb_rating_count", 0)
    release_count = mb.get("release_count", 0)
    search_score = mb.get("score", 0)

    components = []
    total_weight = 0

    # Community rating — only trust it with enough votes
    if mb_rating is not None and votes > 0:
        rating_norm = (mb_rating / 5.0) * 100
        if votes >= 10:
            components.append(rating_norm * 0.5)
            total_weight += 0.5
        elif votes >= 3:
            components.append(rating_norm * 0.3)
            total_weight += 0.3
        else:
            components.append(rating_norm * 0.1)
            total_weight += 0.1

    # Release count — appearing on compilations/best-ofs is a strong hit signal
    if release_count > 0:
        # 1 release = normal, 3+ = likely single, 5+ = definite hit
        release_score = min(90, 40 + release_count * 10)
        release_weight = min(0.4, release_count * 0.08)
        components.append(release_score * release_weight)
        total_weight += release_weight

    # Search score — baseline signal, always available
    search_weight = max(0.1, 0.5 - total_weight)
    components.append(search_score * search_weight)
    total_weight += search_weight

    if total_weight > 0:
        score = sum(components) / total_weight
    else:
        score = search_score * 0.6

    # Confidence depends on how much real data we have
    confidence = 0.0
    if votes >= 10:
        confidence = 0.7
    elif votes >= 3:
        confidence = 0.5
    elif release_count >= 3:
        confidence = 0.45
    elif search_score >= 90:
        confidence = 0.3
    else:
        confidence = 0.2

    return min(100, max(0, score)), confidence


def _track_position_bonus(track_number: int | None) -> int:
    """
    Albums typically front-load singles and stronger tracks.
    Track 1-2 are often the lead singles or openers chosen to hook listeners.
    Track 3-4 often includes the second single.

    Returns a small bonus (0-5).
    """
    if track_number is None:
        return 0
    if track_number <= 2:
        return 5
    if track_number <= 4:
        return 3
    return 0


def _blend_scores(spotify: dict | None, lastfm: dict | None, mb: dict | None,
                  track_number: int | None) -> int:
    """
    Combine all sources into a single 0-100 score.

    Strategy: weighted average where each source's weight is its confidence.
    If multiple sources have high confidence, all contribute.
    If only one has data, it dominates. Unknown tracks get baseline 50.
    """
    scores_and_weights = []

    if spotify and spotify.get("popularity", 0) > 0:
        sp_score, sp_conf = _spotify_score(spotify)
        scores_and_weights.append((sp_score, sp_conf))

    if lastfm and lastfm.get("listeners", 0) > 0:
        lfm_score, lfm_conf = _lastfm_score(lastfm)
        scores_and_weights.append((lfm_score, lfm_conf))

    if mb and mb.get("score", 0) > 0:
        mb_score, mb_conf = _musicbrainz_score(mb)
        scores_and_weights.append((mb_score, mb_conf))

    if not scores_and_weights:
        # No external data at all — neutral baseline
        return min(100, 50 + _track_position_bonus(track_number))

    # Weighted average by confidence
    total_weight = sum(w for _, w in scores_and_weights)
    if total_weight > 0:
        blended = sum(s * w for s, w in scores_and_weights) / total_weight
    else:
        blended = 50

    # Apply track position bonus
    blended += _track_position_bonus(track_number)

    return max(0, min(100, int(blended)))


async def enrich_popularity(batch_size: int = 500):
    """
    Fetch popularity scores for tracks that don't have one yet.

    Pipeline (per track, so DB is updated incrementally):
    - Spotify lookup (if configured), then Last.fm lookup (if configured),
      then MusicBrainz only for tracks not well-covered by Spotify/Last.fm.
    - DB write happens immediately after each track so progress is visible.
    """
    if _enrichment_lock.locked():
        logger.info("Popularity: enrichment already running, skipping duplicate call")
        return {"enriched": 0, "skipped": 0, "total_remaining": -1}

    await _enrichment_lock.acquire()
    try:
        with db.get_db() as conn:
            tracks = db.get_tracks_without_popularity(conn, limit=batch_size)

        if not tracks:
            logger.info("Popularity: all tracks already enriched")
            return {"enriched": 0, "skipped": 0, "total_remaining": 0}

        logger.info("Popularity: enriching %d tracks", len(tracks))

        has_lastfm = bool(config.lastfm_api_key)
        has_spotify = bool(config.spotify_client_id and config.spotify_client_secret)

        sources = []
        if has_spotify:
            sources.append("Spotify")
        if has_lastfm:
            sources.append("Last.fm")
        sources.append("MusicBrainz (fallback)")
        logger.info("Popularity: sources — %s", " + ".join(sources))

        enriched = 0
        skipped = 0
        needs_mb_count = 0
        WRITE_BATCH = 50  # flush to DB every N tracks

        pending: list[tuple] = []  # buffered rows for bulk write

        def flush_pending():
            if pending:
                with db.get_db() as conn:
                    db.bulk_update_popularity(conn, pending)
                pending.clear()

        async with httpx.AsyncClient(
            timeout=15,
            headers={"User-Agent": MB_USER_AGENT, "Accept": "application/json"},
        ) as client:
            # Get Spotify token if configured
            spotify_token = None
            if has_spotify:
                spotify_token = await _get_spotify_token(client)
                if not spotify_token:
                    logger.warning("Popularity: Spotify token failed, continuing without Spotify")

            spotify_consecutive_429s = 0
            SPOTIFY_429_LIMIT = 3  # disable Spotify for batch after this many consecutive 429s

            for track in tracks:
                artist = track.get("artist", "")
                title = track.get("title", "")
                track_number = track.get("track_number")

                if not artist or not title:
                    pending.append((30, None, 0, None, None, None, track["id"]))
                    skipped += 1
                else:
                    # Spotify
                    spotify_result = None
                    if spotify_token:
                        raw = await _lookup_spotify(client, artist, title, spotify_token)
                        await asyncio.sleep(SPOTIFY_DELAY)
                        if raw and raw.get("rate_limited"):
                            spotify_consecutive_429s += 1
                            if spotify_consecutive_429s >= SPOTIFY_429_LIMIT:
                                logger.warning(
                                    "Popularity: Spotify rate-limited %d times in a row — "
                                    "disabling for remainder of this batch",
                                    spotify_consecutive_429s,
                                )
                                spotify_token = None  # skip Spotify for rest of batch
                        else:
                            spotify_consecutive_429s = 0
                            spotify_result = raw

                    # Last.fm
                    lastfm_result = None
                    if has_lastfm:
                        lastfm_result = await _lookup_lastfm(client, artist, title)
                        await asyncio.sleep(LASTFM_DELAY)

                    # MusicBrainz — only when Spotify/Last.fm don't have good coverage
                    has_good_spotify = (spotify_result and spotify_result.get("popularity", 0) >= 20)
                    has_good_lastfm = (lastfm_result and lastfm_result.get("listeners", 0) >= 100_000)
                    mb_result = None
                    if not (has_good_spotify or has_good_lastfm):
                        mb_result = await _lookup_musicbrainz(client, artist, title)
                        await asyncio.sleep(MB_DELAY)
                        needs_mb_count += 1

                    popularity = _blend_scores(spotify_result, lastfm_result, mb_result, track_number)
                    pending.append((
                        popularity,
                        mb_result.get("mb_rating") if mb_result else None,
                        mb_result.get("mb_rating_count", 0) if mb_result else 0,
                        lastfm_result.get("listeners") if lastfm_result else None,
                        lastfm_result.get("playcount") if lastfm_result else None,
                        spotify_result.get("popularity") if spotify_result else None,
                        track["id"],
                    ))
                    enriched += 1

                if len(pending) >= WRITE_BATCH:
                    flush_pending()

                if (enriched + skipped) % 100 == 0:
                    logger.info("Popularity: %d enriched, %d skipped so far (%d MB lookups)", enriched, skipped, needs_mb_count)

            flush_pending()  # write any remaining tracks

        # Check remaining
        with db.get_db() as conn:
            remaining = db.count_tracks_without_popularity(conn)

        logger.info(
            "Popularity enrichment done: %d enriched, %d skipped, %d remaining",
            enriched, skipped, remaining
        )
        return {"enriched": enriched, "skipped": skipped, "total_remaining": remaining}
    finally:
        _enrichment_lock.release()
