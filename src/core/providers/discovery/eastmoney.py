"""热门发现 Provider:东方财富。

包装现有 `EastMoneyDiscoveryCollector`。请求通过 req.extra 区分:
- ("kind", "stocks"|"boards") — 默认 stocks
- ("mode", "turnover"|"gainers")
- ("limit", int)
"""

from __future__ import annotations

import logging

from src.collectors.discovery_collector import EastMoneyDiscoveryCollector
from src.core.providers.base import DiscoveryProvider, ProviderRequest, ProviderResponse

logger = logging.getLogger(__name__)


def _extra(req: ProviderRequest, key: str, default):
    for k, v in req.extra:
        if k == key:
            return v
    return default


class EastmoneyDiscoveryProvider(DiscoveryProvider):
    name = "eastmoney"
    supports_markets = {"CN", "HK", "US"}

    def __init__(self, config: dict | None = None):
        super().__init__(config=config)
        self._collector = EastMoneyDiscoveryCollector(
            timeout_s=(self.config or {}).get("timeout", 8.0),
        )

    async def fetch(self, req: ProviderRequest) -> ProviderResponse:
        kind = _extra(req, "kind", "stocks")
        mode = _extra(req, "mode", "turnover")
        limit = int(_extra(req, "limit", 20))

        try:
            if kind == "boards":
                if req.market != "CN":
                    return ProviderResponse(success=False, error="boards 仅支持 CN")
                data = await self._collector.fetch_hot_boards(
                    market=req.market, mode=mode, limit=limit
                )
            elif kind == "board_stocks":
                if req.market != "CN":
                    return ProviderResponse(success=False, error="board_stocks only supports CN")
                board_code = str(_extra(req, "board_code", "") or "").strip()
                if not board_code:
                    return ProviderResponse(success=False, error="missing board_code")
                data = await self._collector.fetch_board_stocks(
                    board_code=board_code, mode=mode, limit=limit
                )
            else:
                data = await self._collector.fetch_hot_stocks(
                    market=req.market, mode=mode, limit=limit
                )
        except Exception as e:
            return ProviderResponse(success=False, error=str(e))

        return ProviderResponse(success=True, data=data or [])
