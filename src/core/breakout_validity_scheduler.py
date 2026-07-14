"""突破有效性策略调度器:每个交易日收盘后扫描全市场并复评持仓。"""

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.core.breakout_validity_scanner import run_daily

logger = logging.getLogger(__name__)


class BreakoutValidityScheduler:
    def __init__(self, timezone: str = "Asia/Shanghai", hour: int = 15, minute: int = 20):
        self.scheduler = AsyncIOScheduler(timezone=timezone)
        self.hour = hour
        self.minute = minute
        self._running = False

    async def _daily_job(self) -> None:
        if self._running:
            logger.debug("[突破有效性] 上一轮仍在执行,跳过")
            return
        self._running = True
        try:
            result = await asyncio.to_thread(run_daily)
            logger.info("[突破有效性] 每日任务完成: %s", result)
        except Exception:
            logger.exception("[突破有效性] 每日任务异常")
        finally:
            self._running = False

    async def trigger_once(self) -> dict:
        return await asyncio.to_thread(run_daily)

    def start(self) -> None:
        self.scheduler.add_job(
            self._daily_job,
            "cron",
            day_of_week="mon-fri",
            hour=self.hour,
            minute=self.minute,
            id="breakout_validity_daily",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        self.scheduler.start()

    def shutdown(self) -> None:
        try:
            self.scheduler.shutdown(wait=False)
        except Exception:
            pass
