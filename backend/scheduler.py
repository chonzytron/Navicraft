"""
Background scheduler for periodic library scans.
Uses APScheduler with asyncio integration.
"""

import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from config import config
import scanner
import navidrome

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


def start_scheduler():
    """Start the background scheduler."""
    global _scheduler
    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(
        _scheduled_scan,
        trigger=IntervalTrigger(hours=config.scan_interval_hours),
        id="library_scan",
        name="Library scan",
        replace_existing=True,
    )
    _scheduler.start()
    logger.info("Scheduler started: scanning every %d hours", config.scan_interval_hours)


def stop_scheduler():
    """Stop the scheduler."""
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Scheduler stopped")
