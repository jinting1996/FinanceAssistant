"""板块估值调度器:冷启动自动回填历史,每交易日收盘后增量刷新申万一级行业估值。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.core.sector_valuation import backfill_years, refresh_latest
from src.web.database import SessionLocal
from src.web.models import SectorValuationDaily

logger = logging.getLogger(__name__)

BACKFILL_START_YEAR = 2021  # 冷启动回填起始年(约 5 年,够算 3/5 年分位)


def _row_count() -> int:
    db = SessionLocal()
    try:
        return db.query(SectorValuationDaily).count()
    finally:
        db.close()


def _do_backfill() -> dict:
    db = SessionLocal()
    try:
        return backfill_years(db, start_year=BACKFILL_START_YEAR, end_year=datetime.now().year)
    finally:
        db.close()


def _do_refresh() -> dict:
    db = SessionLocal()
    try:
        return refresh_latest(db)
    finally:
        db.close()


class SectorValuationScheduler:
    def __init__(self, timezone: str = "Asia/Shanghai", hour: int = 15, minute: int = 40):
        self.scheduler = AsyncIOScheduler(timezone=timezone)
        self.hour = hour
        self.minute = minute
        self._running = False

    async def _cold_start(self) -> None:
        """库为空时后台回填一次历史(耗时数分钟,不阻塞启动)。"""
        try:
            if _row_count() > 0:
                return
            logger.info("[板块估值] 估值库为空,开始冷启动回填(后台)…")
            report = await asyncio.to_thread(_do_backfill)
            logger.info("[板块估值] 冷启动回填完成: %s", report)
        except Exception:
            logger.exception("[板块估值] 冷启动回填异常")

    async def _daily_job(self) -> None:
        if self._running:
            logger.debug("[板块估值] 上一轮仍在执行,跳过")
            return
        self._running = True
        try:
            # 库空则先回填,否则只增量刷新最近窗口
            if _row_count() == 0:
                result = await asyncio.to_thread(_do_backfill)
            else:
                result = await asyncio.to_thread(_do_refresh)
            logger.info("[板块估值] 每日刷新完成: %s", result)
        except Exception:
            logger.exception("[板块估值] 每日刷新异常")
        finally:
            self._running = False

    async def trigger_backfill(self) -> dict:
        return await asyncio.to_thread(_do_backfill)

    def start(self) -> None:
        self.scheduler.add_job(
            self._daily_job,
            "cron",
            day_of_week="mon-fri",
            hour=self.hour,
            minute=self.minute,
            id="sector_valuation_daily",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        self.scheduler.start()
        # 启动后台冷启动回填(不阻塞事件循环)
        try:
            asyncio.get_event_loop().create_task(self._cold_start())
        except RuntimeError:
            pass
        logger.info("板块估值调度器已启动,每交易日 %02d:%02d 刷新", self.hour, self.minute)

    def shutdown(self) -> None:
        try:
            self.scheduler.shutdown(wait=False)
        except Exception:
            pass
        logger.info("板块估值调度器已关闭")
