import time
from datetime import date, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.collectors.kline_collector import KlineData
from src.core.screener.providers import ScreenerStock
from src.web.api import screener
from src.web.database import Base
from src.web.models import StockScreenerRun


def _session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'screener.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _bars(count: int = 60):
    start = date.today() - timedelta(days=count - 1)
    return [
        KlineData(
            date=(start + timedelta(days=i)).isoformat(),
            open=10 + i,
            close=10 + i,
            high=11 + i,
            low=9 + i,
            volume=1000 + i,
            source="test",
        )
        for i in range(count)
    ]


class FakeScreenerProvider:
    name = "fake"

    def resolve_universe(self, db, config, *, limit: int):
        return [
            ScreenerStock(
                symbol="600519",
                market="CN",
                name="贵州茅台",
                board_code="BK0001",
                board_name="白酒",
            )
        ][:limit]

    def fetch_klines(self, stock, *, days: int):
        return _bars(days)


def test_screener_create_run_executes_and_returns_results(monkeypatch, tmp_path):
    Session = _session_factory(tmp_path)
    monkeypatch.setattr(screener, "SessionLocal", Session)
    monkeypatch.setattr(screener, "get_screener_provider", lambda name=None: FakeScreenerProvider())

    db = Session()
    try:
        created = screener.create_run(
            screener.ScreenerRunIn(
                formula="C > MA(C,5)",
                universe_config={
                    "provider": "panwatch",
                    "include_watchlist": False,
                    "include_watched_boards": True,
                    "max_symbols": 1,
                    "days": 60,
                },
            ),
            db=db,
        )
        run_id = created["id"]

        deadline = time.time() + 3
        row = None
        while time.time() < deadline:
            db.expire_all()
            row = db.query(StockScreenerRun).filter(StockScreenerRun.id == run_id).first()
            if row and row.status in {"success", "failed"}:
                break
            time.sleep(0.05)

        assert row is not None
        assert row.status == "success"
        result = screener.get_run(run_id, db=db)
        assert result["status"] == "success"
        assert result["total_count"] == 1
        assert result["matched_count"] == 1
        assert result["results"][0]["symbol"] == "600519"
        assert result["results"][0]["board_name"] == "白酒"
        assert result["results"][0]["indicators"]["ma5"] is not None
    finally:
        db.close()
