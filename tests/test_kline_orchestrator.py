"""KlineOrchestrator + Provider 用例 — 主备链、缓存、可选依赖降级。"""

from __future__ import annotations

import unittest

from src.core.providers.base import KlineProvider, ProviderRequest, ProviderResponse
from src.core.providers.orchestrator import KlineOrchestrator


class _MockKlineProvider(KlineProvider):
    def __init__(self, name, markets=("CN", "HK", "US"), results=None, config=None):
        super().__init__(config=config)
        self.name = name
        self.supports_markets = set(markets)
        self._results = list(results or [])
        self.call_count = 0

    async def fetch(self, req: ProviderRequest) -> ProviderResponse:
        self.call_count += 1
        if self._results:
            return self._results.pop(0)
        # 默认返回 1 条假 kline
        return ProviderResponse(
            success=True,
            data=[{"date": "2025-01-01", "open": 1, "close": 1, "high": 1, "low": 1, "volume": 0}],
        )


def _stub_sources(orch: KlineOrchestrator, names: list[str]) -> None:
    def _fake_load(market: str):
        out = []
        for name in names:
            inst = orch._instances.get(name)
            if inst is None:
                continue
            if inst.supports_markets and market not in inst.supports_markets:
                continue
            out.append((name, {}))
        return out
    orch._load_enabled_sources = _fake_load


class TestKlineOrchestrator(unittest.IsolatedAsyncioTestCase):
    async def test_primary_succeeds_skip_backup(self):
        """K线主源成功 — 不调用备份"""
        orch = KlineOrchestrator()
        p1 = _MockKlineProvider("p1")
        p2 = _MockKlineProvider("p2")
        orch.register("p1", lambda cfg: p1)
        orch.register("p2", lambda cfg: p2)
        orch._get_or_create_instance("p1", {})
        orch._get_or_create_instance("p2", {})
        _stub_sources(orch, ["p1", "p2"])

        resp = await orch.fetch(
            ProviderRequest(symbols=("600519",), market="CN", extra=(("days", 30),))
        )
        self.assertTrue(resp.success)
        self.assertEqual(resp.provider, "p1")
        self.assertEqual(p1.call_count, 1)
        self.assertEqual(p2.call_count, 0)

    async def test_failover_chain(self):
        """K线主源失败 → 触发备份"""
        orch = KlineOrchestrator()
        p1 = _MockKlineProvider("p1", results=[ProviderResponse(success=False, error="boom")])
        p2 = _MockKlineProvider("p2")
        orch.register("p1", lambda cfg: p1)
        orch.register("p2", lambda cfg: p2)
        orch._get_or_create_instance("p1", {})
        orch._get_or_create_instance("p2", {})
        _stub_sources(orch, ["p1", "p2"])

        resp = await orch.fetch(
            ProviderRequest(symbols=("600519",), market="CN", extra=(("days", 30),))
        )
        self.assertTrue(resp.success)
        self.assertEqual(resp.provider, "p2")

    async def test_cache_hit(self):
        """K线缓存命中 — 不重复调 provider"""
        orch = KlineOrchestrator()
        p1 = _MockKlineProvider("p1")
        orch.register("p1", lambda cfg: p1)
        orch._get_or_create_instance("p1", {})
        _stub_sources(orch, ["p1"])

        req = ProviderRequest(symbols=("600519",), market="CN", extra=(("days", 30),))
        await orch.fetch(req)
        await orch.fetch(req)
        self.assertEqual(p1.call_count, 1)


class TestTushareSoftDep(unittest.IsolatedAsyncioTestCase):
    async def test_tushare_missing_returns_error_not_raise(self):
        """tushare 未安装 — 应返回 success=False 而不抛异常"""
        from src.core.providers.kline.tushare import TushareKlineProvider

        # 实例化时:有 tushare 可能装着,但没 token;无 tushare 也行 — 两种情况 init_error 都非空
        p = TushareKlineProvider(config={})
        if not p._init_error:
            # 如果环境已配 token 跳过
            self.skipTest("tushare 已配置,跳过软依赖测试")

        resp = await p.fetch(ProviderRequest(symbols=("600519",), market="CN"))
        self.assertFalse(resp.success)
        self.assertIn("tushare", resp.error.lower())


class TestYFinanceSoftDep(unittest.IsolatedAsyncioTestCase):
    async def test_yfinance_missing_quote_returns_error(self):
        """yfinance 未安装 — quote provider 返回 success=False"""
        from src.core.providers.quote.yfinance import YFinanceQuoteProvider

        p = YFinanceQuoteProvider(config={})
        if not p._init_error:
            self.skipTest("yfinance 已安装,跳过软依赖测试")

        resp = await p.fetch(ProviderRequest(symbols=("AAPL",), market="US"))
        self.assertFalse(resp.success)


if __name__ == "__main__":
    unittest.main()
