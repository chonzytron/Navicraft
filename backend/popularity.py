"""
Multi-source popularity enrichment for tracks.

Sources (weighted by data quality):
1. Deezer — track rank from real streaming data (best signal, no auth needed)
2. Last.fm — listener count + playcount (real-world usage from millions of users)
3. MusicBrainz — community ratings (0-100) + rating vote count (no auth needed)
4. Track position heuristic — early album tracks (1-3) are more likely singles/hits

Scoring philosophy:
- Each source contributes a weighted sub-score based on how much data it has
- More data points = higher confidence = higher weight in the blend
- Unknown tracks get a neutral baseline (50) so they're not buried

Pipeline strategy:
- Phase 1: Deezer + Last.fm + MusicBrainz lookups
- Deezer API is free with no authentication — 50 requests per 5 seconds
- MusicBrainz API is free with no authentication — 1 request per second (strict)
"""

import asyncio
import logging
import math
import time
import httpx

import database as db
from config import config

logger = logging.getLogger(__name__)

# Prevents concurrent enrichment runs (startup, post-scan, and scheduler can all call this)
_enrichment_lock = asyncio.Lock()

# --- Deezer ---
DEEZER_SEARCH_URL = "https://api.deezer.com/search"
DEEZER_DELAY = 0.1  # 10 req/s — well within the 50 req/5s limit
DEEZER_SLOWDOWN_DELAY = 0.5  # slower rate after recovering from rate limiting

# --- Last.fm ---
LASTFM_BASE = "https://ws.audioscrobbler.com/2.0/"
LASTFM_DELAY = 0.25  # 5 req/sec allowed

# --- MusicBrainz ---
MUSICBRAINZ_BASE = "https://musicbrainz.org/ws/2"
MUSICBRAINZ_DELAY = 1.1  # Strict: max 1 req/sec, use 1.1s to stay safe
MUSICBRAINZ_USER_AGENT = "NaviCraft/2.0 (https://github.com/chonzytron/navicraft)"


