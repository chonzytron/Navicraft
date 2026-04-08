"""
Essentia-based mood and theme tag scanner using MTG-Jamendo models.

Analyzes audio files locally to assign mood tags (happy, sad, energetic, etc.)
and theme tags (film, nature, party, etc.) using the MTG-Jamendo mood/theme
classification model. Only Essentia audio analysis is used as the tag source
to ensure a standardized, consistent vocabulary across the entire library.

Tags are categorized into mood (emotional state/energy) vs theme
(context/setting/use-case) buckets and stored separately.
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

# Suppress noisy TensorFlow C++ warnings (CUDA, network, etc.) before any TF import.
# These are irrelevant in CPU-only environments (Docker containers, most servers).
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")

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
    category is 'mood' or 'theme', or None if unrecognized.
    Only accepts tags from the canonical Essentia vocabulary."""
    tag_lower = tag.lower().strip()
    if tag_lower in MOOD_CATEGORY:
        return "mood", tag_lower
    if tag_lower in THEME_CATEGORY:
        return "theme", tag_lower
    return None, None


def _format_scored_tags(scores: dict[str, float]) -> str:
    """Format a {tag: confidence} dict as a comma-separated string sorted by confidence desc.
    Example: "happy:0.85, energetic:0.72, upbeat:0.31"
    """
    sorted_tags = sorted(scores.items(), key=lambda x: -x[1])
    return ", ".join(f"{tag}:{score}" for tag, score in sorted_tags)


# --- Essentia audio analysis ---

def _analyze_track_essentia(
    file_path: str, embedding_model, mood_model, labels: list[str]
) -> tuple[dict, dict]:
    """Analyze a single track with Essentia. Returns (mood_scores, theme_scores).
    Each dict maps canonical tag name to confidence score (0.0-1.0)."""
    mood_scores: dict[str, float] = {}
    theme_scores: dict[str, float] = {}
    try:
        from essentia.standard import MonoLoader

        audio = MonoLoader(filename=file_path, sampleRate=16000, resampleQuality=4)()
        if len(audio) < 16000:  # less than 1 second
            return mood_scores, theme_scores

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
                    mood_scores[canonical] = round(float(score), 3)
                elif cat == "theme":
                    theme_scores[canonical] = round(float(score), 3)
    except Exception as e:
        logger.debug("Essentia analysis failed for %s: %s", file_path, e)

    return mood_scores, theme_scores


def _scan_batch_essentia_sync(
    tracks: list[dict], model_dir: Path, labels: list[str], progress_cb=None
) -> dict[int, tuple[dict, dict]]:
    """Synchronously run Essentia on a batch. Returns {track_id: (mood_scores, theme_scores)}."""
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


# --- Main scan pipeline ---

async def scan_mood_tags(batch_size: int | None = None) -> dict:
    """
    Scan tracks for mood and theme tags using Essentia audio analysis only.

    Uses the MTG-Jamendo mood/theme model to classify tracks into a standardized
    vocabulary of 31 mood tags and 26 theme tags with confidence scores.
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
                import essentia
                essentia.log.infoActive = False
                essentia.log.warningActive = False
                import essentia.standard  # noqa: F401
                has_essentia = True
            except Exception as exc:
                has_essentia = False
                logger.warning(
                    "essentia-tensorflow not available — mood scanning requires it. "
                    "Install with: pip install --pre essentia-tensorflow==2.1b6.dev1389 — error: %s", exc
                )
                _mood_scan_progress.update(running=False, message="Essentia not available")
                return {"status": "error", "message": "essentia-tensorflow is required for mood scanning"}

            # Download models if needed
            if not await download_models():
                logger.warning("Essentia model download failed")
                _mood_scan_progress.update(running=False, message="Model download failed")
                return {"status": "error", "message": "Failed to download Essentia models"}

            model_dir = _get_model_dir()
            labels = _load_labels(model_dir)

            # Get tracks that need mood scanning
            with db.get_db() as conn:
                tracks = db.get_tracks_without_mood_scan(conn, limit=batch_size)

            if not tracks:
                _mood_scan_progress.update(running=False, current=0, total=0, message="All tracks scanned")
                return {"status": "complete", "scanned": 0, "tagged": 0}

            total = len(tracks)
            _mood_scan_progress.update(total=total, message=f"Analyzing 0/{total} tracks...")

            # Essentia audio analysis (CPU-heavy, run in thread pool)
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

            # Build DB update rows from Essentia results
            now = time.time()
            results: list[tuple] = []  # (mood_tags, theme_tags, essentia_scanned_at, track_id)
            tagged = 0

            for track in tracks:
                track_id = track["id"]
                mood_scores, theme_scores = essentia_results.get(track_id, ({}, {}))

                # Store as "tag:confidence" pairs sorted by confidence descending
                mood_str = _format_scored_tags(mood_scores) if mood_scores else None
                theme_str = _format_scored_tags(theme_scores) if theme_scores else None

                results.append((mood_str, theme_str, now, track_id))
                if mood_str or theme_str:
                    tagged += 1

            # Batch write to DB
            for i in range(0, len(results), 50):
                batch = results[i:i + 50]
                with db.get_db() as conn:
                    db.bulk_update_mood_tags(conn, batch)

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


# --- Continuous scanning mode ---

_continuous_mode = False
_continuous_task = None


def is_continuous() -> bool:
    """Return whether continuous scanning is active."""
    return _continuous_mode


async def start_continuous(batch_size: int):
    """Start continuous mood scanning — runs batches back-to-back until stopped."""
    global _continuous_mode, _continuous_task
    if _continuous_mode:
        return
    _continuous_mode = True
    _continuous_task = asyncio.create_task(_continuous_loop(batch_size))


async def stop_continuous():
    """Stop continuous mood scanning after the current batch finishes."""
    global _continuous_mode
    _continuous_mode = False


async def _continuous_loop(batch_size: int):
    """Run mood scan batches back-to-back until stopped or all tracks are done."""
    global _continuous_mode
    try:
        while _continuous_mode:
            # Wait if a batch is already running (e.g. triggered by scheduler)
            progress = get_progress()
            if progress.get("running"):
                await asyncio.sleep(3)
                continue

            with db.get_db() as conn:
                remaining = db.count_tracks_without_mood_scan(conn)
            if remaining == 0:
                logger.info("Continuous mood scan: all tracks scanned, stopping")
                break

            logger.info("Continuous mood scan: %d remaining, processing %d", remaining, batch_size)
            await scan_mood_tags(batch_size=batch_size)
            await asyncio.sleep(2)  # Brief pause between batches
    except Exception:
        logger.exception("Continuous mood scan loop failed")
    finally:
        _continuous_mode = False
