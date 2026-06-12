"""Stooq US daily K-line Provider."""

from __future__ import annotations

import asyncio

from src.collectors.kline_collector import _fetch_stooq_us_klines
from src.core.providers.base import KlineProvider, ProviderRequest, ProviderResponse


def _extra(req: ProviderRequest, key: str, default):
    for k, v in req.extra:
        if k == key:
            return v
    return default


class StooqKlineProvider(KlineProvider):
    name = "stooq"
    supports_markets = {"US"}

    async def fetch(self, req: ProviderRequest) -> ProviderResponse:
        if not req.symbols:
            return ProviderResponse(success=True, data=[])
        if len(req.symbols) > 1:
            return ProviderResponse(success=False, error="StooqKlineProvider 仅支持单 symbol")
        if req.market != "US":
            return ProviderResponse(success=False, error="stooq only supports US")

        interval = str(_extra(req, "interval", "1d") or "1d").lower()
        if interval not in {"", "1d", "day", "d"}:
            return ProviderResponse(success=False, error=f"stooq daily provider does not support interval={interval}")
        days = int(_extra(req, "days", 60) or 60)
        try:
            data = await asyncio.to_thread(_fetch_stooq_us_klines, req.symbols[0])
        except Exception as e:
            return ProviderResponse(success=False, error=str(e))
        if len(data) > days:
            data = data[-days:]
        return ProviderResponse(success=True, data=data)
