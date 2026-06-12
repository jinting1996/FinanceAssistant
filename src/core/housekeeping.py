"""Application data housekeeping jobs."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import distinct

from src.web.database import SessionLocal
from src.web.models import (
    AgentRun,
    AppSettings,
    BoardKlineCache,
    MarketScanSnapshot,
    NewsCache,
    PriceAlertHit,
    StockScreenerResult,
    StockScreenerRun,
    StrategySignalRun,
)

logger = logging.getLogger(__name__)

HOUSEKEEPING_RETENTION_SETTING_KEY = "housekeeping_retention"

DEFAULT_RETENTION: dict[str, int] = {
    "news_cache_days": 45,
    "agent_run_days": 90,
    "price_alert_hit_days": 180,
    "stock_screener_run_days": 90,
    "strategy_signal_run_days": 180,
    "market_scan_snapshot_days": 45,
    "board_kline_trading_days": 250,
}


def normalize_retention_config(payload: dict[str, Any] | None) -> dict[str, int]:
    cfg = dict(DEFAULT_RETENTION)
    if not isinstance(payload, dict):
        return cfg
    for key, default in DEFAULT_RETENTION.items():
        raw = payload.get(key, default)
        try:
            value = int(raw)
        except Exception:
            value = default
        cfg[key] = max(1, value)
    return cfg


def get_housekeeping_retention(db=None) -> dict[str, int]:
    def _load(session) -> dict[str, int]:
        row = (
            session.query(AppSettings)
            .filter(AppSettings.key == HOUSEKEEPING_RETENTION_SETTING_KEY)
            .first()
        )
        if not row or not row.value:
            return normalize_retention_config(None)
        try:
            data = json.loads(row.value)
        except Exception:
            return normalize_retention_config(None)
        return normalize_retention_config(data if isinstance(data, dict) else None)

    if db is not None:
        return _load(db)

    session = SessionLocal()
    try:
        return _load(session)
    finally:
        session.close()


def save_housekeeping_retention(payload: dict[str, Any], db=None) -> dict[str, int]:
    cfg = normalize_retention_config(payload)
    raw = json.dumps(cfg, ensure_ascii=False, sort_keys=True)

    def _save(session) -> dict[str, int]:
        row = (
            session.query(AppSettings)
            .filter(AppSettings.key == HOUSEKEEPING_RETENTION_SETTING_KEY)
            .first()
        )
        if row:
            row.value = raw
            row.description = "数据清理保留期配置"
        else:
            session.add(
                AppSettings(
                    key=HOUSEKEEPING_RETENTION_SETTING_KEY,
                    value=raw,
                    description="数据清理保留期配置",
                )
            )
        session.commit()
        return cfg

    if db is not None:
        return _save(db)

    session = SessionLocal()
    try:
        return _save(session)
    finally:
        session.close()


def _delete_older_than(session, model, column, cutoff: datetime) -> int:
    return session.query(model).filter(column < cutoff).delete(synchronize_session=False)


def _delete_snapshot_older_than(session, model, cutoff_date: str) -> int:
    return (
        session.query(model)
        .filter(model.snapshot_date < cutoff_date)
        .delete(synchronize_session=False)
    )


def _cleanup_board_klines(session, keep_trading_days: int) -> int:
    pairs = (
        session.query(BoardKlineCache.market, BoardKlineCache.board_code)
        .distinct()
        .all()
    )
    deleted = 0
    for market, board_code in pairs:
        dates = [
            row[0]
            for row in (
                session.query(distinct(BoardKlineCache.date))
                .filter(
                    BoardKlineCache.market == market,
                    BoardKlineCache.board_code == board_code,
                )
                .order_by(BoardKlineCache.date.desc())
                .all()
            )
        ]
        if len(dates) <= keep_trading_days:
            continue
        cutoff_date = dates[keep_trading_days - 1]
        deleted += (
            session.query(BoardKlineCache)
            .filter(
                BoardKlineCache.market == market,
                BoardKlineCache.board_code == board_code,
                BoardKlineCache.date < cutoff_date,
            )
            .delete(synchronize_session=False)
        )
    return deleted


def run_housekeeping(db=None, *, now: datetime | None = None) -> dict[str, int]:
    """Delete old operational data according to AppSettings retention config."""

    current = now or datetime.now()

    def _run(session) -> dict[str, int]:
        cfg = get_housekeeping_retention(session)
        stats: dict[str, int] = {}

        stats["news_cache"] = _delete_older_than(
            session,
            NewsCache,
            NewsCache.publish_time,
            current - timedelta(days=cfg["news_cache_days"]),
        )
        stats["agent_runs"] = _delete_older_than(
            session,
            AgentRun,
            AgentRun.created_at,
            current - timedelta(days=cfg["agent_run_days"]),
        )
        stats["price_alert_hits"] = _delete_older_than(
            session,
            PriceAlertHit,
            PriceAlertHit.trigger_time,
            current - timedelta(days=cfg["price_alert_hit_days"]),
        )

        screener_cutoff = current - timedelta(days=cfg["stock_screener_run_days"])
        old_run_ids = [
            row[0]
            for row in (
                session.query(StockScreenerRun.id)
                .filter(StockScreenerRun.created_at < screener_cutoff)
                .all()
            )
        ]
        if old_run_ids:
            stats["stock_screener_results"] = (
                session.query(StockScreenerResult)
                .filter(StockScreenerResult.run_id.in_(old_run_ids))
                .delete(synchronize_session=False)
            )
            stats["stock_screener_runs"] = (
                session.query(StockScreenerRun)
                .filter(StockScreenerRun.id.in_(old_run_ids))
                .delete(synchronize_session=False)
            )
        else:
            stats["stock_screener_results"] = 0
            stats["stock_screener_runs"] = 0

        stats["strategy_signal_runs"] = _delete_snapshot_older_than(
            session,
            StrategySignalRun,
            (current - timedelta(days=cfg["strategy_signal_run_days"])).strftime("%Y-%m-%d"),
        )
        stats["market_scan_snapshots"] = _delete_snapshot_older_than(
            session,
            MarketScanSnapshot,
            (current - timedelta(days=cfg["market_scan_snapshot_days"])).strftime("%Y-%m-%d"),
        )
        stats["board_kline_cache"] = _cleanup_board_klines(
            session,
            cfg["board_kline_trading_days"],
        )
        session.commit()
        logger.info("[housekeeping] 清理完成: %s", stats)
        return stats

    if db is not None:
        return _run(db)

    session = SessionLocal()
    try:
        return _run(session)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
