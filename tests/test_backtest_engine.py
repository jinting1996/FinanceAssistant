from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.collectors.kline_collector import KlineData
from src.core import backtest_engine, strategy_catalog, strategy_pool
from src.web.api import backtests as backtests_api
from src.web.database import Base
from src.web.models import (
    BacktestRun,
    BacktestStrategyMetric,
    BacktestTrade,
    Stock,
    StockScreenerFormula,
    StrategyCatalog,
    StrategySignalRun,
)


def _session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'backtest.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _patch_sessions(monkeypatch, Session):
    monkeypatch.setattr(backtest_engine, "SessionLocal", Session)
    monkeypatch.setattr(strategy_catalog, "SessionLocal", Session)
    monkeypatch.setattr(strategy_pool, "SessionLocal", Session)
    monkeypatch.setattr(backtests_api, "SessionLocal", Session)


def _k(date: str, open_: float, close: float, high: float | None = None, low: float | None = None) -> KlineData:
    return KlineData(
        date=date,
        open=open_,
        close=close,
        high=high if high is not None else max(open_, close),
        low=low if low is not None else min(open_, close),
        volume=1000,
    )


def _strategy(code: str = "trend_follow", **cfg) -> StrategyCatalog:
    return StrategyCatalog(
        code=code,
        name=code,
        enabled=True,
        market_scope="CN",
        strategy_type=cfg.pop("strategy_type", "builtin"),
        source_ref_type=cfg.pop("source_ref_type", ""),
        source_ref_id=cfg.pop("source_ref_id", None),
        run_config=cfg or {"position_pct": 0.05},
    )


def _run(strategy_codes: list[str] | None = None, **kwargs) -> BacktestRun:
    return BacktestRun(
        id=kwargs.get("id", "run-1"),
        status="queued",
        market=kwargs.get("market", "CN"),
        start_date=kwargs.get("start_date", "2026-01-01"),
        end_date=kwargs.get("end_date", "2026-01-10"),
        initial_capital=kwargs.get("initial_capital", 100000.0),
        strategy_codes=strategy_codes or ["trend_follow"],
    )


