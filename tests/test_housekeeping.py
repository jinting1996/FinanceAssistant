from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.housekeeping import run_housekeeping, save_housekeeping_retention
from src.web.database import Base
from src.web.models import (
    AgentRun,
    BoardKlineCache,
    MarketScanSnapshot,
    NewsCache,
    PriceAlertHit,
    PriceAlertRule,
    Stock,
    StockScreenerResult,
    StockScreenerRun,
    StrategySignalRun,
)


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_housekeeping_deletes_expired_operational_rows():
    db = _session()
    now = datetime(2026, 6, 12, 4, 0, 0)
    try:
        save_housekeeping_retention(
            {
                "news_cache_days": 10,
                "agent_run_days": 10,
                "price_alert_hit_days": 10,
                "stock_screener_run_days": 10,
                "strategy_signal_run_days": 10,
                "market_scan_snapshot_days": 10,
                "board_kline_trading_days": 2,
            },
            db,
        )
        stock = Stock(symbol="600519", market="CN", name="贵州茅台")
        db.add(stock)
        db.flush()
        rule = PriceAlertRule(stock_id=stock.id, name="test")
        db.add(rule)
        db.flush()

        old_dt = now - timedelta(days=30)
        fresh_dt = now - timedelta(days=2)
        db.add_all(
            [
                NewsCache(source="x", external_id="old", title="old", publish_time=old_dt),
                NewsCache(source="x", external_id="fresh", title="fresh", publish_time=fresh_dt),
                AgentRun(agent_name="a", status="success", created_at=old_dt),
                AgentRun(agent_name="a", status="success", created_at=fresh_dt),
                PriceAlertHit(
                    rule_id=rule.id,
                    stock_id=stock.id,
                    trigger_time=old_dt,
                    trigger_bucket="old",
                ),
                PriceAlertHit(
                    rule_id=rule.id,
                    stock_id=stock.id,
                    trigger_time=fresh_dt,
                    trigger_bucket="fresh",
                ),
                StockScreenerRun(
                    formula_snapshot="C>0",
                    status="success",
                    created_at=old_dt,
                ),
                StockScreenerRun(
                    formula_snapshot="C>0",
                    status="success",
                    created_at=fresh_dt,
                ),
                StrategySignalRun(
                    snapshot_date="2026-05-01",
                    stock_symbol="600519",
                    stock_market="CN",
                    strategy_code="demo",
                ),
                StrategySignalRun(
                    snapshot_date="2026-06-10",
                    stock_symbol="000001",
                    stock_market="CN",
                    strategy_code="demo",
                ),
                MarketScanSnapshot(
                    snapshot_date="2026-05-01",
                    stock_symbol="600519",
                    stock_market="CN",
                ),
                MarketScanSnapshot(
                    snapshot_date="2026-06-10",
                    stock_symbol="000001",
                    stock_market="CN",
                ),
            ]
        )
        db.flush()
        old_run = (
            db.query(StockScreenerRun)
            .filter(StockScreenerRun.created_at == old_dt)
            .first()
        )
        fresh_run = (
            db.query(StockScreenerRun)
            .filter(StockScreenerRun.created_at == fresh_dt)
            .first()
        )
        db.add_all(
            [
                StockScreenerResult(
                    run_id=old_run.id,
                    symbol="600519",
                    market="CN",
                    name="old",
                ),
                StockScreenerResult(
                    run_id=fresh_run.id,
                    symbol="000001",
                    market="CN",
                    name="fresh",
                ),
            ]
        )
        for i, day in enumerate(["2026-06-08", "2026-06-09", "2026-06-10"]):
            db.add(
                BoardKlineCache(
                    market="CN",
                    board_code="BK0001",
                    date=day,
                    open=1 + i,
                    high=1 + i,
                    low=1 + i,
                    close=1 + i,
                )
            )
        db.commit()

        stats = run_housekeeping(db, now=now)

        assert stats["news_cache"] == 1
        assert stats["agent_runs"] == 1
        assert stats["price_alert_hits"] == 1
        assert stats["stock_screener_runs"] == 1
        assert stats["stock_screener_results"] == 1
        assert stats["strategy_signal_runs"] == 1
        assert stats["market_scan_snapshots"] == 1
        assert stats["board_kline_cache"] == 1

        assert db.query(NewsCache).count() == 1
        assert db.query(AgentRun).count() == 1
        assert db.query(PriceAlertHit).count() == 1
        assert db.query(StockScreenerRun).count() == 1
        assert db.query(StockScreenerResult).count() == 1
        assert db.query(StrategySignalRun).count() == 1
        assert db.query(MarketScanSnapshot).count() == 1
        assert [r.date for r in db.query(BoardKlineCache).order_by(BoardKlineCache.date)] == [
            "2026-06-09",
            "2026-06-10",
        ]
    finally:
        db.close()
