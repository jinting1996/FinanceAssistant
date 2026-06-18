"""模拟盘按策略累计盈亏曲线 单元测试。"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.web.api.paper_trading import _build_strategy_pnl_curve
from src.web.database import Base
from src.web.models import PaperTradingTrade


def _session(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'paper.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _trade(strategy_code, market, pnl, day):
    return PaperTradingTrade(
        stock_symbol="000001",
        stock_market=market,
        quantity=100,
        entry_price=10.0,
        exit_price=10.0,
        pnl=pnl,
        strategy_code=strategy_code,
        closed_at=datetime(2026, 1, day, tzinfo=timezone.utc),
    )


def test_strategy_pnl_curve_accumulates_by_close_date(tmp_path):
    """按策略累计盈亏:同日合并、按日累加,只含该策略的交易"""
    Session = _session(tmp_path)
    db = Session()
    try:
        db.add_all([
            _trade("alpha", "CN", 100.0, 2),
            _trade("alpha", "CN", 50.0, 2),   # 同日合并 → +150
            _trade("alpha", "CN", -30.0, 5),  # 累计 → 120
            _trade("beta", "CN", 999.0, 3),   # 其他策略不计入
        ])
        db.commit()

        curve = _build_strategy_pnl_curve(db, "alpha", None)
        assert [p["date"] for p in curve] == ["2026-01-02", "2026-01-05"]
        assert curve[0]["equity"] == 150.0
        assert curve[1]["equity"] == 120.0
    finally:
        db.close()


def test_strategy_pnl_curve_filters_by_market(tmp_path):
    """按策略累计盈亏:可按市场过滤"""
    Session = _session(tmp_path)
    db = Session()
    try:
        db.add_all([
            _trade("alpha", "CN", 100.0, 2),
            _trade("alpha", "HK", 200.0, 3),
        ])
        db.commit()

        cn = _build_strategy_pnl_curve(db, "alpha", "CN")
        assert len(cn) == 1 and cn[0]["equity"] == 100.0
    finally:
        db.close()
