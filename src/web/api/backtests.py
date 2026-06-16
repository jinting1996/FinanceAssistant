"""Backtest API for strategy-pool driven runs."""

from __future__ import annotations

from datetime import datetime, timedelta
import threading
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from src.core.backtest_engine import run_backtest
from src.core.strategy_catalog import ensure_strategy_catalog
from src.core.strategy_pool import list_strategy_pool
from src.web.database import SessionLocal
from src.web.models import BacktestDailyEquity, BacktestRun, BacktestStrategyMetric, BacktestTrade

router = APIRouter()


class BacktestRunIn(BaseModel):
    strategy_codes: list[str] = Field(default_factory=list)
    market: str = "CN"
    start_date: str = ""
    end_date: str = ""
    initial_capital: float = 1_000_000.0
    params: dict = Field(default_factory=dict)


def _default_dates(payload: BacktestRunIn) -> tuple[str, str]:
    today = datetime.now().date()
    end = payload.end_date or today.strftime("%Y-%m-%d")
    start = payload.start_date or (today - timedelta(days=180)).strftime("%Y-%m-%d")
    return start[:10], end[:10]


def _serialize_run(row: BacktestRun) -> dict:
    return {
        "id": row.id,
        "status": row.status,
        "message": row.message or "",
        "market": row.market,
        "start_date": row.start_date,
        "end_date": row.end_date,
        "initial_capital": float(row.initial_capital or 0.0),
        "strategy_codes": row.strategy_codes or [],
        "params": row.params or {},
        "summary": row.summary or {},
        "error": row.error or "",
        "created_at": row.created_at.isoformat() if row.created_at else "",
        "started_at": row.started_at.isoformat() if row.started_at else "",
        "finished_at": row.finished_at.isoformat() if row.finished_at else "",
        "updated_at": row.updated_at.isoformat() if row.updated_at else "",
    }


def _serialize_trade(row: BacktestTrade) -> dict:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "strategy_code": row.strategy_code,
        "strategy_name": row.strategy_name or "",
        "strategy_type": row.strategy_type or "",
        "source_ref_id": row.source_ref_id,
        "signal_run_id": row.signal_run_id,
        "stock_symbol": row.stock_symbol,
        "stock_market": row.stock_market,
        "stock_name": row.stock_name or "",
        "quantity": int(row.quantity or 0),
        "entry_date": row.entry_date or "",
        "exit_date": row.exit_date or "",
        "entry_price": float(row.entry_price or 0.0),
        "exit_price": float(row.exit_price or 0.0),
        "stop_loss": row.stop_loss,
        "target_price": row.target_price,
        "pnl": float(row.pnl or 0.0),
        "pnl_pct": float(row.pnl_pct or 0.0),
        "fees": float(row.fees or 0.0),
        "holding_days": int(row.holding_days or 0),
        "exit_reason": row.exit_reason or "",
        "skipped": bool(row.skipped),
        "skip_reason": row.skip_reason or "",
        "meta": row.meta or {},
    }


def _serialize_equity(row: BacktestDailyEquity) -> dict:
    return {
        "date": row.date,
        "cash": float(row.cash or 0.0),
        "positions_value": float(row.positions_value or 0.0),
        "equity": float(row.equity or 0.0),
        "drawdown_pct": float(row.drawdown_pct or 0.0),
    }


def _serialize_metric(row: BacktestStrategyMetric) -> dict:
    return {
        "strategy_code": row.strategy_code,
        "strategy_name": row.strategy_name or "",
        "strategy_type": row.strategy_type or "",
        "stock_market": row.stock_market or "",
        "total_trades": int(row.total_trades or 0),
        "winning_trades": int(row.winning_trades or 0),
        "win_rate": float(row.win_rate or 0.0),
        "total_pnl": float(row.total_pnl or 0.0),
        "total_return_pct": float(row.total_return_pct or 0.0),
        "avg_return_pct": float(row.avg_return_pct or 0.0),
        "max_drawdown_pct": float(row.max_drawdown_pct or 0.0),
        "recent_30d_return_pct": float(row.recent_30d_return_pct or 0.0),
        "sample_size": int(row.sample_size or 0),
        "exit_reason_counts": row.exit_reason_counts or {},
        "skip_reason_counts": row.skip_reason_counts or {},
    }


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _as_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes", "on"}
    return default


