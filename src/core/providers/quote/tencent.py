"""腾讯行情 Provider:包装现有的 `_fetch_tencent_quotes` 同步函数。

行为完全兼容原 `_fetch_tencent_quotes` — 返回 list[dict],每个 dict 至少含
`symbol / current_price / change_pct / change_amount / volume / turnover` 等字段。
"""

from __future__ import annotations

import asyncio
import logging

from src.collectors.akshare_collector import _fetch_tencent_quotes, _tencent_symbol
from src.core.providers.base import ProviderRequest, ProviderResponse, QuoteProvider
from src.models.market import MarketCode

logger = logging.getLogger(__name__)


class TencentQuoteProvider(QuoteProvider):
    name = "tencent"
    supports_markets = {"CN", "HK", "US"}

    async def fetch(self, req: ProviderRequest) -> ProviderResponse:
        if not req.symbols:
            return ProviderResponse(success=True, data=[])

        try:
            market_code = MarketCode(req.market)
        except ValueError:
            return ProviderResponse(success=False, error=f"unsupported market: {req.market}")

        tencent_syms = [_tencent_symbol(s, market_code) for s in req.symbols]
        try:
            # 底层是同步 httpx,丢到线程池避免阻塞事件循环
            rows = await asyncio.to_thread(_fetch_tencent_quotes, tencent_syms)
        except Exception as e:
            return ProviderResponse(success=False, error=str(e))

        # 标注 market 字段,方便上层按 (market, symbol) 索引
        for row in rows:
            row.setdefault("market", req.market)

        return ProviderResponse(success=True, data=rows)

    async def health_check(self) -> bool:
        """探活:拉一次贵州茅台。"""
        try:
            resp = await self.fetch(
                ProviderRequest(symbols=("600519",), market="CN")
            )
            return resp.success and not resp.is_empty
        except Exception:
            return False
