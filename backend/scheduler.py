"""
Background scheduler for periodic library scans and popularity enrichment.
Uses APScheduler with asyncio integration.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.date import DateTrigger
from config import config
import scanner
import navidrome
import plex
import popularity
import mood_scanner
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


async def _scheduled_mood_scan():
    """Process X tracks for mood/theme tags, then schedule the next run Y hours later.

    Uses a one-shot DateTrigger instead of IntervalTrigger so the Y-hour wait
    starts *after* the batch finishes, not from when it was scheduled.
    """
    if not config.mood_scan_enabled:
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
        # Always reschedule — Y hours from NOW (after batch completed)
        _reschedule_mood_scan()


def _reschedule_mood_scan():
    """Schedule the next mood scan run Y hours from now."""
    global _scheduler
    if not _scheduler or not _scheduler.running:
        return
    next_run = datetime.now() + timedelta(hours=config.mood_scan_interval_hours)
    try:
        _scheduler.add_job(
            _scheduled_mood_scan,
            trigger=DateTrigger(run_date=next_run),
            id="mood_scan",
            name="Mood tag scan",
            replace_existing=True,
        )
        logger.info("Mood scan: next run scheduled at %s (%dh from now)",
                     next_run.strftime("%H:%M"), config.mood_scan_interval_hours)
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

    # Mood / theme tag scanning — process X tracks, then wait Y hours, repeat.
    # Always registered (checks config.mood_scan_enabled at runtime) so that
    # enabling via the UI Settings takes effect without a container restart.
    # First run triggers after 30s; subsequent runs scheduled Y hours after
    # each batch completes (not Y hours from schedule start).
    _scheduler.add_job(
        _scheduled_mood_scan,
        trigger=DateTrigger(run_date=datetime.now() + timedelta(seconds=30)),
        id="mood_scan",
        name="Mood tag scan",
        replace_existing=True,
    )

    _scheduler.start()
    logger.info(
        "Scheduler started: scanning every %dh, enrichment every 2m, mood scan %s (%d tracks every %dh)",
        config.scan_interval_hours,
        "enabled" if config.mood_scan_enabled else "disabled",
        config.mood_scan_batch_size,
        config.mood_scan_interval_hours,
    )


def stop_scheduler():
    """Stop the scheduler."""
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Scheduler stopped")
