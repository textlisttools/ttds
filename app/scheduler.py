"""Background jobs: pull orders, upload tracking, re-check prices and stock."""
import logging

from apscheduler.schedulers.background import BackgroundScheduler

from .config import get_settings
from .db import log, session
from .services import orders, products
from .services.connections import NotConnected

logger = logging.getLogger("ttds")

JOBS = {
    "orders": ("Check TikTok for new orders", orders.sync_orders),
    "tracking": ("Upload AliExpress tracking to TikTok", orders.sync_tracking),
    "monitor": ("Re-check AliExpress prices and stock", products.sync_all_products),
}


def run_job(name: str) -> str:
    label, fn = JOBS[name]
    with session() as db:
        try:
            n = fn(db, get_settings())
            return f"{label}: done ({n} change{'s' if n != 1 else ''})."
        except NotConnected as e:
            return f"{label}: skipped — {e}"
        except Exception as e:
            db.rollback()
            log(db, f"{label} failed: {e}", "error")
            db.commit()
            logger.exception("job %s failed", name)
            return f"{label}: failed — {e}"


def start() -> BackgroundScheduler:
    s = get_settings()
    sched = BackgroundScheduler(timezone="UTC")
    for name, minutes in (("orders", s.order_poll_minutes), ("tracking", s.tracking_poll_minutes),
                          ("monitor", s.monitor_poll_minutes)):
        sched.add_job(run_job, "interval", args=[name], minutes=minutes, id=name,
                      max_instances=1, coalesce=True)
    sched.start()
    return sched
