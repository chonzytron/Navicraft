"""
Local music file scanner.
Walks the music directory, reads ID3/Vorbis/FLAC tags via mutagen,
and populates the SQLite index. Supports incremental scanning by mtime.
"""

import os
import logging
import asyncio
from pathlib import Path
from typing import Optional, Callable
import mutagen
from mutagen.easyid3 import EasyID3
from mutagen.flac import FLAC
from mutagen.oggvorbis import OggVorbis
from mutagen.oggopus import OggOpus
from mutagen.mp4 import MP4
from mutagen.apev2 import APEv2
from mutagen.id3 import ID3

from config import config
import database as db

logger = logging.getLogger(__name__)

# Progress callback type: (phase, current, total, message)
ProgressCallback = Optional[Callable[[str, int, int, str], None]]


def _safe_first(tags: dict, key: str, default=None):
    """Safely get the first value from a tag list."""
    val = tags.get(key)
    if val is None:
        return default
    if isinstance(val, list):
        return val[0] if val else default
    return val


def _safe_int(val, default=None) -> Optional[int]:
    if val is None:
        return default
    try:
        # Handle "3/12" format for track numbers
        s = str(val).split("/")[0].strip()
        return int(s) if s else default
    except (ValueError, TypeError):
        return default


def _safe_float(val, default=None) -> Optional[float]:
    if val is None:
        return default
    try:
        return float(str(val).strip())
    except (ValueError, TypeError):
        return default


def _extract_metadata(file_path: str) -> Optional[dict]:
    """
    Extract rich metadata from a music file using mutagen.
    Returns a dict ready for database insertion, or None on failure.
    """
    try:
        audio = mutagen.File(file_path, easy=True)
        if audio is None:
            return None

        stat = os.stat(file_path)
        ext = Path(file_path).suffix.lower()

        # Base metadata from easy tags
        meta = {
            "file_path": file_path,
            "file_mtime": stat.st_mtime,
            "file_size": stat.st_size,
            "format": ext.lstrip("."),
            "bitrate": None,
            "sample_rate": None,
            "channels": None,
            "title": _safe_first(audio, "title"),
            "artist": _safe_first(audio, "artist"),
            "album_artist": _safe_first(audio, "albumartist") or _safe_first(audio, "album_artist"),
            "album": _safe_first(audio, "album"),
            "genre": _safe_first(audio, "genre"),
            "year": None,
            "track_number": None,
            "disc_number": None,
            "duration": None,
            "bpm": None,
            "composer": _safe_first(audio, "composer"),
            "comment": None,
            "label": None,
            "mood": None,
        }

        # Duration and audio info from the info object
        if hasattr(audio, "info") and audio.info:
            info = audio.info
            meta["duration"] = getattr(info, "length", None)
            meta["bitrate"] = getattr(info, "bitrate", None)
            meta["sample_rate"] = getattr(info, "sample_rate", None)
            meta["channels"] = getattr(info, "channels", None)

        # Year — try 'date', then 'year', then 'originaldate'
        for date_key in ("date", "year", "originaldate", "originalyear"):
            date_val = _safe_first(audio, date_key)
            if date_val:
                meta["year"] = _safe_int(str(date_val)[:4])
                if meta["year"]:
                    break

        # Track and disc numbers
        meta["track_number"] = _safe_int(_safe_first(audio, "tracknumber"))
        meta["disc_number"] = _safe_int(_safe_first(audio, "discnumber"))

        # Try to get extended tags from the raw file (BPM, mood, comment, label)
        _extract_extended_tags(file_path, ext, meta)

        # Fallback: use filename as title if no tag
        if not meta["title"]:
            meta["title"] = Path(file_path).stem

        return meta

    except Exception as e:
        logger.debug("Failed to read %s: %s", file_path, e)
        return None