async def _lookup_deezer(client: httpx.AsyncClient, artist: str, title: str) -> dict | None:
    """
    Look up a track on Deezer. Returns the rank (0-1000000) and deezer_id,
    or {"not_found": True} if the track doesn't exist on Deezer.
    No authentication needed — the Deezer API is free.
    """
    try:
        query = f'artist:"{artist}" track:"{title}"'
        resp = await client.get(
            DEEZER_SEARCH_URL,
            params={"q": query, "limit": 5},
        )
        if resp.status_code == 429:
            return {"rate_limited": True}
        resp.raise_for_status()
        data = resp.json()

        # Deezer returns errors as JSON with an "error" key
        if "error" in data:
            error_code = data["error"].get("code", 0)
            if error_code == 4:  # Quota limit exceeded
                return {"rate_limited": True}
            logger.debug("Deezer API error: %s", data["error"])
            return None

        tracks = data.get("data", [])
        if not tracks:
            return {"not_found": True}

        artist_lower = artist.lower()
        for track in tracks:
            track_artist = (track.get("artist", {}).get("name", "")).lower()
            if artist_lower in track_artist or track_artist in artist_lower:
                return {
                    "rank": track.get("rank", 0),
                    "deezer_id": str(track.get("id", "")),
                }

        # Fallback to first result
        return {
            "rank": tracks[0].get("rank", 0),
            "deezer_id": str(tracks[0].get("id", "")),
        }

    except (httpx.HTTPStatusError, ValueError, KeyError) as e:
        logger.debug("Deezer lookup failed for '%s - %s': %s", artist, title, e)
        return None
    except Exception as e:
        logger.debug("Deezer error for '%s - %s': %s", artist, title, e)
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
            return {"not_found": True}  # API worked, track simply doesn't exist on Last.fm

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
    Look up a track on MusicBrainz. Returns the community rating (0-100) and vote count.
    MusicBrainz API is free, no auth needed, but strictly limited to 1 request per second.
    Must include a descriptive User-Agent header per their API terms.
    """
    try:
        query = f'recording:"{title}" AND artist:"{artist}"'
        resp = await client.get(
            f"{MUSICBRAINZ_BASE}/recording",
            params={"query": query, "limit": 5, "fmt": "json"},
            headers={"User-Agent": MUSICBRAINZ_USER_AGENT},
        )
        if resp.status_code == 429 or resp.status_code == 503:
            return {"rate_limited": True}
        resp.raise_for_status()
        data = resp.json()

        recordings = data.get("recordings", [])
        if not recordings:
            return {"not_found": True}

        # Try to match artist name
        artist_lower = artist.lower()
        best = None
        for rec in recordings:
            rec_artists = rec.get("artist-credit", [])
            for ac in rec_artists:
                ac_name = (ac.get("name") or ac.get("artist", {}).get("name", "")).lower()
                if artist_lower in ac_name or ac_name in artist_lower:
                    best = rec
                    break
            if best:
                break

        if not best:
            best = recordings[0]

        rating_obj = best.get("rating", {})
        rating_value = rating_obj.get("value")  # 0-5 scale or None
        rating_count = rating_obj.get("votes-count", 0)

        if rating_value is None and rating_count == 0:
            # Recording exists but has no community ratings
            return {"not_found": True}

        # Convert from 0-5 scale to 0-100
        rating_100 = round((rating_value or 0) * 20)

        return {
            "rating": rating_100,
            "rating_count": rating_count,
        }

    except (httpx.HTTPStatusError, ValueError, KeyError) as e:
        logger.debug("MusicBrainz lookup failed for '%s - %s': %s", artist, title, e)
        return None
    except Exception as e:
        logger.debug("MusicBrainz error for '%s - %s': %s", artist, title, e)
        return None


def _deezer_score(deezer: dict) -> tuple[float, float]:
    """
    Derive a score (0-100) and confidence (0-1) from Deezer data.

    Deezer rank ranges from 0 to ~1,000,000. Higher = more popular.
    We map this to a 0-100 scale using a logarithmic curve.
    """
    rank = deezer.get("rank", 0)
    if rank <= 0:
        return 0.0, 0.1  # Track exists on Deezer but has ~0 rank

    # Map rank (0-1M) to 0-100 via log scale
    # rank ~1000 -> ~30, ~10000 -> ~45, ~100000 -> ~65, ~500000 -> ~82, ~900000 -> ~95
    log_score = math.log10(max(rank, 1)) / math.log10(1_000_000) * 100
    score = min(100, max(0, log_score))

    # Confidence based on rank magnitude
    if rank >= 500_000:
        confidence = 1.0
    elif rank >= 100_000:
        confidence = 0.9
    elif rank >= 10_000:
        confidence = 0.7
    elif rank >= 1_000:
        confidence = 0.5
    else:
        confidence = 0.3

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
    Derive a score (0-100) and confidence (0-1) from MusicBrainz data.

    MusicBrainz ratings are community votes on a 0-100 scale (converted from 0-5).
    The rating_count (number of votes) determines confidence — more votes = more reliable.
    """
    rating = mb.get("rating", 0)
    count = mb.get("rating_count", 0)

    if count <= 0:
        return 0.0, 0.0

    # The rating itself is already on a 0-100 scale
    score = min(100, max(0, float(rating)))

    # Confidence based on vote count
    if count >= 50:
        confidence = 0.9
    elif count >= 20:
        confidence = 0.7
    elif count >= 10:
        confidence = 0.5
    elif count >= 3:
        confidence = 0.3
    else:
        confidence = 0.15

    return score, confidence


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


def _blend_scores(deezer: dict | None, lastfm: dict | None,
                  track_number: int | None,
                  musicbrainz: dict | None = None) -> int:
    """
    Combine all sources into a single 0-100 score.

    Strategy: weighted average where each source's weight is its confidence.
    If multiple sources have high confidence, all contribute.
    If only one has data, it dominates. Unknown tracks get baseline 50.
    """
    scores_and_weights = []

    if deezer and deezer.get("rank", 0) > 0:
        dz_score, dz_conf = _deezer_score(deezer)
        scores_and_weights.append((dz_score, dz_conf))

    if lastfm and lastfm.get("listeners", 0) > 0:
        lfm_score, lfm_conf = _lastfm_score(lastfm)
        scores_and_weights.append((lfm_score, lfm_conf))

    if musicbrainz and musicbrainz.get("rating_count", 0) > 0:
        mb_score, mb_conf = _musicbrainz_score(musicbrainz)
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

    return max(0, min(100, round(blended)))


