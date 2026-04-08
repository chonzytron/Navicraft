"""
Essentia-based mood and theme tag scanner using MTG-Jamendo models.

Analyzes audio files locally to assign mood tags (happy, sad, energetic, etc.)
and theme tags (film, nature, party, etc.). Supplements Essentia analysis with:
- File metadata mood tags (ID3/Vorbis mood field)
- Last.fm user-applied tags (via track.getTopTags API)
- MusicBrainz folksonomy tags (via tag lookup API)

All tags are categorized into mood (emotional state/energy) vs theme
(context/setting/use-case) buckets and stored separately.
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

import httpx

import database as db
from config import config

logger = logging.getLogger("navicraft.mood_scanner")

# Lock to prevent concurrent mood scans
_mood_scan_lock = asyncio.Lock()

# Progress state
_mood_scan_progress = {
    "running": False,
    "current": 0,
    "total": 0,
    "message": "Idle",
}

# --- Model configuration ---

MODEL_DIR_NAME = "essentia_models"
EMBEDDING_MODEL = "discogs-effnet-bs64-1.pb"
MOOD_MODEL = "mtg_jamendo_moodtheme-discogs-effnet-1.pb"
MOOD_METADATA = "mtg_jamendo_moodtheme-discogs-effnet-1.json"

MODEL_URLS = {
    EMBEDDING_MODEL: "https://essentia.upf.edu/models/feature-extractors/discogs-effnet/discogs-effnet-bs64-1.pb",
    MOOD_MODEL: "https://essentia.upf.edu/models/classification-heads/mtg_jamendo_moodtheme/mtg_jamendo_moodtheme-discogs-effnet-1.pb",
    MOOD_METADATA: "https://essentia.upf.edu/models/classification-heads/mtg_jamendo_moodtheme/mtg_jamendo_moodtheme-discogs-effnet-1.json",
}

# Confidence threshold for including an Essentia-predicted tag
ESSENTIA_THRESHOLD = 0.1

# --- Tag categorization ---
# MTG-Jamendo mood/theme predictions classified into mood vs theme buckets.

MOOD_CATEGORY = {
    "calm", "cool", "dark", "deep", "dramatic", "dream", "emotional",
    "energetic", "epic", "fun", "funny", "groovy", "happy", "heavy",
    "hopeful", "inspiring", "love", "meditative", "melancholic", "melodic",
    "motivational", "positive", "powerful", "relaxing", "romantic", "sad",
    "sexy", "slow", "soft", "upbeat", "uplifting",
}

THEME_CATEGORY = {
    "action", "adventure", "advertising", "background", "ballad", "children",
    "christmas", "commercial", "corporate", "documentary", "drama",
    "electronic", "fast", "film", "game", "holiday", "movie", "nature",
    "party", "retro", "soundscape", "space", "sport", "summer", "trailer",
    "travel",
}

# Common Last.fm / MusicBrainz user tags mapped to our mood/theme categories.
# Tags not in these maps are attempted via the Essentia category sets above,
# and if still unmatched, skipped.
LASTFM_MOOD_ALIASES = {
    "chill": "calm", "chillout": "calm", "mellow": "calm", "peaceful": "calm",
    "aggressive": "heavy", "angry": "heavy", "intense": "heavy",
    "beautiful": "melodic", "atmospheric": "deep", "ambient": "meditative",
    "uplifting": "uplifting", "euphoric": "uplifting", "joyful": "happy",
    "cheerful": "happy", "danceable": "groovy", "groovy": "groovy",
    "melancholy": "melancholic", "bittersweet": "melancholic",
    "nostalgic": "melancholic", "sentimental": "emotional",
    "dreamy": "dream", "ethereal": "dream", "hypnotic": "deep",
    "sensual": "sexy", "seductive": "sexy",
    "depressing": "sad", "somber": "sad", "gloomy": "dark",
    "haunting": "dark", "eerie": "dark", "sinister": "dark",
    "tender": "soft", "gentle": "soft", "delicate": "soft",
    "triumphant": "epic", "anthemic": "epic", "majestic": "epic",
    "playful": "fun", "quirky": "fun", "witty": "funny",
    "passionate": "emotional", "heartfelt": "emotional",
    "fierce": "powerful", "bold": "powerful", "driving": "energetic",
    "lively": "energetic", "vibrant": "energetic", "dynamic": "energetic",
}

LASTFM_THEME_ALIASES = {
    "workout": "sport", "exercise": "sport", "gym": "sport", "running": "sport",
    "driving": "travel", "road trip": "travel", "journey": "travel",
    "study": "background", "focus": "background", "concentration": "background",
    "sleep": "background", "night": "background",
    "cinematic": "film", "soundtrack": "film", "ost": "film", "score": "film",
    "gaming": "game", "video game": "game",
    "festive": "holiday", "xmas": "christmas", "winter": "christmas",
    "beach": "summer", "tropical": "summer", "sunny": "summer",
    "urban": "corporate", "city": "corporate",
    "vintage": "retro", "oldies": "retro", "throwback": "retro",
    "meditation": "nature", "yoga": "nature", "zen": "nature",
    "dance": "party", "club": "party", "rave": "party",
    "kids": "children", "lullaby": "children",
    "wedding": "romantic", "love songs": "love",
    "sci-fi": "space", "futuristic": "space", "cosmic": "space",
}

# Last.fm API settings
LASTFM_BASE = "https://ws.audioscrobbler.com/2.0/"
LASTFM_DELAY = 0.25
LASTFM_TAG_MIN_COUNT = 10  # Minimum tag count to consider a Last.fm tag relevant

# MusicBrainz API settings
MUSICBRAINZ_BASE = "https://musicbrainz.org/ws/2"
MUSICBRAINZ_DELAY = 1.1
MUSICBRAINZ_USER_AGENT = "NaviCraft/2.0 (https://github.com/chonzytron/navicraft)"


def _get_model_dir() -> Path:
    data_dir = Path(os.getenv("DATA_DIR", os.path.dirname(config.db_path)))
    return data_dir / MODEL_DIR_NAME


def _models_available() -> bool:
    model_dir = _get_model_dir()
    return all((model_dir / name).exists() for name in [EMBEDDING_MODEL, MOOD_MODEL])


async def download_models() -> bool:
    """Download Essentia models if not present. Returns True if models are ready."""
    model_dir = _get_model_dir()
    model_dir.mkdir(parents=True, exist_ok=True)

    if _models_available():
        return True

    for filename, url in MODEL_URLS.items():
        filepath = model_dir / filename
        if filepath.exists():
            continue
        logger.info("Downloading Essentia model: %s", filename)
        _mood_scan_progress.update(message=f"Downloading model: {filename}...")
        try:
            async with httpx.AsyncClient(timeout=600, follow_redirects=True) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                filepath.write_bytes(resp.content)
                logger.info("Downloaded %s (%d bytes)", filename, len(resp.content))
        except Exception as e:
            logger.error("Failed to download %s: %s", filename, e)
            if filepath.exists():
                filepath.unlink()
            return False

    return True


def _load_labels(model_dir: Path) -> list[str]:
    """Load tag labels from metadata JSON, with hardcoded fallback."""
    metadata_path = model_dir / MOOD_METADATA
    if metadata_path.exists():
        try:
            with open(metadata_path) as f:
                meta = json.load(f)
            if "classes" in meta:
                return meta["classes"]
        except Exception:
            pass
    # Fallback: combined mood + theme labels from MTG-Jamendo
    return sorted(MOOD_CATEGORY | THEME_CATEGORY)


def _categorize_tag(tag: str) -> tuple[Optional[str], Optional[str]]:
    """Categorize a tag into mood or theme. Returns (category, canonical_tag).
    category is 'mood' or 'theme', or None if unrecognized."""
    tag_lower = tag.lower().strip()
    if tag_lower in MOOD_CATEGORY:
        return "mood", tag_lower
    if tag_lower in THEME_CATEGORY:
        return "theme", tag_lower
    if tag_lower in LASTFM_MOOD_ALIASES:
        return "mood", LASTFM_MOOD_ALIASES[tag_lower]
    if tag_lower in LASTFM_THEME_ALIASES:
        return "theme", LASTFM_THEME_ALIASES[tag_lower]
    return None, None


def _categorize_file_mood(mood_str: str) -> tuple[set, set]:
    """Parse the existing file-tag mood field and categorize into mood/theme sets."""
    mood_tags = set()
    theme_tags = set()
    if not mood_str:
        return mood_tags, theme_tags
    # Mood field may be semicolon, comma, or slash separated
    for sep in [";", ",", "/"]:
        if sep in mood_str:
            parts = [p.strip() for p in mood_str.split(sep) if p.strip()]
            break
    else:
        parts = [mood_str.strip()]

    for part in parts:
        cat, canonical = _categorize_tag(part)
        if cat == "mood":
            mood_tags.add(canonical)
        elif cat == "theme":
            theme_tags.add(canonical)
    return mood_tags, theme_tags


# --- Essentia audio analysis ---

def _analyze_track_essentia(
    file_path: str, embedding_model, mood_model, labels: list[str]
) -> tuple[set, set]:
    """Analyze a single track with Essentia. Returns (mood_tags, theme_tags)."""
    mood_tags = set()
    theme_tags = set()
    try:
        from essentia.standard import MonoLoader

        audio = MonoLoader(filename=file_path, sampleRate=16000, resampleQuality=4)()
        if len(audio) < 16000:  # less than 1 second
            return mood_tags, theme_tags

        embeddings = embedding_model(audio)
        predictions = mood_model(embeddings)

        # Average predictions across time frames
        avg_predictions = predictions.mean(axis=0)

        for i, score in enumerate(avg_predictions):
            if i < len(labels) and score >= ESSENTIA_THRESHOLD:
                tag = labels[i]
                # Strip common prefixes from model labels (e.g. "mood/theme---happy" -> "happy")
                if "---" in tag:
                    tag = tag.split("---")[-1]
                cat, canonical = _categorize_tag(tag)
                if cat == "mood":
                    mood_tags.add(canonical)
                elif cat == "theme":
                    theme_tags.add(canonical)
    except Exception as e:
        logger.debug("Essentia analysis failed for %s: %s", file_path, e)

    return mood_tags, theme_tags


def _scan_batch_essentia_sync(
    tracks: list[dict], model_dir: Path, labels: list[str], progress_cb=None
) -> dict[int, tuple[set, set]]:
    """Synchronously run Essentia on a batch. Returns {track_id: (mood_set, theme_set)}."""
    from essentia.standard import TensorflowPredictEffnetDiscogs, TensorflowPredict2D

    embedding_model = TensorflowPredictEffnetDiscogs(
        graphFilename=str(model_dir / EMBEDDING_MODEL),
        output="PartitionedCall:1",
    )
    mood_model = TensorflowPredict2D(
        graphFilename=str(model_dir / MOOD_MODEL),
    )

    results = {}
    for i, track in enumerate(tracks):
        moods, themes = _analyze_track_essentia(
            track["file_path"], embedding_model, mood_model, labels
        )
        results[track["id"]] = (moods, themes)
        if progress_cb and (i + 1) % 5 == 0:
            progress_cb(i + 1, len(tracks))

    return results


# --- API tag lookups ---

async def _lookup_lastfm_tags(
    client: httpx.AsyncClient, artist: str, title: str
) -> tuple[set, set]:
    """Fetch top tags from Last.fm and categorize into mood/theme."""
    mood_tags = set()
    theme_tags = set()
    try:
        resp = await client.get(
            LASTFM_BASE,
            params={
                "method": "track.getTopTags",
                "api_key": config.lastfm_api_key,
                "artist": artist,
                "track": title,
                "format": "json",
            },
        )
        if resp.status_code == 429:
            return mood_tags, theme_tags
        resp.raise_for_status()
        data = resp.json()

        tags = data.get("toptags", {}).get("tag", [])
        for tag_obj in tags:
            count = int(tag_obj.get("count", 0))
            if count < LASTFM_TAG_MIN_COUNT:
                continue
            name = tag_obj.get("name", "").lower().strip()
            cat, canonical = _categorize_tag(name)
            if cat == "mood":
                mood_tags.add(canonical)
            elif cat == "theme":
                theme_tags.add(canonical)
    except Exception as e:
        logger.debug("Last.fm tag lookup failed for '%s - %s': %s", artist, title, e)

    return mood_tags, theme_tags


async def _lookup_musicbrainz_tags(
    client: httpx.AsyncClient, artist: str, title: str
) -> tuple[set, set]:
    """Fetch folksonomy tags from MusicBrainz and categorize into mood/theme."""
    mood_tags = set()
    theme_tags = set()
    try:
        # First, find the recording MBID
        query = f'recording:"{title}" AND artist:"{artist}"'
        resp = await client.get(
            f"{MUSICBRAINZ_BASE}/recording",
            params={"query": query, "limit": 3, "fmt": "json"},
            headers={"User-Agent": MUSICBRAINZ_USER_AGENT},
        )
        if resp.status_code in (429, 503):
            return mood_tags, theme_tags
        resp.raise_for_status()
        data = resp.json()

        recordings = data.get("recordings", [])
        if not recordings:
            return mood_tags, theme_tags

        # Match artist
        artist_lower = artist.lower()
        best = None
        for rec in recordings:
            for ac in rec.get("artist-credit", []):
                ac_name = (ac.get("name") or ac.get("artist", {}).get("name", "")).lower()
                if artist_lower in ac_name or ac_name in artist_lower:
                    best = rec
                    break
            if best:
                break
        if not best:
            best = recordings[0]

        mbid = best.get("id")
        if not mbid:
            return mood_tags, theme_tags

        await asyncio.sleep(MUSICBRAINZ_DELAY)

        # Fetch tags for this recording
        resp2 = await client.get(
            f"{MUSICBRAINZ_BASE}/recording/{mbid}",
            params={"inc": "tags", "fmt": "json"},
            headers={"User-Agent": MUSICBRAINZ_USER_AGENT},
        )
        if resp2.status_code in (429, 503):
            return mood_tags, theme_tags
        resp2.raise_for_status()
        tag_data = resp2.json()

        for tag_obj in tag_data.get("tags", []):
            count = tag_obj.get("count", 0)
            if count < 1:
                continue
            name = tag_obj.get("name", "").lower().strip()
            cat, canonical = _categorize_tag(name)
            if cat == "mood":
                mood_tags.add(canonical)
            elif cat == "theme":
                theme_tags.add(canonical)
    except Exception as e:
        logger.debug("MusicBrainz tag lookup failed for '%s - %s': %s", artist, title, e)

    return mood_tags, theme_tags


# --- Main scan pipeline ---

async def scan_mood_tags(batch_size: int | None = None) -> dict:
    """
    Scan tracks for mood and theme tags.

    Pipeline per track:
    1. Essentia audio analysis (MTG-Jamendo mood/theme model)
    2. File metadata mood field
    3. Last.fm top tags (if API key configured)
    4. MusicBrainz folksonomy tags
    5. Merge all sources, categorize into mood vs theme, store in DB.
    """
    if _mood_scan_lock.locked():
        return {"status": "already_running"}

    if batch_size is None:
        batch_size = config.mood_scan_batch_size

    async with _mood_scan_lock:
        _mood_scan_progress.update(running=True, current=0, total=0, message="Starting mood scan...")

        try:
            # Check essentia availability
            try:
                import essentia  # noqa: F401
                import essentia.standard  # noqa: F401
                has_essentia = True
            except Exception as exc:
                has_essentia = False
                logger.warning(
                    "essentia-tensorflow not available — skipping audio analysis, using API tags only. "
                    "Install with: pip install --pre essentia-tensorflow==2.1b6.dev1389 — error: %s", exc
                )

            # Download models if needed
            if has_essentia:
                if not await download_models():
                    logger.warning("Essentia model download failed — skipping audio analysis")
                    has_essentia = False

            model_dir = _get_model_dir() if has_essentia else None
            labels = _load_labels(model_dir) if has_essentia else []

            # Get tracks that need mood scanning
            with db.get_db() as conn:
                tracks = db.get_tracks_without_mood_scan(conn, limit=batch_size)

            if not tracks:
                _mood_scan_progress.update(running=False, current=0, total=0, message="All tracks scanned")
                return {"status": "complete", "scanned": 0, "tagged": 0}

            total = len(tracks)
            _mood_scan_progress.update(total=total, message=f"Analyzing 0/{total} tracks...")

            # Phase 1: Essentia audio analysis (CPU-heavy, run in thread pool)
            essentia_results: dict[int, tuple[set, set]] = {}
            if has_essentia:
                def progress_cb(current, batch_total):
                    _mood_scan_progress.update(
                        current=current,
                        message=f"Essentia: {current}/{batch_total} tracks...",
                    )

                loop = asyncio.get_event_loop()
                essentia_results = await loop.run_in_executor(
                    None,
                    _scan_batch_essentia_sync,
                    tracks, model_dir, labels, progress_cb,
                )

            # Phase 2: File tag moods + API tag lookups
            has_lastfm = bool(config.lastfm_api_key)
            now = time.time()
            results: list[tuple] = []  # (mood_tags, theme_tags, essentia_scanned_at, track_id)
            tagged = 0

            async with httpx.AsyncClient(
                timeout=15, headers={"Accept": "application/json"}
            ) as client:
                for i, track in enumerate(tracks):
                    track_id = track["id"]
                    artist = track.get("artist", "")
                    title = track.get("title", "")

                    # Start with Essentia results
                    mood_set, theme_set = essentia_results.get(track_id, (set(), set()))

                    # Add file tag mood
                    file_moods, file_themes = _categorize_file_mood(track.get("mood") or "")
                    mood_set |= file_moods
                    theme_set |= file_themes

                    # Last.fm tags
                    if has_lastfm and artist and title:
                        lfm_moods, lfm_themes = await _lookup_lastfm_tags(client, artist, title)
                        mood_set |= lfm_moods
                        theme_set |= lfm_themes
                        await asyncio.sleep(LASTFM_DELAY)

                    # MusicBrainz tags
                    if artist and title:
                        mb_moods, mb_themes = await _lookup_musicbrainz_tags(client, artist, title)
                        mood_set |= mb_moods
                        theme_set |= mb_themes
                        await asyncio.sleep(MUSICBRAINZ_DELAY)

                    mood_str = ", ".join(sorted(mood_set)) if mood_set else None
                    theme_str = ", ".join(sorted(theme_set)) if theme_set else None

                    results.append((mood_str, theme_str, now, track_id))
                    if mood_str or theme_str:
                        tagged += 1

                    if (i + 1) % 5 == 0 or i == total - 1:
                        _mood_scan_progress.update(
                            current=i + 1,
                            message=f"Enriching tags: {i + 1}/{total} tracks...",
                        )

                    # Batch write every 50 tracks
                    if len(results) >= 50:
                        with db.get_db() as conn:
                            db.bulk_update_mood_tags(conn, results)
                        results.clear()

            # Flush remaining
            if results:
                with db.get_db() as conn:
                    db.bulk_update_mood_tags(conn, results)

            _mood_scan_progress.update(
                running=False,
                current=total,
                message=f"Done: {tagged}/{total} tracks tagged",
            )

            logger.info("Mood scan complete: %d/%d tracks tagged", tagged, total)
            return {"status": "ok", "scanned": total, "tagged": tagged}

        except Exception as e:
            logger.exception("Mood scan failed")
            _mood_scan_progress.update(running=False, message=f"Error: {e}")
            return {"status": "error", "message": str(e)}


async def reset_mood_tags() -> int:
    """Reset all mood/theme tags so they can be re-scanned."""
    with db.get_db() as conn:
        return db.reset_mood_tags(conn)


def get_progress() -> dict:
    """Return current mood scan progress."""
    return dict(_mood_scan_progress)
