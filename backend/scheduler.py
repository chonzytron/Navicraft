"""
Background scheduler for periodic library scans and popularity enrichment.
Uses APScheduler with asyncio integration.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.date import DateTrigger
from config import config
import scanner
import navidrome
import plex
import popularity
import mood_scanner
import playlist_watcher
import database as db

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler = None


async def _scheduled_scan():
    """Run an incremental scan + Navidrome ID sync."""
    logger.info("Scheduled scan starting...")
    try:
        stats = await scanner.scan_library(full_scan=False)
        logger.info("Scheduled scan complete: %s", stats)

        # Sync media server IDs if any changes
        if stats.get("added", 0) > 0 or stats.get("updated", 0) > 0:
            if config.navidrome_url and config.navidrome_password:
                try:
                    await navidrome.sync_navidrome_ids()
                except Exception:
                    logger.exception("Navidrome ID sync failed during scheduled scan")
            if config.plex_url and config.plex_token:
                try:
                    await plex.sync_plex_ids()
                except Exception:
                    logger.exception("Plex ID sync failed during scheduled scan")
    except Exception:
        logger.exception("Scheduled scan failed")


async def _scheduled_enrichment():
    """
    Continuously enrich tracks with popularity data.
    Runs every 2 minutes, processing 500 tracks per batch.
    Covers three cases:
    - New tracks needing full enrichment (popularity IS NULL)
    - Tracks missing Deezer data (added when Deezer was rate-limited)
    - Tracks missing Last.fm data (added when Last.fm was unavailable)
    """
    try:
        with db.get_db() as conn:
            remaining = db.count_tracks_without_popularity(conn)
            missing_deezer = db.count_tracks_missing_deezer(conn)
            missing_lastfm = db.count_tracks_missing_lastfm(conn)

        if remaining == 0 and missing_deezer == 0 and missing_lastfm == 0:
            return

        logger.info(
            "Enrichment job: %d unscored, %d missing Deezer, %d missing Last.fm",
            remaining, missing_deezer, missing_lastfm,
        )
        result = await popularity.enrich_popularity(batch_size=500)
        logger.info("Enrichment job done: %s", result)
    except Exception:
        logger.exception("Scheduled enrichment failed")


def _is_in_mood_window() -> bool:
    """Check if current time (in configured timezone) is within the mood scan window."""
    from_h = config.mood_scan_from_hour
    to_h = config.mood_scan_to_hour
    if from_h == to_h:
        return False  # same hour = window disabled
    try:
        tz = ZoneInfo(config.timezone)
    except Exception:
        tz = ZoneInfo("UTC")
    hour = datetime.now(tz).hour
    if from_h < to_h:
        return from_h <= hour < to_h
    else:
        # Wraps midnight, e.g. 22-06 means 22:00–05:59
        return hour >= from_h or hour < to_h


async def _scheduled_mood_scan():
    """Process a batch of tracks for mood/theme tags if within the configured time window.

    Uses a one-shot DateTrigger. Within the window, reschedules 30s after batch
    completion for the next batch. Outside the window, schedules for the next
    window start.
    """
    if not config.mood_scan_enabled:
        _reschedule_mood_scan()
        return
    if not _is_in_mood_window():
        logger.info("Mood scan: outside window (%02d:00–%02d:00 %s), sleeping",
                     config.mood_scan_from_hour, config.mood_scan_to_hour, config.timezone)
        _reschedule_mood_scan()
        return
    try:
        with db.get_db() as conn:
            remaining = db.count_tracks_without_mood_scan(conn)
        if remaining == 0:
            logger.info("Mood scan: all tracks already scanned")
            _reschedule_mood_scan()
            return

        logger.info("Mood scan job: %d tracks remaining, processing %d",
                     remaining, config.mood_scan_batch_size)
        result = await mood_scanner.scan_mood_tags(batch_size=config.mood_scan_batch_size)
        logger.info("Mood scan job done: %s", result)
    except Exception:
        logger.exception("Scheduled mood scan failed")
    finally:
        _reschedule_mood_scan()


def _reschedule_mood_scan():
    """Schedule the next mood scan based on the time window.

    If currently inside the window, run again in 30s.
    If outside, calculate seconds until the next window start.
    """
    global _scheduler
    if not _scheduler or not _scheduler.running:
        return
    if _is_in_mood_window():
        next_run = datetime.now() + timedelta(seconds=30)
    else:
        # Calculate delay until next window start
        try:
            tz = ZoneInfo(config.timezone)
        except Exception:
            tz = ZoneInfo("UTC")
        now = datetime.now(tz)
        from_h = config.mood_scan_from_hour
        target = now.replace(hour=from_h, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        delay_seconds = (target - now).total_seconds()
        next_run = datetime.now() + timedelta(seconds=delay_seconds)
        logger.info("Mood scan: next window at %02d:00 %s (%.1fh from now)",
                     from_h, config.timezone, delay_seconds / 3600)
    try:
        _scheduler.add_job(
            _scheduled_mood_scan,
            trigger=DateTrigger(run_date=next_run),
            id="mood_scan",
            name="Mood tag scan",
            replace_existing=True,
        )
    except Exception:
        logger.exception("Failed to reschedule mood scan")


def start_scheduler():
    """Start the background scheduler."""
    global _scheduler
    _scheduler = AsyncIOScheduler()

    # Library scan
    _scheduler.add_job(
        _scheduled_scan,
        trigger=IntervalTrigger(hours=config.scan_interval_hours),
        id="library_scan",
        name="Library scan",
        replace_existing=True,
    )

    # Popularity enrichment — runs every 2 minutes until all tracks are scored.
    # Each batch is 500 tracks. With the two-source pipeline (Deezer at ~10 req/s,
    # Last.fm at 5 req/s), this keeps enrichment running back-to-back with minimal gaps.
    _scheduler.add_job(
        _scheduled_enrichment,
        trigger=IntervalTrigger(minutes=2),
        id="popularity_enrichment",
        name="Popularity enrichment",
        replace_existing=True,
    )

    # Mood / theme tag scanning — runs batches within a configured time window.
    # Always registered (checks config.mood_scan_enabled at runtime) so that
    # enabling via the UI Settings takes effect without a container restart.
    # First run triggers after 30s; within the window, batches run with 30s gaps.
    _scheduler.add_job(
        _scheduled_mood_scan,
        trigger=DateTrigger(run_date=datetime.now() + timedelta(seconds=30)),
        id="mood_scan",
        name="Mood tag scan",
        replace_existing=True,
    )

    # Navidrome playlist watcher — polls for [navicraft, ...] playlists.
    # Always registered (checks config.navicraft_watcher_enabled at runtime)
    # so enabling via Settings takes effect without a container restart.
    _scheduler.add_job(
        playlist_watcher.check_navidrome_playlists,
        trigger=IntervalTrigger(seconds=config.navicraft_watcher_interval),
        id="navicraft_watcher",
        name="Navidrome playlist watcher",
        replace_existing=True,
    )

    _scheduler.start()
    logger.info(
        "Scheduler started: scanning every %dh, enrichment every 2m, mood scan %s (%d tracks, window %02d:00–%02d:00 %s), "
        "playlist watcher %s (every %ds)",
        config.scan_interval_hours,
        "enabled" if config.mood_scan_enabled else "disabled",
        config.mood_scan_batch_size,
        config.mood_scan_from_hour, config.mood_scan_to_hour,
        config.timezone,
        "enabled" if config.navicraft_watcher_enabled else "disabled",
        config.navicraft_watcher_interval,
    )


def stop_scheduler():
    """Stop the scheduler."""
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Scheduler stopped")
