from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import pytest
from fastapi import HTTPException

from src.core import mcp_datasource, paper_trading_engine, strategy_catalog, strategy_pool
from src.core.paper_trading_engine import PaperTradingEngine
from src.web.api import paper_trading, recommendations
from src.web.database import Base
from src.web.models import (
    PaperTradingAccount,
    PaperTradingPosition,
    PaperTradingTrade,
    StockScreenerFormula,
    StrategyCatalog,
    StrategySignalRun,
)


def _session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'strategy_pool.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _patch_sessions(monkeypatch, Session):
    monkeypatch.setattr(strategy_catalog, "SessionLocal", Session)
    monkeypatch.setattr(strategy_pool, "SessionLocal", Session)
    monkeypatch.setattr(paper_trading_engine, "SessionLocal", Session)


def _signal(
    *,
    symbol: str,
    strategy_code: str,
    entry_low: float = 9.5,
    entry_high: float = 10.5,
) -> StrategySignalRun:
    from datetime import date

    return StrategySignalRun(
        snapshot_date=date.today().strftime("%Y-%m-%d"),
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
        stop_loss=8.8,
        target_price=12.0,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def test_strategy_pool_seeds_default_builtin_strategies(monkeypatch, tmp_path):
    """策略池 — 默认初始化返回 10 个内置策略且带新字段"""
    Session = _session_factory(tmp_path)
    _patch_sessions(monkeypatch, Session)

    data = strategy_pool.list_strategy_pool(enabled_only=False)
    items = data["items"]

    assert len(items) == 10
    assert {item["strategy_type"] for item in items} == {"builtin"}
    assert {item["code"] for item in items} >= {
        "trend_follow",
        "macd_golden",
        "market_scan",
        "price_action",
        "base_position_vwap_t",
    }
    assert all("run_config" in item for item in items)
    assert all("auto_run_enabled" in item for item in items)


def test_register_screener_strategy_is_idempotent(monkeypatch, tmp_path):
    """策略池 — 选股公式加入策略池重复调用只更新不重复创建"""
    Session = _session_factory(tmp_path)
    _patch_sessions(monkeypatch, Session)
    db = Session()
    try:
        formula = StockScreenerFormula(
            name="低位放量",
            description="测试公式",
            formula="C > MA(C,5)",
            universe_config={"provider": "panwatch"},
            enabled=True,
        )
        db.add(formula)
        db.commit()
        formula_id = formula.id
    finally:
        db.close()

    first = strategy_pool.register_screener_strategy(
        formula_id,
        run_config={"max_results": 10},
    )
    second = strategy_pool.register_screener_strategy(
        formula_id,
        run_config={"position_pct": 0.12},
    )

    db = Session()
    try:
        rows = (
            db.query(StrategyCatalog)
            .filter(StrategyCatalog.code == f"screener:{formula_id}")
            .all()
        )
        assert len(rows) == 1
        row = rows[0]
        assert first["code"] == second["code"] == f"screener:{formula_id}"
        assert row.strategy_type == "screener_formula"
        assert row.source_ref_type == "stock_screener_formula"
        assert row.source_ref_id == formula_id
        assert row.run_config["max_results"] == 10
        assert row.run_config["position_pct"] == 0.12
    finally:
        db.close()


def test_top_n_selection_excludes_insufficient_samples(monkeypatch, tmp_path):
    """策略池 — Top N 自动选择排除样本数小于 5 的策略"""
    Session = _session_factory(tmp_path)
    _patch_sessions(monkeypatch, Session)
    db = Session()
    try:
        db.add_all(
            [
                StrategyCatalog(
                    code="sample_low",
                    name="样本不足",
                    strategy_type="builtin",
                    enabled=True,
                ),
                StrategyCatalog(
                    code="sample_ok",
                    name="样本足够",
                    strategy_type="builtin",
                    enabled=True,
                ),
            ]
        )
        now = datetime.now(timezone.utc)
        for i in range(4):
            db.add(
                PaperTradingTrade(
                    stock_symbol=f"L{i}",
                    stock_market="CN",
                    stock_name=f"L{i}",
                    quantity=100,
                    entry_price=10,
                    exit_price=11,
                    pnl=100,
                    pnl_pct=10,
                    exit_reason="target_price",
                    strategy_code="sample_low",
                    closed_at=now - timedelta(days=i),
                )
            )
        for i in range(5):
            db.add(
                PaperTradingTrade(
                    stock_symbol=f"O{i}",
                    stock_market="CN",
                    stock_name=f"O{i}",
                    quantity=100,
                    entry_price=10,
                    exit_price=11,
                    pnl=100,
                    pnl_pct=10,
                    exit_reason="target_price",
                    strategy_code="sample_ok",
                    closed_at=now - timedelta(days=i),
                )
            )
        strategy_pool.save_paper_strategy_selection(
            {"mode": "top_n", "top_n": 5, "strategy_codes": []},
            db,
        )

        selected = strategy_pool.resolve_enabled_strategy_codes_for_paper(db)

        assert selected == {"sample_ok"}
    finally:
        db.close()


def test_paper_entries_use_selected_strategy_and_record_skips(monkeypatch, tmp_path):
    """模拟盘 — 自定义策略选择只建仓选中策略,未选中策略在SQL层被过滤"""
    Session = _session_factory(tmp_path)
    _patch_sessions(monkeypatch, Session)
    db = Session()
    try:
        db.add_all(
            [
                StrategyCatalog(
                    code="trend_follow",
                    name="趋势延续",
                    strategy_type="builtin",
                    enabled=True,
                    run_config={"position_pct": 0.1},
                ),
                StrategyCatalog(
                    code="macd_golden",
                    name="MACD金叉",
                    strategy_type="builtin",
                    enabled=True,
                ),
                PaperTradingAccount(
                    initial_capital=1_000_000,
                    current_capital=1_000_000,
                    peak_capital=1_000_000,
                    enabled=True,
                    market_allocations={"CN": 1.0, "HK": 0.0, "US": 0.0},
                ),
                _signal(symbol="600001", strategy_code="trend_follow"),
                _signal(symbol="600002", strategy_code="macd_golden"),
            ]
        )
        strategy_pool.save_paper_strategy_selection(
            {"mode": "custom", "strategy_codes": ["trend_follow"], "top_n": 5},
            db,
        )
        db.commit()

        engine = PaperTradingEngine()
        monkeypatch.setattr(
            engine,
            "_fetch_quotes_map",
            lambda symbols_markets: {
                ("CN", "600001"): {"symbol": "600001", "current_price": 10.0},
                ("CN", "600002"): {"symbol": "600002", "current_price": 10.0},
            },
        )
        account = db.query(PaperTradingAccount).first()
        opened, _, _, skip_events = engine._check_entries(db, account)

        assert opened == 1
        assert db.query(PaperTradingPosition).count() == 1
        pos = db.query(PaperTradingPosition).first()
        assert pos.stock_symbol == "600001"
        assert pos.quantity == 10_000
        # 未选中策略的信号在 SQL 层就被过滤,不进入候选(也不再产生逐条跳过事件)
        assert all(evt["reason"] != "existing_position" or evt["strategy_code"] != "macd_golden" for evt in skip_events)
    finally:
        db.close()


def test_paper_entries_require_price_inside_entry_range(monkeypatch, tmp_path):
    """模拟盘 — 当前价不在入场区间内时不建仓并记录跳过原因"""
    Session = _session_factory(tmp_path)
    _patch_sessions(monkeypatch, Session)
    db = Session()
    try:
        db.add_all(
            [
                StrategyCatalog(
                    code="trend_follow",
                    name="趋势延续",
                    strategy_type="builtin",
                    enabled=True,
                ),
                PaperTradingAccount(
                    initial_capital=1_000_000,
                    current_capital=1_000_000,
                    peak_capital=1_000_000,
                    enabled=True,
                    market_allocations={"CN": 1.0, "HK": 0.0, "US": 0.0},
                ),
                _signal(
                    symbol="600001",
                    strategy_code="trend_follow",
                    entry_low=9.0,
                    entry_high=9.5,
                ),
            ]
        )
        strategy_pool.save_paper_strategy_selection(
            {"mode": "custom", "strategy_codes": ["trend_follow"], "top_n": 5},
            db,
        )
        db.commit()

        engine = PaperTradingEngine()
        monkeypatch.setattr(
            engine,
            "_fetch_quotes_map",
            lambda symbols_markets: {
                ("CN", "600001"): {"symbol": "600001", "current_price": 10.0}
            },
        )
        account = db.query(PaperTradingAccount).first()
        opened, _, _, skip_events = engine._check_entries(db, account)

        assert opened == 0
        assert db.query(PaperTradingPosition).count() == 0
        assert [evt["reason"] for evt in skip_events] == ["above_entry_range"]
    finally:
        db.close()


def test_strategy_pool_update_rejects_invalid_risk_level(monkeypatch, tmp_path):
    """策略池 API — 非法风险等级被拒绝"""
    Session = _session_factory(tmp_path)
    _patch_sessions(monkeypatch, Session)
    strategy_pool.list_strategy_pool(enabled_only=False)

    with pytest.raises(HTTPException) as exc:
        recommendations.update_strategy_pool_api(
            "trend_follow",
            recommendations.StrategyPoolUpdateIn(risk_level="extreme"),
        )

    assert exc.value.status_code == 400


def test_strategy_pool_from_missing_screener_formula_returns_404(monkeypatch, tmp_path):
    """策略池 API — 不存在的选股公式注册返回 404"""
    Session = _session_factory(tmp_path)
    _patch_sessions(monkeypatch, Session)

    with pytest.raises(HTTPException) as exc:
        recommendations.create_strategy_from_screener_api(
            999,
            recommendations.ScreenerStrategyRegisterIn(run_config={}),
        )

    assert exc.value.status_code == 404


def test_mcp_datasource_catalog_exposes_reserved_capabilities():
    """MCP 数据源 — 预留目录包含 tdx_mcp 与 ifind 能力"""
    data = mcp_datasource.mcp_datasource_catalog()
    providers = {item["provider"]: item for item in data["items"]}

    assert {"tdx_mcp", "ifind"} <= set(providers)
    assert "intraday_kline" in providers["tdx_mcp"]["capabilities"]
    assert "research" in providers["ifind"]["capabilities"]


def test_strategy_performance_includes_exit_reason_distribution(monkeypatch, tmp_path):
    """模拟盘绩效 — 每个策略返回退出原因分布"""
    Session = _session_factory(tmp_path)
    _patch_sessions(monkeypatch, Session)
    db = Session()
    try:
        db.add_all(
            [
                PaperTradingTrade(
                    stock_symbol="600001",
                    stock_market="CN",
                    stock_name="A",
                    quantity=100,
                    entry_price=10,
                    exit_price=11,
                    pnl=100,
                    pnl_pct=10,
                    exit_reason="target_price",
                    strategy_code="trend_follow",
                    closed_at=datetime.now(timezone.utc),
                ),
                PaperTradingTrade(
                    stock_symbol="600002",
                    stock_market="CN",
                    stock_name="B",
                    quantity=100,
                    entry_price=10,
                    exit_price=9,
                    pnl=-100,
                    pnl_pct=-10,
                    exit_reason="stop_loss",
                    strategy_code="trend_follow",
                    closed_at=datetime.now(timezone.utc),
                ),
                PaperTradingTrade(
                    stock_symbol="600003",
                    stock_market="CN",
                    stock_name="C",
                    quantity=100,
                    entry_price=10,
                    exit_price=12,
                    pnl=200,
                    pnl_pct=20,
                    exit_reason="target_price",
                    strategy_code="trend_follow",
                    closed_at=datetime.now(timezone.utc),
                ),
            ]
        )
        db.commit()

        perf = paper_trading._strategy_performance(db, None)

        trend = next(item for item in perf if item["strategy_code"] == "trend_follow")
        assert trend["exit_reason_counts"] == {"target_price": 2, "stop_loss": 1}
    finally:
        db.close()