def _signal(
    strategy_code: str = "trend_follow",
    *,
    symbol: str = "000001",
    snapshot_date: str = "2026-01-02",
    entry_low: float = 9.5,
    entry_high: float = 10.5,
    stop_loss: float = 9.0,
    target_price: float = 12.0,
    holding_days: int = 3,
) -> StrategySignalRun:
    return StrategySignalRun(
        snapshot_date=snapshot_date,
        stock_symbol=symbol,
        stock_market="CN",
        stock_name=symbol,
        strategy_code=strategy_code,
        strategy_name=strategy_code,
        status="active",
        action="buy",
        rank_score=90,
        score=90,
        entry_low=entry_low,
        entry_high=entry_high,
        stop_loss=stop_loss,
        target_price=target_price,
        holding_days=holding_days,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def test_backtest_replays_signal_at_next_open_and_rounds_cn_lot(monkeypatch, tmp_path):
    Session = _session_factory(tmp_path)
    _patch_sessions(monkeypatch, Session)
    monkeypatch.setattr(
        backtest_engine,
        "fetch_klines_for_backtest",
        lambda symbol, market, days: [
            _k("2026-01-02", 9.8, 10.0),
            _k("2026-01-05", 10.0, 10.2),
            _k("2026-01-06", 10.4, 12.1, high=12.1, low=10.1),
        ],
    )
    db = Session()
    try:
        db.add(_strategy(position_pct=0.05))
        db.add(_signal(target_price=12.0))
        db.add(_run())
        db.commit()
    finally:
        db.close()

    summary = backtest_engine.run_backtest("run-1")

    db = Session()
    try:
        trade = db.query(BacktestTrade).filter(BacktestTrade.skipped.is_(False)).one()
        assert summary["total_trades"] == 1
        assert trade.entry_date == "2026-01-05"
        assert trade.entry_price == 10.0
        assert trade.quantity == 500
        assert trade.exit_reason == "target_price"
        assert trade.exit_date == "2026-01-06"
    finally:
        db.close()


def test_backtest_skips_when_open_outside_entry_range(monkeypatch, tmp_path):
    Session = _session_factory(tmp_path)
    _patch_sessions(monkeypatch, Session)
    monkeypatch.setattr(
        backtest_engine,
        "fetch_klines_for_backtest",
        lambda symbol, market, days: [_k("2026-01-02", 10.0, 10.0), _k("2026-01-05", 11.0, 11.0)],
    )
    db = Session()
    try:
        db.add(_strategy())
        db.add(_signal(entry_low=9.0, entry_high=10.5))
        db.add(_run())
        db.commit()
    finally:
        db.close()

    summary = backtest_engine.run_backtest("run-1")

    db = Session()
    try:
        trade = db.query(BacktestTrade).one()
        assert summary["total_trades"] == 0
        assert trade.skipped is True
        assert trade.skip_reason == "above_entry_range"
    finally:
        db.close()


def test_backtest_stop_loss_wins_when_stop_and_target_hit_same_day(monkeypatch, tmp_path):
    Session = _session_factory(tmp_path)
    _patch_sessions(monkeypatch, Session)
    monkeypatch.setattr(
        backtest_engine,
        "fetch_klines_for_backtest",
        lambda symbol, market, days: [
            _k("2026-01-02", 10.0, 10.0),
            _k("2026-01-05", 10.0, 10.1),
            _k("2026-01-06", 10.2, 10.0, high=12.5, low=8.8),
        ],
    )
    db = Session()
    try:
        db.add(_strategy())
        db.add(_signal(stop_loss=9.0, target_price=12.0))
        db.add(_run())
        db.commit()
    finally:
        db.close()

    backtest_engine.run_backtest("run-1")

    db = Session()
    try:
        trade = db.query(BacktestTrade).filter(BacktestTrade.skipped.is_(False)).one()
        assert trade.exit_reason == "stop_loss"
        assert trade.exit_price == 9.0
        assert trade.pnl < 0
    finally:
        db.close()


def test_backtest_fees_tax_and_slippage_reduce_pnl(monkeypatch, tmp_path):
    Session = _session_factory(tmp_path)
    _patch_sessions(monkeypatch, Session)
    monkeypatch.setattr(
        backtest_engine,
        "fetch_klines_for_backtest",
        lambda symbol, market, days: [
            _k("2026-01-02", 10.0, 10.0),
            _k("2026-01-05", 10.0, 10.0),
            _k("2026-01-06", 11.0, 11.0),
        ],
    )
    db = Session()
    try:
        db.add(_strategy(position_pct=0.05, fee_pct=0.001, tax_pct=0.001, slippage_pct=0.01))
        db.add(_signal(target_price=11.0, stop_loss=8.0))
        db.add(_run())
        db.commit()
    finally:
        db.close()

    backtest_engine.run_backtest("run-1")

    db = Session()
    try:
        trade = db.query(BacktestTrade).filter(BacktestTrade.skipped.is_(False)).one()
        assert trade.entry_price == 10.1
        assert trade.exit_price == 10.89
        assert trade.fees > 0
        assert trade.pnl < (10.89 - 10.1) * trade.quantity
    finally:
        db.close()


def test_backtest_forces_close_at_end_date(monkeypatch, tmp_path):
    Session = _session_factory(tmp_path)
    _patch_sessions(monkeypatch, Session)
    monkeypatch.setattr(
        backtest_engine,
        "fetch_klines_for_backtest",
        lambda symbol, market, days: [
            _k("2026-01-02", 10.0, 10.0),
            _k("2026-01-05", 10.0, 10.1),
            _k("2026-01-09", 10.2, 10.4),
        ],
    )
    db = Session()
    try:
        db.add(_strategy())
        db.add(_signal(target_price=20.0, stop_loss=5.0, holding_days=30))
        db.add(_run(end_date="2026-01-09"))
        db.commit()
    finally:
        db.close()

    backtest_engine.run_backtest("run-1")

    db = Session()
    try:
        trade = db.query(BacktestTrade).filter(BacktestTrade.skipped.is_(False)).one()
        assert trade.exit_date == "2026-01-09"
        assert trade.exit_reason == "end_date"
    finally:
        db.close()


def test_screener_formula_backtest_generates_trades_and_updates_ranking(monkeypatch, tmp_path):
    Session = _session_factory(tmp_path)
    _patch_sessions(monkeypatch, Session)
    monkeypatch.setattr(
        backtest_engine,
        "fetch_klines_for_backtest",
        lambda symbol, market, days: [
            _k("2025-12-31", 9.0, 9.0),
            _k("2026-01-02", 9.5, 10.0),
            _k("2026-01-05", 10.0, 10.2),
            _k("2026-01-06", 10.4, 10.8, high=10.8, low=10.2),
        ],
    )
    db = Session()
    try:
        formula = StockScreenerFormula(
            name="上涨",
            formula="C > REF(C,1)",
            universe_config={"provider": "panwatch", "include_watchlist": True, "include_watched_boards": False, "max_symbols": 1, "days": 30},
            enabled=True,
        )
        db.add(formula)
        db.flush()
        code = f"screener:{formula.id}"
        db.add(Stock(symbol="000001", name="平安银行", market="CN"))
        db.add(
            _strategy(
                code,
                strategy_type="screener_formula",
                source_ref_type="stock_screener_formula",
                source_ref_id=formula.id,
                position_pct=0.05,
                max_holding_days=1,
            )
        )
        db.add(_run([code], id="run-formula", start_date="2026-01-02", end_date="2026-01-06"))
        db.commit()
    finally:
        db.close()

    backtest_engine.run_backtest("run-formula")

    db = Session()
    try:
        trade = db.query(BacktestTrade).filter(BacktestTrade.skipped.is_(False)).first()
        metric = db.query(BacktestStrategyMetric).filter(BacktestStrategyMetric.strategy_code == code).one()
        ranking = strategy_pool.calculate_strategy_ranking(db)[code]
        assert trade is not None
        assert trade.strategy_type == "screener_formula"
        assert metric.sample_size >= 1
        assert ranking["sample_size"] >= 1
        assert ranking["status_label"] in {"样本不足", "已验证"}
    finally:
        db.close()


def test_backtest_api_create_and_missing_run(monkeypatch, tmp_path):
    Session = _session_factory(tmp_path)
    _patch_sessions(monkeypatch, Session)
    monkeypatch.setattr(backtests_api, "_start_background_run", lambda run_id: None)
    db = Session()
    try:
        db.add(_strategy())
        db.commit()
    finally:
        db.close()

    created = backtests_api.create_backtest_run(
        backtests_api.BacktestRunIn(
            strategy_codes=["trend_follow"],
            start_date="2026-01-01",
            end_date="2026-01-10",
            initial_capital=50000,
        )
    )

    assert created["status"] == "queued"
    assert created["strategy_codes"] == ["trend_follow"]
    fetched = backtests_api.get_backtest_run(created["id"])
    assert fetched["id"] == created["id"]
    assert backtests_api.get_backtest_trades(created["id"])["items"] == []
    with pytest.raises(HTTPException) as exc:
        backtests_api.get_backtest_run("missing")
    assert exc.value.status_code == 404
