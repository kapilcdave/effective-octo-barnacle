from __future__ import annotations

import logging

from apscheduler.schedulers.blocking import BlockingScheduler

import config
from db import db_session
import options_executor
import reporter
from runtime import get_logger
import scorer
import scraper
import tagger


def _logger() -> logging.Logger:
    return get_logger("tradingbot.main")


def _safe(job_name: str, fn):
    log = _logger()

    def wrapped():
        try:
            log.info("job start %s", job_name)
            fn()
            log.info("job done %s", job_name)
        except Exception as e:
            log.exception("job failed %s: %s", job_name, e)

    return wrapped


def main() -> None:
    log = _logger()
    with db_session():
        pass
    log.info("tradingbot starting scheduler")

    scheduler = BlockingScheduler()
    scheduler.add_job(_safe("scraper", scraper.run), "interval", minutes=15)
    scheduler.add_job(_safe("tagger", tagger.run), "interval", minutes=20)
    scheduler.add_job(_safe("scorer", scorer.run), "interval", hours=1)
    scheduler.add_job(_safe("reporter", reporter.run), "cron", day_of_week="sun", hour=20)

    # Options: reconcile fills often, enforce exit rules on a slower cadence.
    scheduler.add_job(_safe("options_sync", options_executor.sync), "interval", minutes=5)
    scheduler.add_job(_safe("options_monitor", options_executor.monitor), "interval", minutes=15)
    if config.OPTIONS_AUTO_TRADE:
        log.warning("OPTIONS_AUTO_TRADE=1: option entries will be submitted automatically")
        scheduler.add_job(_safe("options_auto_trade", options_executor.auto_trade), "interval", minutes=30)

    scheduler.start()


if __name__ == "__main__":
    main()
