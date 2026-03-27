"""
Background scheduler for periodic library scans and popularity enrichment.
Uses APScheduler with asyncio integration.
"""

import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from config import config
import scanner
import navidrome
import popularity
import database as db

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler = None


async def _scheduled_scan():
    """Run an incremental scan + Navidrome ID sync."""
    logger.info("Scheduled scan starting...")
    try:
        stats = await scanner.scan_library(full_scan=False)
        logger.info("Scheduled scan complete: %s", stats)

        # Sync Navidrome IDs if any changes
        if stats.get("added", 0) > 0 or stats.get("updated", 0) > 0:
            await navidrome.sync_navidrome_ids()
    except Exception:
        logger.exception("Scheduled scan failed")


async def _scheduled_enrichment():
    """
    Continuously enrich tracks with popularity data.
    Runs every 10 minutes, processing 500 tracks per batch.
    At ~1.1s per track (MusicBrainz rate limit), 500 tracks takes ~9 minutes,
    so this keeps the enrichment pipeline running back-to-back until all
    tracks are scored.
    """
    try:
        with db.get_db() as conn:
            remaining = db.count_tracks_without_popularity(conn)

        if remaining == 0:
            return

        logger.info("Enrichment job: %d tracks remaining", remaining)
        result = await popularity.enrich_popularity(batch_size=500)
        logger.info("Enrichment job done: %s", result)
    except Exception:
        logger.exception("Scheduled enrichment failed")


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

    # Popularity enrichment — runs every 10 minutes until all tracks are scored
    _scheduler.add_job(
        _scheduled_enrichment,
        trigger=IntervalTrigger(minutes=10),
        id="popularity_enrichment",
        name="Popularity enrichment",
        replace_existing=True,
    )

    _scheduler.start()
    logger.info(
        "Scheduler started: scanning every %dh, enrichment every 10m",
        config.scan_interval_hours,
    )


def stop_scheduler():
    """Stop the scheduler."""
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Scheduler stopped")