async def enrich_popularity(batch_size: int = 500):
    """
    Fetch popularity scores for tracks that don't have one yet.

    Pipeline (per track, so DB is updated incrementally):
    - Deezer lookup (free, no auth), then Last.fm lookup (if configured).
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

        enriched = 0
        skipped = 0
        WRITE_BATCH = 50  # flush to DB every N tracks

        pending: list[tuple] = []  # buffered rows for bulk write

        def flush_pending():
            if pending:
                try:
                    with db.get_db() as conn:
                        db.bulk_update_popularity(conn, pending)
                except Exception:
                    logger.exception("Popularity: failed to flush %d pending rows", len(pending))
                pending.clear()

        async with httpx.AsyncClient(
            timeout=15,
            headers={"Accept": "application/json"},
        ) as client:

            if not tracks:
                logger.info("Popularity: all tracks already enriched — checking top-ups")
            else:
                sources = ["Deezer", "MusicBrainz"]
                if has_lastfm:
                    sources.append("Last.fm")
                logger.info("Popularity: enriching %d tracks via %s", len(tracks), " + ".join(sources))

            deezer_delay = DEEZER_DELAY  # adaptive: slows after rate limit recovery

            if tracks:
                for track in tracks:
                    artist = track.get("artist", "")
                    title = track.get("title", "")
                    track_number = track.get("track_number")

                    if not artist or not title:
                        pending.append((30, None, None, None, None, None, None, None, None, None, track["id"]))
                        skipped += 1
                    else:
                        now = time.time()
                        # Deezer
                        deezer_result = None
                        deezer_checked_at = None
                        raw = await _lookup_deezer(client, artist, title)
                        if raw and raw.get("rate_limited"):
                            # Wait and retry
                            logger.info("Popularity: Deezer rate limited — waiting 5s before retry")
                            await asyncio.sleep(5)
                            raw = await _lookup_deezer(client, artist, title)
                            if raw and raw.get("rate_limited"):
                                logger.warning("Popularity: Deezer still rate limited — slowing down")
                                deezer_delay = DEEZER_SLOWDOWN_DELAY
                            else:
                                deezer_delay = DEEZER_SLOWDOWN_DELAY
                                logger.info("Popularity: Deezer recovered — slowing to %.1fs delay", deezer_delay)
                                if raw and raw.get("not_found"):
                                    deezer_checked_at = now
                                elif raw:
                                    deezer_result = raw
                                    deezer_checked_at = now
                        elif raw and raw.get("not_found"):
                            deezer_checked_at = now  # confirmed not on Deezer, retry tomorrow
                        elif raw:
                            deezer_result = raw
                            deezer_checked_at = now
                        await asyncio.sleep(deezer_delay)

                        # Last.fm
                        lastfm_result = None
                        lastfm_checked_at = None
                        if has_lastfm:
                            raw_lfm = await _lookup_lastfm(client, artist, title)
                            await asyncio.sleep(LASTFM_DELAY)
                            if raw_lfm and raw_lfm.get("not_found"):
                                lastfm_checked_at = now  # confirmed not on Last.fm, retry tomorrow
                            elif raw_lfm and not raw_lfm.get("not_found"):
                                lastfm_result = raw_lfm
                                lastfm_checked_at = now

                        # MusicBrainz
                        mb_result = None
                        mb_checked_at = None
                        raw_mb = await _lookup_musicbrainz(client, artist, title)
                        if raw_mb and raw_mb.get("rate_limited"):
                            logger.info("Popularity: MusicBrainz rate limited — waiting 5s before retry")
                            await asyncio.sleep(5)
                            raw_mb = await _lookup_musicbrainz(client, artist, title)
                        await asyncio.sleep(MUSICBRAINZ_DELAY)
                        if raw_mb and raw_mb.get("not_found"):
                            mb_checked_at = now
                        elif raw_mb and not raw_mb.get("rate_limited"):
                            mb_result = raw_mb
                            mb_checked_at = now

                        popularity = _blend_scores(deezer_result, lastfm_result, track_number, mb_result)
                        pending.append((
                            popularity,
                            lastfm_result.get("listeners") if lastfm_result else None,
                            lastfm_result.get("playcount") if lastfm_result else None,
                            deezer_result.get("rank") if deezer_result else None,
                            deezer_result.get("deezer_id") if deezer_result else None,
                            deezer_checked_at,
                            lastfm_checked_at,
                            mb_result.get("rating") if mb_result else None,
                            mb_result.get("rating_count") if mb_result else None,
                            mb_checked_at,
                            track["id"],
                        ))
                        enriched += 1

                    if len(pending) >= WRITE_BATCH:
                        flush_pending()

                    if (enriched + skipped) % 100 == 0:
                        logger.info("Popularity: %d enriched, %d skipped so far", enriched, skipped)

                flush_pending()  # write any remaining tracks

            # --- Deezer top-up pass ---
            # Fill in tracks enriched without Deezer data (e.g. during a prior rate limit).
            deezer_topup = 0
            with db.get_db() as conn:
                missing_deezer = db.get_tracks_missing_deezer(conn, limit=batch_size)

            if missing_deezer:
                logger.info("Popularity: Deezer top-up — %d tracks missing Deezer data", len(missing_deezer))
                topup_pending: list[tuple] = []
                not_found_ids: list[int] = []
                topup_delay = DEEZER_DELAY

                for track in missing_deezer:
                    artist = track.get("artist", "")
                    title = track.get("title", "")
                    if not artist or not title:
                        continue

                    raw = await _lookup_deezer(client, artist, title)

                    if raw and raw.get("rate_limited"):
                        logger.info("Popularity: Deezer top-up rate limited — waiting 5s before retry")
                        await asyncio.sleep(5)
                        raw = await _lookup_deezer(client, artist, title)
                        if raw and raw.get("rate_limited"):
                            logger.warning("Popularity: Deezer still rate limited during top-up — slowing down")
                            topup_delay = DEEZER_SLOWDOWN_DELAY
                        else:
                            topup_delay = DEEZER_SLOWDOWN_DELAY

                    await asyncio.sleep(topup_delay)

                    if raw is None:
                        continue  # network/API error — retry next batch, don't mark

                    if raw.get("not_found"):
                        not_found_ids.append(track["id"])  # confirmed absent, retry tomorrow
                        continue

                    # Reblend with existing Last.fm + MusicBrainz data
                    lastfm_result = None
                    if track.get("lastfm_listeners") is not None:
                        lastfm_result = {
                            "listeners": track["lastfm_listeners"],
                            "playcount": track.get("lastfm_playcount"),
                        }
                    mb_result = None
                    if track.get("musicbrainz_rating") is not None:
                        mb_result = {
                            "rating": track["musicbrainz_rating"],
                            "rating_count": track.get("musicbrainz_rating_count", 0),
                        }

                    new_popularity = _blend_scores(raw, lastfm_result, track.get("track_number"), mb_result)
                    topup_pending.append((
                        new_popularity, raw["rank"], raw.get("deezer_id"), time.time(), track["id"]
                    ))
                    deezer_topup += 1

                    if len(topup_pending) >= WRITE_BATCH:
                        with db.get_db() as conn:
                            db.update_deezer_popularity(conn, topup_pending)
                        topup_pending.clear()

                if not_found_ids:
                    with db.get_db() as conn:
                        db.update_deezer_not_found(conn, not_found_ids)

                if topup_pending:
                    with db.get_db() as conn:
                        db.update_deezer_popularity(conn, topup_pending)

                logger.info("Popularity: Deezer top-up done — %d tracks updated", deezer_topup)

            # --- Last.fm top-up pass ---
            # Fill in tracks that are missing Last.fm data (e.g. enriched when Last.fm was down,
            # or newly added tracks that got Deezer data but no Last.fm yet).
            lastfm_topup = 0
            if has_lastfm:
                with db.get_db() as conn:
                    missing_lastfm = db.get_tracks_missing_lastfm(conn, limit=batch_size)

                if missing_lastfm:
                    logger.info("Popularity: Last.fm top-up — %d tracks missing Last.fm data", len(missing_lastfm))
                    lastfm_topup_pending: list[tuple] = []
                    lastfm_not_found_ids: list[int] = []

                    for track in missing_lastfm:
                        artist = track.get("artist", "")
                        title = track.get("title", "")
                        if not artist or not title:
                            continue

                        raw_lfm = await _lookup_lastfm(client, artist, title)
                        await asyncio.sleep(LASTFM_DELAY)

                        if raw_lfm is None:
                            continue  # network/API error — retry next batch, don't mark

                        if raw_lfm.get("not_found"):
                            lastfm_not_found_ids.append(track["id"])  # confirmed absent, retry tomorrow
                            continue

                        lastfm_result = raw_lfm
                        # Reblend with existing Deezer + MusicBrainz data already stored in DB
                        deezer_result = None
                        if track.get("deezer_rank") is not None:
                            deezer_result = {"rank": track["deezer_rank"]}
                        mb_result = None
                        if track.get("musicbrainz_rating") is not None:
                            mb_result = {
                                "rating": track["musicbrainz_rating"],
                                "rating_count": track.get("musicbrainz_rating_count", 0),
                            }

                        new_popularity = _blend_scores(deezer_result, lastfm_result, track.get("track_number"), mb_result)
                        lastfm_topup_pending.append((
                            new_popularity,
                            lastfm_result.get("listeners"),
                            lastfm_result.get("playcount"),
                            time.time(),
                            track["id"],
                        ))
                        lastfm_topup += 1

                        if len(lastfm_topup_pending) >= WRITE_BATCH:
                            with db.get_db() as conn:
                                db.update_lastfm_popularity(conn, lastfm_topup_pending)
                            lastfm_topup_pending.clear()

                    if lastfm_not_found_ids:
                        with db.get_db() as conn:
                            db.update_lastfm_not_found(conn, lastfm_not_found_ids)

                    if lastfm_topup_pending:
                        with db.get_db() as conn:
                            db.update_lastfm_popularity(conn, lastfm_topup_pending)

                    logger.info("Popularity: Last.fm top-up done — %d tracks updated", lastfm_topup)

            # --- MusicBrainz top-up pass ---
            # Fill in tracks that are missing MusicBrainz data.
            mb_topup = 0
            with db.get_db() as conn:
                missing_mb = db.get_tracks_missing_musicbrainz(conn, limit=batch_size)

            if missing_mb:
                logger.info("Popularity: MusicBrainz top-up — %d tracks missing MusicBrainz data", len(missing_mb))
                mb_topup_pending: list[tuple] = []
                mb_not_found_ids: list[int] = []

                for track in missing_mb:
                    artist = track.get("artist", "")
                    title = track.get("title", "")
                    if not artist or not title:
                        continue

                    raw_mb = await _lookup_musicbrainz(client, artist, title)

                    if raw_mb and raw_mb.get("rate_limited"):
                        logger.info("Popularity: MusicBrainz top-up rate limited — waiting 5s before retry")
                        await asyncio.sleep(5)
                        raw_mb = await _lookup_musicbrainz(client, artist, title)
                        if raw_mb and raw_mb.get("rate_limited"):
                            logger.warning("Popularity: MusicBrainz still rate limited during top-up — stopping")
                            break

                    await asyncio.sleep(MUSICBRAINZ_DELAY)

                    if raw_mb is None:
                        continue

                    if raw_mb.get("not_found"):
                        mb_not_found_ids.append(track["id"])
                        continue

                    # Reblend with existing Deezer + Last.fm data
                    deezer_result = None
                    if track.get("deezer_rank") is not None:
                        deezer_result = {"rank": track["deezer_rank"]}
                    lastfm_result = None
                    if track.get("lastfm_listeners") is not None:
                        lastfm_result = {
                            "listeners": track["lastfm_listeners"],
                            "playcount": track.get("lastfm_playcount"),
                        }

                    new_popularity = _blend_scores(deezer_result, lastfm_result, track.get("track_number"), raw_mb)
                    mb_topup_pending.append((
                        new_popularity,
                        raw_mb["rating"],
                        raw_mb.get("rating_count", 0),
                        time.time(),
                        track["id"],
                    ))
                    mb_topup += 1

                    if len(mb_topup_pending) >= WRITE_BATCH:
                        with db.get_db() as conn:
                            db.update_musicbrainz_popularity(conn, mb_topup_pending)
                        mb_topup_pending.clear()

                if mb_not_found_ids:
                    with db.get_db() as conn:
                        db.update_musicbrainz_not_found(conn, mb_not_found_ids)

                if mb_topup_pending:
                    with db.get_db() as conn:
                        db.update_musicbrainz_popularity(conn, mb_topup_pending)

                logger.info("Popularity: MusicBrainz top-up done — %d tracks updated", mb_topup)

        # Check remaining
        with db.get_db() as conn:
            remaining = db.count_tracks_without_popularity(conn)
            missing_deezer_count = db.count_tracks_missing_deezer(conn)
            missing_lastfm_count = db.count_tracks_missing_lastfm(conn)
            missing_mb_count = db.count_tracks_missing_musicbrainz(conn)

        logger.info(
            "Popularity enrichment done: %d enriched, %d skipped, %d remaining, "
            "%d missing Deezer, %d missing Last.fm, %d missing MusicBrainz",
            enriched, skipped, remaining, missing_deezer_count, missing_lastfm_count, missing_mb_count
        )
        return {
            "enriched": enriched,
            "skipped": skipped,
            "total_remaining": remaining,
            "missing_deezer": missing_deezer_count,
            "missing_lastfm": missing_lastfm_count,
            "missing_musicbrainz": missing_mb_count,
        }
    finally:
        _enrichment_lock.release()
