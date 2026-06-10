"""Tushare K 线 Provider(可选,需用户配 token)。

接入指南:`.docs/tushare-integration.md`。

约束:
- 仅 A 股(港股/美股 Tushare 接入有限,不在 MVP 范围)
- 软依赖:未安装 `tushare` 包时,初始化即 disabled,fetch 返回明确错误
- token 通过 DataSource.config["token"] 配置;也支持环境变量 TUSHARE_TOKEN 兜底
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta

from src.collectors.kline_collector import KlineData
from src.core.providers.base import KlineProvider, ProviderRequest, ProviderResponse

logger = logging.getLogger(__name__)


class TushareKlineProvider(KlineProvider):
    name = "tushare"
    supports_markets = {"CN"}  # MVP 仅支持 A 股

    def __init__(self, config: dict | None = None):
        super().__init__(config=config)
        self._tushare = None
        self._pro = None
        self._init_error = ""

        try:
            import tushare as ts  # noqa: F401
            self._tushare = ts
        except ImportError:
            self._init_error = "tushare 未安装,执行 `pip install tushare` 后启用此 provider"
            return

        token = (self.config or {}).get("token") or os.environ.get("TUSHARE_TOKEN", "")
        if not token:
            self._init_error = "tushare token 未配置(DataSource.config.token 或环境变量 TUSHARE_TOKEN)"
            return

        try:
            self._tushare.set_token(token)
            self._pro = self._tushare.pro_api()
        except Exception as e:
            self._init_error = f"tushare pro_api 初始化失败: {e}"

    @staticmethod
    def _ts_code(symbol: str) -> str:
        """A 股代码转 Tushare 格式(600519 → 600519.SH)。"""
        from src.core.cn_symbol import get_cn_prefix
        prefix = get_cn_prefix(symbol, upper=True)  # SH/SZ/BJ
        return f"{symbol}.{prefix}"

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
                error="TushareKlineProvider 仅支持单 symbol",
            )
        if req.market != "CN":
            return ProviderResponse(success=False, error="Tushare provider 仅支持 CN 市场")

        symbol = req.symbols[0]
        days = self._days(req)
        ts_code = self._ts_code(symbol)

        # Tushare daily 按交易日,start_date 多预留几天保证拿够 days 条
        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=days * 2)).strftime("%Y%m%d")

        def _fetch_blocking():
            return self._pro.daily(
                ts_code=ts_code, start_date=start_date, end_date=end_date
            )

        try:
            df = await asyncio.to_thread(_fetch_blocking)
        except Exception as e:
            return ProviderResponse(success=False, error=f"tushare daily 调用失败: {e}")

        if df is None or len(df) == 0:
            return ProviderResponse(success=False, error="tushare 返回空")

        # df 列:ts_code/trade_date/open/high/low/close/pre_close/change/pct_chg/vol/amount
        df = df.sort_values("trade_date")  # 升序,与 KlineCollector 保持一致
        klines: list[KlineData] = []
        for _, row in df.iterrows():
            try:
                d = str(row["trade_date"])
                # 转 YYYY-MM-DD 与 KlineCollector 保持一致
                date_fmt = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
                klines.append(
                    KlineData(
                        date=date_fmt,
                        open=float(row["open"]),
                        close=float(row["close"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        volume=float(row.get("vol") or 0),
                    )
                )
            except Exception as e:
                logger.debug(f"tushare row 解析失败: {e}")
                continue

        return ProviderResponse(success=True, data=klines[-days:])

    async def health_check(self) -> bool:
        if self._init_error:
            return False
        try:
            resp = await self.fetch(
                ProviderRequest(symbols=("600519",), market="CN", extra=(("days", 5),))
            )
            return resp.success and not resp.is_empty
        except Exception:
            return False
