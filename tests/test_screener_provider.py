from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.screener.providers import PanWatchScreenerDataProvider
from src.web.database import Base
from src.web.models import Stock, WatchedBoard


def test_universe_dedupes_watchlist_and_board_stocks(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(Stock(symbol="600519", market="CN", name="贵州茅台", sort_order=1))
        db.add(WatchedBoard(market="CN", board_code="BK0001", board_name="白酒", sort_order=1, enabled=True))
        db.commit()

        class FakeDiscoveryOrchestrator:
            def fetch_sync(self, *args, **kwargs):
                return SimpleNamespace(
                    success=True,
                    data=[
                        SimpleNamespace(symbol="600519", name="贵州茅台"),
                        SimpleNamespace(symbol="000858", name="五粮液"),
                    ],
                )

        monkeypatch.setattr(
            "src.core.screener.providers.get_discovery_orchestrator",
            lambda: FakeDiscoveryOrchestrator(),
        )

        rows = PanWatchScreenerDataProvider().resolve_universe(
            db,
            {
                "include_watchlist": True,
                "include_watched_boards": True,
                "board_codes": [],
                "max_symbols": 300,
            },
            limit=300,
        )

        keys = [f"{x.market}:{x.symbol}" for x in rows]
        assert keys == ["CN:600519", "CN:000858"]
        assert rows[0].board_code == "BK0001"
    finally:
        db.close()