def _start_background_run(run_id: str) -> None:
    thread = threading.Thread(target=run_backtest, args=(run_id,), daemon=True)
    thread.start()


@router.post("/runs")
def create_backtest_run(payload: BacktestRunIn):
    ensure_strategy_catalog()
    pool = list_strategy_pool(enabled_only=True).get("items", [])
    codes = payload.strategy_codes or [str(x.get("code")) for x in pool if x.get("code")]
    start, end = _default_dates(payload)
    run_id = str(uuid4())
    db = SessionLocal()
    try:
        row = BacktestRun(
            id=run_id,
            status="queued",
            message="回测已入队",
            strategy_codes=codes,
            market=(payload.market or "CN").upper(),
            start_date=start,
            end_date=end,
            initial_capital=max(1.0, float(payload.initial_capital or 1_000_000.0)),
            params=payload.params or {},
            summary={
                "total_return_pct": None,
                "max_drawdown_pct": None,
                "win_rate": None,
                "sample_size": 0,
            },
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        out = _serialize_run(row)
    finally:
        db.close()
    _start_background_run(run_id)
    return out


@router.get("/runs/{run_id}")
def get_backtest_run(run_id: str):
    db = SessionLocal()
    try:
        row = db.query(BacktestRun).filter(BacktestRun.id == run_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="backtest run not found")
        return _serialize_run(row)
    finally:
        db.close()


@router.get("/runs/{run_id}/trades")
def get_backtest_trades(
    run_id: str,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    include_skipped: bool = True,
):
    limit = max(1, min(_as_int(limit, 100), 1000))
    offset = max(0, _as_int(offset, 0))
    include_skipped = _as_bool(include_skipped, True)
    db = SessionLocal()
    try:
        if not db.query(BacktestRun.id).filter(BacktestRun.id == run_id).first():
            raise HTTPException(status_code=404, detail="backtest run not found")
        q = db.query(BacktestTrade).filter(BacktestTrade.run_id == run_id)
        if not include_skipped:
            q = q.filter(BacktestTrade.skipped.is_(False))
        total = q.count()
        rows = (
            q.order_by(BacktestTrade.entry_date.asc(), BacktestTrade.id.asc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        return {"items": [_serialize_trade(row) for row in rows], "total": total, "limit": limit, "offset": offset}
    finally:
        db.close()


@router.get("/runs/{run_id}/equity")
def get_backtest_equity(run_id: str):
    db = SessionLocal()
    try:
        if not db.query(BacktestRun.id).filter(BacktestRun.id == run_id).first():
            raise HTTPException(status_code=404, detail="backtest run not found")
        rows = (
            db.query(BacktestDailyEquity)
            .filter(BacktestDailyEquity.run_id == run_id)
            .order_by(BacktestDailyEquity.date.asc())
            .all()
        )
        return {"items": [_serialize_equity(row) for row in rows]}
    finally:
        db.close()


@router.get("/runs/{run_id}/strategies")
def get_backtest_strategies(run_id: str):
    db = SessionLocal()
    try:
        if not db.query(BacktestRun.id).filter(BacktestRun.id == run_id).first():
            raise HTTPException(status_code=404, detail="backtest run not found")
        rows = (
            db.query(BacktestStrategyMetric)
            .filter(BacktestStrategyMetric.run_id == run_id)
            .order_by(BacktestStrategyMetric.total_pnl.desc(), BacktestStrategyMetric.strategy_code.asc())
            .all()
        )
        return {"items": [_serialize_metric(row) for row in rows]}
    finally:
        db.close()
