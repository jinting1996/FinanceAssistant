"""K-line service helpers backed by KlineOrchestrator."""

from __future__ import annotations

from src.core.http import run_sync
from src.core.providers import ProviderRequest, ProviderResponse, get_kline_orchestrator
from src.models.market import MarketCode


def _market_value(market: MarketCode | str) -> str:
    return market.value if isinstance(market, MarketCode) else str(market or "CN").upper()


def _fetch_response_sync(req: ProviderRequest, cache_ttl_sec: float | None) -> ProviderResponse:
    orch = get_kline_orchestrator()
    return run_sync(orch.fetch(req, cache_ttl_sec=cache_ttl_sec))


def fetch_kline_response_sync(
    symbol: str,
    market: MarketCode | str,
    *,
    days: int = 60,
    interval: str = "1d",
    cache_ttl_sec: float | None = None,
) -> ProviderResponse:
    normalized_interval = str(interval or "1d")
    ttl = cache_ttl_sec
    if ttl is None:
        ttl = 45 if normalized_interval.lower() in {"1min", "5min", "1", "5", "5m"} else 60
    req = ProviderRequest(
        symbols=(symbol,),
        market=_market_value(market),
        extra=(("days", int(days or 60)), ("interval", normalized_interval)),
    )
    return _fetch_response_sync(req, ttl)


def fetch_klines_sync(
    symbol: str,
    market: MarketCode | str,
    *,
    days: int = 60,
    interval: str = "1d",
    cache_ttl_sec: float | None = None,
) -> list:
    resp = fetch_kline_response_sync(
        symbol,
        market,
        days=days,
        interval=interval,
        cache_ttl_sec=cache_ttl_sec,
    )
    if not resp.success:
        return []
    return resp.data or []
