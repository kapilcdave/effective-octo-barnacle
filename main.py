from __future__ import annotations

import logging

from apscheduler.schedulers.blocking import BlockingScheduler

from db import db_session
import reporter
import scorer
import scraper
import tagger


def _logger() -> logging.Logger:
    log = logging.getLogger("tradingbot.main")
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = logging.FileHandler("logs/bot.log")
    fh.setFormatter(formatter)
    sh = logging.StreamHandler()
    sh.setFormatter(formatter)
    log.addHandler(fh)
    log.addHandler(sh)
    return log


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
    scheduler.start()


if __name__ == "__main__":
    main()

