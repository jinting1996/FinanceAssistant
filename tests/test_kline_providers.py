from src.collectors.kline_collector import KlineData
from src.core.providers.base import ProviderRequest
from src.core.providers.kline import eastmoney as eastmoney_module
from src.core.providers.kline import stooq as stooq_module
from src.core.providers.kline.eastmoney import EastmoneyKlineProvider
from src.core.providers.kline.stooq import StooqKlineProvider
from src.core.providers.orchestrator import KlineOrchestrator, get_kline_orchestrator
from src.models.market import MarketCode


def test_kline_orchestrator_registers_builtin_fallback_providers():
    orch = get_kline_orchestrator()

    providers = set(orch.registered_providers())

    assert {"tencent", "eastmoney", "stooq", "tushare", "yfinance"} <= providers


async def _fetch(provider, req):
    return await provider.fetch(req)


def test_eastmoney_provider_fetches_daily(monkeypatch):
    calls = []

    def fake_daily(symbol, market, days):
        calls.append((symbol, market, days))
        return [
            KlineData(
                date="2026-06-12",
                open=1,
                close=2,
                high=3,
                low=1,
                volume=100,
                source="eastmoney",
            )
        ]

    monkeypatch.setattr(eastmoney_module, "_fetch_eastmoney_klines", fake_daily)

    import asyncio

    resp = asyncio.run(
        EastmoneyKlineProvider().fetch(
            ProviderRequest(symbols=("AAPL",), market="US", extra=(("days", 5),))
        )
    )

    assert resp.success
    assert resp.data[0].source == "eastmoney"
    assert calls == [("AAPL", MarketCode.US, 5)]


def test_eastmoney_provider_fetches_intraday(monkeypatch):
    calls = []

    def fake_intraday(symbol, market, interval, limit):
        calls.append((symbol, market, interval, limit))
        return [
            KlineData(
                date="2026-06-12 09:31",
                open=1,
                close=2,
                high=3,
                low=1,
                volume=100,
                source="eastmoney",
            )
        ]

    monkeypatch.setattr(eastmoney_module, "_fetch_eastmoney_intraday_klines", fake_intraday)

    import asyncio

    resp = asyncio.run(
        EastmoneyKlineProvider().fetch(
            ProviderRequest(
                symbols=("600519",),
                market="CN",
                extra=(("days", 241), ("interval", "1min")),
            )
        )
    )

    assert resp.success
    assert resp.data[0].date == "2026-06-12 09:31"
    assert calls == [("600519", MarketCode.CN, "1min", 241)]


def test_stooq_provider_fetches_us_and_trims_days(monkeypatch):
    def fake_stooq(symbol):
        return [
            KlineData(date="2026-06-10", open=1, close=1, high=1, low=1, volume=1, source="stooq"),
            KlineData(date="2026-06-11", open=2, close=2, high=2, low=2, volume=2, source="stooq"),
            KlineData(date="2026-06-12", open=3, close=3, high=3, low=3, volume=3, source="stooq"),
        ]

    monkeypatch.setattr(stooq_module, "_fetch_stooq_us_klines", fake_stooq)

    import asyncio

    resp = asyncio.run(
        StooqKlineProvider().fetch(
            ProviderRequest(symbols=("AAPL",), market="US", extra=(("days", 2),))
        )
    )

    assert resp.success
    assert [k.date for k in resp.data] == ["2026-06-11", "2026-06-12"]
