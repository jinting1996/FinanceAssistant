"""YFinance 行情 Provider(可选)。

适用场景:HK/US 主源或备份,A 股不可用。
软依赖:未安装 `yfinance` 时 fetch 返回错误。需要 HTTP 代理(从 app_settings 读)。
"""

from __future__ import annotations

import asyncio
import logging

from src.core.providers.base import ProviderRequest, ProviderResponse, QuoteProvider

logger = logging.getLogger(__name__)


def _yf_ticker(symbol: str, market: str) -> str:
    """转 yfinance 代码:
    - US: 直接 symbol(AAPL)
    - HK: 5 位代码 + .HK(00700 → 0700.HK,需去前导 0 留 4 位)
    - CN: 不支持
    """
    if market == "US":
        return symbol
    if market == "HK":
        # HK 在 yfinance 是 4 位带 .HK
        return f"{int(symbol):04d}.HK" if symbol.isdigit() else f"{symbol}.HK"
    return symbol  # CN 不支持,由 supports_markets 拦截


class YFinanceQuoteProvider(QuoteProvider):
    name = "yfinance"
    supports_markets = {"HK", "US"}

    def __init__(self, config: dict | None = None):
        super().__init__(config=config)
        self._yf = None
        self._init_error = ""
        try:
            import yfinance as yf  # noqa: F401
            self._yf = yf
        except ImportError:
            self._init_error = "yfinance 未安装,执行 `pip install yfinance` 后启用此 provider"

    async def fetch(self, req: ProviderRequest) -> ProviderResponse:
        if self._init_error:
            return ProviderResponse(success=False, error=self._init_error)
        if not req.symbols:
            return ProviderResponse(success=True, data=[])
        if req.market not in self.supports_markets:
            return ProviderResponse(success=False, error=f"yfinance 不支持 market={req.market}")

        def _blocking():
            results = []
            for sym in req.symbols:
                try:
                    ticker = self._yf.Ticker(_yf_ticker(sym, req.market))
                    info = ticker.fast_info  # 比 .info 快很多
                    last_price = float(info["last_price"]) if info.get("last_price") else None
                    prev_close = float(info["previous_close"]) if info.get("previous_close") else None
                    if last_price is None:
                        continue
                    change_amount = last_price - prev_close if prev_close else 0
                    change_pct = (change_amount / prev_close * 100) if prev_close else 0
                    results.append({
                        "symbol": sym,
                        "name": "",
                        "market": req.market,
                        "current_price": last_price,
                        "prev_close": prev_close,
                        "open_price": float(info.get("open") or 0),
                        "high_price": float(info.get("day_high") or 0),
                        "low_price": float(info.get("day_low") or 0),
                        "change_amount": change_amount,
                        "change_pct": change_pct,
                        "volume": float(info.get("last_volume") or 0),
                        "turnover": None,
                    })
                except Exception as e:
                    logger.debug(f"yfinance 拉取 {sym} 失败: {e}")
            return results

        try:
            data = await asyncio.to_thread(_blocking)
        except Exception as e:
            return ProviderResponse(success=False, error=str(e))

        return ProviderResponse(success=True, data=data)
