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
import plex
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

    _scheduler.start()
    logger.info(
        "Scheduler started: scanning every %dh, enrichment every 2m",
        config.scan_interval_hours,
    )


def stop_scheduler():
    """Stop the scheduler."""
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Scheduler stopped")
