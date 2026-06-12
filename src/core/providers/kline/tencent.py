"""腾讯 K 线 Provider."""

from __future__ import annotations

import asyncio
import logging

from src.collectors.kline_collector import _fetch_tencent_klines
from src.core.providers.base import KlineProvider, ProviderRequest, ProviderResponse
from src.models.market import MarketCode

logger = logging.getLogger(__name__)


class TencentKlineProvider(KlineProvider):
    name = "tencent"
    supports_markets = {"CN", "HK", "US"}

    def _days(self, req: ProviderRequest) -> int:
        for k, v in req.extra:
            if k == "days":
                try:
                    return int(v)
                except Exception:
                    return 60
        return 60

    def _interval(self, req: ProviderRequest) -> str:
        for k, v in req.extra:
            if k == "interval":
                return str(v or "1d").lower()
        return "1d"

    async def fetch(self, req: ProviderRequest) -> ProviderResponse:
        if not req.symbols:
            return ProviderResponse(success=True, data=[])

        try:
            market_code = MarketCode(req.market)
        except ValueError:
            return ProviderResponse(success=False, error=f"unsupported market: {req.market}")

        # 当前 Orchestrator 单 symbol 用,批量按 symbol 串行(K 线接口本身就是单只)
        if len(req.symbols) > 1:
            return ProviderResponse(
                success=False,
                error="TencentKlineProvider only supports single symbol per request",
            )

        symbol = req.symbols[0]
        days = self._days(req)
        interval = self._interval(req)
        if interval not in {"", "1d", "day", "d"}:
            return ProviderResponse(success=False, error=f"tencent daily provider does not support interval={interval}")
        try:
            klines = await asyncio.to_thread(
                _fetch_tencent_klines, symbol, market_code, days
            )
        except Exception as e:
            return ProviderResponse(success=False, error=str(e))

        if req.market == "US" and len(klines) < max(10, min(days, 30)):
            return ProviderResponse(success=False, error="tencent US daily data insufficient")
        if req.market in {"CN", "HK"}:
            need_fallback = days >= 500 or len(klines) < max(120, int(days * 0.6))
            if need_fallback:
                return ProviderResponse(success=False, error="tencent daily data insufficient")

        return ProviderResponse(success=True, data=klines)

    async def health_check(self) -> bool:
        try:
            resp = await self.fetch(
                ProviderRequest(symbols=("600519",), market="CN", extra=(("days", 20),))
            )
            return resp.success and not resp.is_empty
        except Exception:
            return False
