from src.collectors.kline_collector import KlineData
from src.models.market import MarketCode
from src.web.api.klines import _load_klines, _load_klines_from_orchestrator


class FakeKlineCollector:
    def __init__(self):
        self.daily_calls = []
        self.intraday_calls = []

    def get_klines(self, symbol: str, days: int = 60):
        self.daily_calls.append((symbol, days))
        return [
            KlineData(date="2026-01-02", open=10, close=11, high=12, low=9, volume=100),
            KlineData(date="2026-01-30", open=11, close=12, high=13, low=10, volume=120),
            KlineData(date="2026-02-02", open=12, close=13, high=14, low=11, volume=130),
        ]

    def get_intraday_klines(self, symbol: str, interval: str = "1min", limit: int = 241):
        self.intraday_calls.append((symbol, interval, limit))
        return [
            KlineData(date="2026-02-02 09:31", open=10, close=10.1, high=10.2, low=9.9, volume=1000),
            KlineData(date="2026-02-02 09:32", open=10.1, close=10.2, high=10.3, low=10, volume=900),
        ]


def test_one_minute_interval_uses_intraday_collector():
    collector = FakeKlineCollector()

    out = _load_klines(collector, "600519", 241, "1min")

    assert [k.date for k in out] == ["2026-02-02 09:31", "2026-02-02 09:32"]
    assert collector.intraday_calls == [("600519", "1min", 241)]
    assert collector.daily_calls == []


def test_five_minute_interval_uses_intraday_collector():
    collector = FakeKlineCollector()

    _load_klines(collector, "600519", 240, "5min")

    assert collector.intraday_calls == [("600519", "5min", 240)]
    assert collector.daily_calls == []


def test_legacy_one_m_interval_still_means_monthly_kline():
    collector = FakeKlineCollector()

    out = _load_klines(collector, "600519", 60, "1m")

    assert collector.daily_calls == [("600519", 60)]
    assert collector.intraday_calls == []
    assert [k.date for k in out] == ["2026-01-30", "2026-02-02"]
    assert out[0].open == 10
    assert out[0].close == 12
    assert out[0].high == 13
    assert out[0].low == 9
    assert out[0].volume == 220


def test_kline_collector_delegates_daily_request_to_orchestrator(monkeypatch):
    calls = []

    class FakeResponse:
        success = True
        error = ""
        data = [
            KlineData(
                date="2026-06-12",
                open=200,
                close=201,
                high=202,
                low=199,
                volume=1000,
                source="eastmoney",
            )
        ]

    def fake_fetch(symbol, market, days, interval, cache_ttl_sec):
        calls.append((symbol, market, days, interval, cache_ttl_sec))
        return FakeResponse()

    monkeypatch.setattr("src.core.kline_service.fetch_kline_response_sync", fake_fetch)

    from src.collectors.kline_collector import KlineCollector

    out = KlineCollector(MarketCode.US).get_klines("AAPL", days=5)

    assert [k.date for k in out] == ["2026-06-12"]
    assert out[0].source == "eastmoney"
    assert calls == [("AAPL", MarketCode.US, 5, "1d", 60)]


def test_api_monthly_interval_fetches_daily_from_orchestrator(monkeypatch):
    calls = []

    class FakeResponse:
        success = True
        error = ""
        data = [
            KlineData(date="2026-01-02", open=10, close=11, high=12, low=9, volume=100, source="mock"),
            KlineData(date="2026-01-30", open=11, close=12, high=13, low=10, volume=120, source="mock"),
            KlineData(date="2026-02-02", open=12, close=13, high=14, low=11, volume=130, source="mock"),
        ]

    def fake_fetch(symbol, market, days, interval, cache_ttl_sec):
        calls.append((symbol, market, days, interval, cache_ttl_sec))
        return FakeResponse()

    monkeypatch.setattr("src.web.api.klines.fetch_kline_response_sync", fake_fetch)

    out = _load_klines_from_orchestrator("600519", MarketCode.CN, 60, "1m")

    assert [k.date for k in out] == ["2026-01-30", "2026-02-02"]
    assert out[0].source == "mock"
    assert calls == [("600519", MarketCode.CN, 60, "1d", 60)]
