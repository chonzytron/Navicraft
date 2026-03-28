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
SPOTIFY_DELAY = 1.0  # ~1 req/s — Spotify Client Credentials search endpoint allows ~60 req/min

# Spotify token cache
_spotify_token: str | None = None
_spotify_token_expires: float = 0

# Spotify rate-limit cooldown — set when a batch gets rate-limited,
# skip Spotify entirely until this timestamp passes.
# Honours Retry-After header if Spotify sends one.
SPOTIFY_COOLDOWN_SECONDS = 600  # fallback cooldown if no Retry-After header
_spotify_blocked_until: float = 0


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
            retry_after = resp.headers.get("Retry-After")
            return {"rate_limited": True, "retry_after": int(retry_after) if retry_after and retry_after.isdigit() else None}
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

        has_lastfm = bool(config.lastfm_api_key)
        has_spotify = bool(config.spotify_client_id and config.spotify_client_secret)

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
            global _spotify_blocked_until
            # Get Spotify token if configured
            spotify_token = None
            if has_spotify:
                if time.time() < _spotify_blocked_until:
                    remaining_cooldown = int(_spotify_blocked_until - time.time())
                    logger.info("Popularity: Spotify in cooldown for %ds more — skipping this batch", remaining_cooldown)
                else:
                    spotify_token = await _get_spotify_token(client)
                    if not spotify_token:
                        logger.warning("Popularity: Spotify token failed, continuing without Spotify")

            if not tracks:
                logger.info("Popularity: all tracks already enriched — checking Spotify top-up")
            else:
                sources = []
                if spotify_token:
                    sources.append("Spotify")
                if has_lastfm:
                    sources.append("Last.fm")
                sources.append("MusicBrainz (fallback)")
                logger.info("Popularity: enriching %d tracks via %s", len(tracks), " + ".join(sources))

            spotify_consecutive_429s = 0
            SPOTIFY_429_LIMIT = 1  # disable Spotify immediately on the first 429

            if tracks:
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
                                    retry_after = raw.get("retry_after") or SPOTIFY_COOLDOWN_SECONDS
                                    logger.warning(
                                        "Popularity: Spotify rate-limited — "
                                        "disabling for %ds (Retry-After: %s)",
                                        retry_after,
                                        raw.get("retry_after"),
                                    )
                                    _spotify_blocked_until = time.time() + retry_after
                                    spotify_token = None  # skip Spotify for rest of batch
                            else:
                                spotify_consecutive_429s = 0
                                spotify_result = raw

                        # Last.fm
                        lastfm_result = None
                        if has_lastfm:
                            lastfm_result = await _lookup_lastfm(client, artist, title)
                            await asyncio.sleep(LASTFM_DELAY)

                        # MusicBrainz — only when neither Spotify nor Last.fm returned anything
                        has_good_spotify = (spotify_result and spotify_result.get("popularity", 0) >= 20)
                        has_good_lastfm = bool(lastfm_result and lastfm_result.get("listeners") is not None)
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

            # --- Spotify top-up pass ---
            # If Spotify is currently working, go back and fill in tracks that were
            # enriched without Spotify data (e.g. enriched during a prior cooldown).
            spotify_topup = 0
            if spotify_token:
                with db.get_db() as conn:
                    missing_spotify = db.get_tracks_missing_spotify(conn, limit=batch_size)

                if missing_spotify:
                    logger.info("Popularity: Spotify top-up — %d tracks missing Spotify data", len(missing_spotify))
                    topup_pending: list[tuple] = []

                    for track in missing_spotify:
                        if _spotify_blocked_until and time.time() < _spotify_blocked_until:
                            break  # still in cooldown mid-pass, stop cleanly

                        artist = track.get("artist", "")
                        title = track.get("title", "")
                        if not artist or not title:
                            continue

                        raw = await _lookup_spotify(client, artist, title, spotify_token)
                        await asyncio.sleep(SPOTIFY_DELAY)

                        if raw and raw.get("rate_limited"):
                            spotify_consecutive_429s += 1
                            if spotify_consecutive_429s >= SPOTIFY_429_LIMIT:
                                retry_after = raw.get("retry_after") or SPOTIFY_COOLDOWN_SECONDS
                                logger.warning(
                                    "Popularity: Spotify rate-limited during top-up — "
                                    "disabling for %ds (Retry-After: %s)",
                                    retry_after,
                                    raw.get("retry_after"),
                                )
                                _spotify_blocked_until = time.time() + retry_after
                                break
                            continue

                        spotify_consecutive_429s = 0
                        if raw is None:
                            continue  # track not found on Spotify, leave as-is

                        # Reblend with existing Last.fm/MB data already stored in DB
                        lastfm_result = None
                        if track.get("lastfm_listeners") is not None:
                            lastfm_result = {
                                "listeners": track["lastfm_listeners"],
                                "playcount": track.get("lastfm_playcount"),
                            }
                        mb_result = None
                        if track.get("mb_rating") is not None:
                            mb_result = {
                                "mb_rating": track["mb_rating"],
                                "mb_rating_count": track.get("mb_rating_count", 0),
                            }

                        new_popularity = _blend_scores(raw, lastfm_result, mb_result, track.get("track_number"))
                        topup_pending.append((new_popularity, raw["popularity"], track["id"]))
                        spotify_topup += 1

                        if len(topup_pending) >= WRITE_BATCH:
                            with db.get_db() as conn:
                                db.update_spotify_popularity(conn, topup_pending)
                            topup_pending.clear()

                    if topup_pending:
                        with db.get_db() as conn:
                            db.update_spotify_popularity(conn, topup_pending)

                    logger.info("Popularity: Spotify top-up done — %d tracks updated", spotify_topup)

        # Check remaining
        with db.get_db() as conn:
            remaining = db.count_tracks_without_popularity(conn)
            missing_spotify = db.count_tracks_missing_spotify(conn)

        logger.info(
            "Popularity enrichment done: %d enriched, %d skipped, %d remaining, %d missing Spotify",
            enriched, skipped, remaining, missing_spotify
        )
        return {"enriched": enriched, "skipped": skipped, "total_remaining": remaining, "missing_spotify": missing_spotify}
    finally:
        _enrichment_lock.release()
