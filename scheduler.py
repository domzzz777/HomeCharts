import asyncio
import logging
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from scraper import run_scrape_all

logger = logging.getLogger(__name__)

_scheduler: Optional[AsyncIOScheduler] = None


def start_scheduler() -> AsyncIOScheduler:
    global _scheduler
    _scheduler = AsyncIOScheduler(timezone="UTC")

    # Daily at 06:00 UTC — run before European market hours
    _scheduler.add_job(
        run_scrape_all,
        CronTrigger(hour=6, minute=0),
        id="daily_scrape",
        name="Daily Batumi scrape",
        replace_existing=True,
    )

    # Also run a midday check at 13:00 UTC
    _scheduler.add_job(
        run_scrape_all,
        CronTrigger(hour=13, minute=0),
        id="midday_scrape",
        name="Midday Batumi scrape",
        replace_existing=True,
    )

    _scheduler.start()
    logger.info("Scheduler started — scrapes at 06:00 and 13:00 UTC daily")
    return _scheduler


def stop_scheduler() -> None:
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")
