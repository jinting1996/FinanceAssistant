"""底仓做 T 独立分钟调度器。"""

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.core.t_monitor_engine import ENGINE

logger = logging.getLogger(__name__)


class TMonitorScheduler:
    def __init__(self, timezone: str = "Asia/Shanghai", interval_seconds: int = 60):
        self.scheduler = AsyncIOScheduler(timezone=timezone)
        self.interval_seconds = max(30, int(interval_seconds))
        self._running = False

    async def _scan_job(self) -> None:
        if self._running:
            return
        self._running = True
        try:
            result = await ENGINE.scan_once()
            logger.log(logging.INFO if result.get("triggered") else logging.DEBUG, "[做T盯盘] %s", result)
        except Exception:
            logger.exception("[做T盯盘] 扫描异常")
        finally:
            self._running = False

    async def trigger_once(self, *, position_id: int | None = None) -> dict:
        return await ENGINE.scan_once(position_id=position_id, bypass_market_hours=True)

    def start(self) -> None:
        self.scheduler.add_job(self._scan_job, "interval", seconds=self.interval_seconds, id="t_monitor_scan", replace_existing=True, coalesce=True, max_instances=1)
        self.scheduler.start()

    def shutdown(self) -> None:
        try:
            self.scheduler.shutdown(wait=False)
        except Exception:
            pass