def _extract_extended_tags(file_path: str, ext: str, meta: dict):
    """Extract BPM, mood, comment, and label from format-specific raw tags."""
    try:
        if ext == ".mp3":
            try:
                tags = ID3(file_path)
            except Exception:
                return
            # BPM
            bpm = tags.get("TBPM")
            if bpm:
                meta["bpm"] = _safe_float(bpm.text[0] if bpm.text else None)
            # Mood — often stored in TXXX:mood or TMOO
            for key in tags:
                kl = key.lower()
                if "mood" in kl:
                    frame = tags[key]
                    val = frame.text[0] if hasattr(frame, 'text') and frame.text else str(frame)
                    meta["mood"] = val
                    break
            # Comment
            comm = tags.get("COMM::eng") or tags.get("COMM::'eng'")
            if comm:
                meta["comment"] = str(comm)
            # Label / publisher
            tpub = tags.get("TPUB")
            if tpub and tpub.text:
                meta["label"] = tpub.text[0]

        elif ext == ".flac":
            try:
                audio = FLAC(file_path)
            except Exception:
                return
            meta["bpm"] = _safe_float(_safe_first(audio, "bpm"))
            meta["mood"] = _safe_first(audio, "mood")
            meta["comment"] = _safe_first(audio, "comment") or _safe_first(audio, "description")
            meta["label"] = _safe_first(audio, "label") or _safe_first(audio, "publisher")

        elif ext in (".ogg",):
            try:
                audio = OggVorbis(file_path)
            except Exception:
                return
            meta["bpm"] = _safe_float(_safe_first(audio, "bpm"))
            meta["mood"] = _safe_first(audio, "mood")
            meta["comment"] = _safe_first(audio, "comment")
            meta["label"] = _safe_first(audio, "label") or _safe_first(audio, "organization")

        elif ext == ".opus":
            try:
                audio = OggOpus(file_path)
            except Exception:
                return
            meta["bpm"] = _safe_float(_safe_first(audio, "bpm"))
            meta["mood"] = _safe_first(audio, "mood")
            meta["comment"] = _safe_first(audio, "comment")

        elif ext in (".m4a", ".aac", ".mp4"):
            try:
                audio = MP4(file_path)
            except Exception:
                return
            tags = audio.tags or {}
            # MP4 uses '\xa9' prefix for standard tags and 'tmpo' for BPM
            tmpo = tags.get("tmpo")
            if tmpo:
                meta["bpm"] = _safe_float(tmpo[0])
            comment = tags.get("\xa9cmt")
            if comment:
                meta["comment"] = comment[0]

    except Exception as e:
        logger.debug("Extended tag extraction failed for %s: %s", file_path, e)


def _walk_music_dir() -> list[str]:
    """Walk the music directory and return all music file paths."""
    files = []
    extensions = config.extensions_set
    music_dir = config.music_dir

    for root, dirs, filenames in os.walk(music_dir):
        # Skip hidden directories
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fname in filenames:
            if Path(fname).suffix.lower() in extensions:
                files.append(os.path.join(root, fname))

    return files


async def scan_library(
    full_scan: bool = False,
    progress_cb: ProgressCallback = None,
) -> dict:
    """
    Scan the music library and update the SQLite index.
    Returns scan stats.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _scan_sync, full_scan, progress_cb)


def _scan_sync(full_scan: bool, progress_cb: ProgressCallback) -> dict:
    """Synchronous scan implementation."""
    with db.get_db() as conn:
        log_id = db.create_scan_log(conn)

    # Phase 1: Discover files
    if progress_cb:
        progress_cb("discovering", 0, 0, "Discovering music files...")

    all_files = _walk_music_dir()
    total = len(all_files)
    logger.info("Found %d music files in %s", total, config.music_dir)

    if progress_cb:
        progress_cb("discovering", total, total, f"Found {total} files")

    # Phase 2: Scan and index
    added = 0
    updated = 0
    scanned = 0
    errors = 0

    with db.get_db() as conn:
        indexed_paths = db.get_all_paths(conn) if not full_scan else set()

        for i, fpath in enumerate(all_files):
            try:
                stat = os.stat(fpath)

                # Incremental: skip if mtime unchanged
                if not full_scan and fpath in indexed_paths:
                    stored_mtime = db.get_track_mtime(conn, fpath)
                    if stored_mtime and abs(stat.st_mtime - stored_mtime) < 1:
                        scanned += 1
                        if progress_cb and i % 200 == 0:
                            progress_cb("scanning", i + 1, total, f"Scanning... ({added} new, {updated} updated)")
                        continue

                meta = _extract_metadata(fpath)
                if meta:
                    is_new = fpath not in indexed_paths
                    db.upsert_track(conn, meta)
                    if is_new:
                        added += 1
                    else:
                        updated += 1

                scanned += 1

            except Exception as e:
                errors += 1
                logger.debug("Error scanning %s: %s", fpath, e)

            if progress_cb and i % 100 == 0:
                progress_cb("scanning", i + 1, total, f"Scanning... ({added} new, {updated} updated)")

        # Phase 3: Remove deleted files
        if progress_cb:
            progress_cb("cleanup", 0, 0, "Removing deleted tracks...")

        current_paths = set(all_files)
        all_indexed = db.get_all_paths(conn)
        removed_paths = all_indexed - current_paths
        removed = len(removed_paths)
        if removed_paths:
            db.remove_tracks(conn, list(removed_paths))
            logger.info("Removed %d deleted tracks from index", removed)

        # Update scan log
        db.update_scan_log(conn, log_id,
            finished_at="datetime('now')",
            tracks_scanned=scanned,
            tracks_added=added,
            tracks_updated=updated,
            tracks_removed=removed,
            status="complete",
        )

    stats = {
        "total_files": total,
        "scanned": scanned,
        "added": added,
        "updated": updated,
        "removed": removed,
        "errors": errors,
        "status": "complete",
    }

    if progress_cb:
        progress_cb("complete", total, total,
            f"Done: {added} added, {updated} updated, {removed} removed")

    logger.info("Scan complete: %s", stats)
    return stats
