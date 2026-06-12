from datetime import date, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.collectors.kline_collector import KlineData
from src.core.providers.base import KlineProvider, ProviderRequest, ProviderResponse
from src.core.providers.orchestrator import KlineOrchestrator
from src.core import stock_kline_cache
from src.core.stock_kline_cache import (
    load_cached_daily_klines,
    upsert_daily_klines,
)
from src.web.database import Base


def _session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _bars(start: date, count: int, *, close_base: float = 10, source: str = "mock"):
    return [
        KlineData(
            date=(start + timedelta(days=i)).isoformat(),
            open=close_base + i,
            close=close_base + i,
            high=close_base + i + 1,
            low=close_base + i - 1,
            volume=100 + i,
            source=source,
        )
        for i in range(count)
    ]


class RecordingKlineProvider(KlineProvider):
    name = "recording"
    supports_markets = {"CN", "US", "HK"}

    def __init__(self, response_bars):
        super().__init__()
        self.response_bars = response_bars
        self.requests = []

    async def fetch(self, req: ProviderRequest) -> ProviderResponse:
        self.requests.append(req)
        return ProviderResponse(success=True, data=self.response_bars)


class SequenceKlineProvider(KlineProvider):
    name = "sequence"
    supports_markets = {"CN", "US", "HK"}

    def __init__(self, responses):
        super().__init__()
        self.responses = list(responses)
        self.requests = []

    async def fetch(self, req: ProviderRequest) -> ProviderResponse:
        self.requests.append(req)
        return ProviderResponse(success=True, data=self.responses.pop(0))


def _stub_sources(orch: KlineOrchestrator, names: list[str]) -> None:
    def _fake_load(market: str):
        return [(name, {}) for name in names]

    orch._load_enabled_sources = _fake_load


def test_stock_kline_cache_loads_recent_complete_rows(monkeypatch):
    Session = _session_factory()
    monkeypatch.setattr(stock_kline_cache, "SessionLocal", Session)
    today = date.today()
    db = Session()
    try:
        upsert_daily_klines("CN", "600519", _bars(today - timedelta(days=4), 5), db=db)
        cached, complete = load_cached_daily_klines(
            "CN",
            "600519",
            days=5,
            db=db,
            today=today,
        )

        assert complete is True
        assert len(cached) == 5
        assert cached[-1].date == today.isoformat()
    finally:
        db.close()


def test_kline_orchestrator_uses_sqlite_cache_without_external_fetch(monkeypatch):
    Session = _session_factory()
    monkeypatch.setattr(stock_kline_cache, "SessionLocal", Session)
    today = date.today()
    db = Session()
    try:
        upsert_daily_klines("CN", "600519", _bars(today - timedelta(days=119), 120), db=db)
    finally:
        db.close()

    provider = RecordingKlineProvider(_bars(today - timedelta(days=29), 30, close_base=110))
    orch = KlineOrchestrator()
    orch.register("recording", lambda cfg: provider)
    orch._get_or_create_instance("recording", {})
    _stub_sources(orch, ["recording"])

    import asyncio

    resp = asyncio.run(
        orch.fetch(ProviderRequest(symbols=("600519",), market="CN", extra=(("days", 120), ("interval", "1d"))))
    )

    assert resp.success
    assert len(resp.data) == 120
    assert resp.provider == "sqlite"
    assert provider.requests == []


def test_kline_orchestrator_fetches_increment_window_when_sqlite_stale(monkeypatch):
    Session = _session_factory()
    monkeypatch.setattr(stock_kline_cache, "SessionLocal", Session)
    today = date.today()
    db = Session()
    try:
        upsert_daily_klines("CN", "600519", _bars(today - timedelta(days=129), 120), db=db)
    finally:
        db.close()

    provider = RecordingKlineProvider(_bars(today - timedelta(days=29), 30, close_base=110))
    orch = KlineOrchestrator()
    orch.register("recording", lambda cfg: provider)
    orch._get_or_create_instance("recording", {})
    _stub_sources(orch, ["recording"])

    import asyncio

    resp = asyncio.run(
        orch.fetch(ProviderRequest(symbols=("600519",), market="CN", extra=(("days", 120), ("interval", "1d"))))
    )

    assert resp.success
    assert len(provider.requests) == 1
    assert dict(provider.requests[0].extra)["days"] == 30
    assert len(resp.data) == 120


def test_kline_orchestrator_refetches_full_window_on_adjusted_price_drift(monkeypatch):
    Session = _session_factory()
    monkeypatch.setattr(stock_kline_cache, "SessionLocal", Session)
    today = date.today()
    db = Session()
    try:
        upsert_daily_klines("CN", "600519", _bars(today - timedelta(days=129), 120), db=db)
    finally:
        db.close()

    provider = SequenceKlineProvider(
        [
            _bars(today - timedelta(days=29), 30, close_base=200),
            _bars(today - timedelta(days=119), 120, close_base=200),
        ]
    )
    orch = KlineOrchestrator()
    orch.register("sequence", lambda cfg: provider)
    orch._get_or_create_instance("sequence", {})
    _stub_sources(orch, ["sequence"])

    import asyncio

    resp = asyncio.run(
        orch.fetch(ProviderRequest(symbols=("600519",), market="CN", extra=(("days", 120), ("interval", "1d"))))
    )

    assert resp.success
    assert len(provider.requests) == 2
    assert dict(provider.requests[0].extra)["days"] == 30
    assert dict(provider.requests[1].extra)["days"] == 120
    assert len(resp.data) == 120
    assert resp.data[0].close == 200


def test_stock_kline_cache_resets_on_adjusted_price_drift(monkeypatch):
    Session = _session_factory()
    monkeypatch.setattr(stock_kline_cache, "SessionLocal", Session)
    day = date.today() - timedelta(days=2)
    db = Session()
    try:
        upsert_daily_klines("CN", "600519", _bars(day, 3, close_base=10), db=db)
        stats = upsert_daily_klines("CN", "600519", _bars(day, 3, close_base=20), db=db)
        cached, complete = load_cached_daily_klines(
            "CN",
            "600519",
            days=3,
            db=db,
            today=date.today(),
        )

        assert stats["reset"] is True
        assert complete is True
        assert [k.close for k in cached] == [20, 21, 22]
    finally:
        db.close()
