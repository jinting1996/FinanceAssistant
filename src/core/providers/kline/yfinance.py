"""YFinance K 线 Provider(可选)。

适用 HK / US。A 股 yfinance 数据不全,不在 MVP。
"""

from __future__ import annotations

import asyncio
import logging

from src.collectors.kline_collector import KlineData
from src.core.providers.base import KlineProvider, ProviderRequest, ProviderResponse

logger = logging.getLogger(__name__)


def _yf_ticker(symbol: str, market: str) -> str:
    if market == "US":
        return symbol
    if market == "HK":
        return f"{int(symbol):04d}.HK" if symbol.isdigit() else f"{symbol}.HK"
    return symbol


class YFinanceKlineProvider(KlineProvider):
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
            self._init_error = "yfinance 未安装"

    def _days(self, req: ProviderRequest) -> int:
        for k, v in req.extra:
            if k == "days":
                try:
                    return int(v)
                except Exception:
                    return 60
        return 60

    async def fetch(self, req: ProviderRequest) -> ProviderResponse:
        if self._init_error:
            return ProviderResponse(success=False, error=self._init_error)
        if not req.symbols:
            return ProviderResponse(success=True, data=[])
        if len(req.symbols) > 1:
            return ProviderResponse(
                success=False,
                error="YFinanceKlineProvider 仅支持单 symbol",
            )
        if req.market not in self.supports_markets:
            return ProviderResponse(success=False, error=f"yfinance 不支持 market={req.market}")

        symbol = req.symbols[0]
        days = self._days(req)

        def _blocking():
            # yfinance period 字符串映射;按 days 选最近的
            if days <= 30:
                period = "1mo"
            elif days <= 90:
                period = "3mo"
            elif days <= 180:
                period = "6mo"
            elif days <= 365:
                period = "1y"
            elif days <= 730:
                period = "2y"
            elif days <= 1825:
                period = "5y"
            else:
                period = "max"

            ticker = self._yf.Ticker(_yf_ticker(symbol, req.market))
            hist = ticker.history(period=period, interval="1d", auto_adjust=True)
            klines = []
            for idx, row in hist.iterrows():
                try:
                    klines.append(
                        KlineData(
                            date=idx.strftime("%Y-%m-%d"),
                            open=float(row["Open"]),
                            close=float(row["Close"]),
                            high=float(row["High"]),
                            low=float(row["Low"]),
                            volume=float(row.get("Volume") or 0),
                        )
                    )
                except Exception as e:
                    logger.debug(f"yfinance row 解析失败: {e}")
            return klines

        try:
            data = await asyncio.to_thread(_blocking)
        except Exception as e:
            return ProviderResponse(success=False, error=str(e))

        return ProviderResponse(success=True, data=data[-days:])
