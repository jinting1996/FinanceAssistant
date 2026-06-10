"""资金流向 Provider:东方财富。

包装现有 `CapitalFlowCollector`,目前仅 CN 一个数据源,Orchestrator 为后续扩展预留。
"""

from __future__ import annotations

import asyncio
import logging

from src.collectors.capital_flow_collector import CapitalFlowCollector
from src.core.providers.base import CapitalFlowProvider, ProviderRequest, ProviderResponse
from src.models.market import MarketCode

logger = logging.getLogger(__name__)


class EastmoneyCapitalFlowProvider(CapitalFlowProvider):
    name = "eastmoney"
    supports_markets = {"CN", "HK", "US"}

    async def fetch(self, req: ProviderRequest) -> ProviderResponse:
        if not req.symbols:
            return ProviderResponse(success=True, data=[])

        try:
            market_code = MarketCode(req.market)
        except ValueError:
            return ProviderResponse(success=False, error=f"unsupported market: {req.market}")

        collector = CapitalFlowCollector(market_code)
        results = []
        for sym in req.symbols:
            try:
                flow = await asyncio.to_thread(collector.get_capital_flow, sym)
                if flow:
                    results.append(flow)
            except Exception as e:
                logger.debug(f"eastmoney capital_flow {sym} 失败: {e}")

        return ProviderResponse(success=True, data=results)
