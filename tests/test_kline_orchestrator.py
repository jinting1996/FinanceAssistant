"""KlineOrchestrator + Provider 用例 — 主备链、缓存、可选依赖降级。"""

from __future__ import annotations

import unittest
import asyncio
import shutil
import tempfile

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.providers.base import KlineProvider, ProviderRequest, ProviderResponse
from src.core.providers.orchestrator import KlineOrchestrator
from src.core import stock_kline_cache
from src.web.database import Base


class _MockKlineProvider(KlineProvider):
    def __init__(self, name, markets=("CN", "HK", "US"), results=None, config=None, delay=0):
        super().__init__(config=config)
        self.name = name
        self.supports_markets = set(markets)
        self._results = list(results or [])
        self.call_count = 0
        self.delay = delay

    async def fetch(self, req: ProviderRequest) -> ProviderResponse:
        self.call_count += 1
        if self.delay:
            await asyncio.sleep(self.delay)
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
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="kline-orch-")
        engine = create_engine(
            f"sqlite:///{self._tmpdir}/kline_cache.db",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(engine)
        self._old_session_local = stock_kline_cache.SessionLocal
        stock_kline_cache.SessionLocal = sessionmaker(bind=engine)

    def tearDown(self):
        stock_kline_cache.SessionLocal = self._old_session_local
        shutil.rmtree(self._tmpdir, ignore_errors=True)

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

    async def test_singleflight_dedupes_concurrent_cache_miss(self):
        """K线并发 cache miss — 同 key 只放行一次 provider 调用"""
        orch = KlineOrchestrator()
        p1 = _MockKlineProvider("p1", delay=0.02)
        orch.register("p1", lambda cfg: p1)
        orch._get_or_create_instance("p1", {})
        _stub_sources(orch, ["p1"])

        req = ProviderRequest(symbols=("600519",), market="CN", extra=(("days", 30),))
        results = await asyncio.gather(*[orch.fetch(req) for _ in range(10)])

        self.assertTrue(all(resp.success for resp in results))
        self.assertEqual(p1.call_count, 1)


class TestKlineIntervalCapability(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="kline-iv-")
        engine = create_engine(
            f"sqlite:///{self._tmpdir}/kline_cache.db",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(engine)
        self._old_session_local = stock_kline_cache.SessionLocal
        stock_kline_cache.SessionLocal = sessionmaker(bind=engine)

    def tearDown(self):
        stock_kline_cache.SessionLocal = self._old_session_local
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    async def test_intraday_skips_daily_only_provider(self):
        """分时请求 — 跳过只支持日线的 provider,路由到支持分钟级的 provider"""
        orch = KlineOrchestrator()
        daily = _MockKlineProvider("daily")  # supports_intraday 默认 False
        intraday = _MockKlineProvider("intraday")
        intraday.supports_intraday = True
        orch.register("daily", lambda cfg: daily)
        orch.register("intraday", lambda cfg: intraday)
        orch._get_or_create_instance("daily", {})
        orch._get_or_create_instance("intraday", {})
        _stub_sources(orch, ["daily", "intraday"])  # daily 优先级更高

        resp = await orch.fetch(
            ProviderRequest(
                symbols=("600519",),
                market="CN",
                extra=(("days", 1), ("interval", "1min")),
            )
        )
        self.assertTrue(resp.success)
        self.assertEqual(resp.provider, "intraday")
        self.assertEqual(daily.call_count, 0)  # 日线 provider 完全不被尝试

    async def test_intraday_no_capable_provider_clear_error(self):
        """分时请求 — 无支持分钟级的 provider 时返回清晰错误,不报日线 provider 的拒绝信息"""
        orch = KlineOrchestrator()
        daily = _MockKlineProvider("daily")
        orch.register("daily", lambda cfg: daily)
        orch._get_or_create_instance("daily", {})
        _stub_sources(orch, ["daily"])

        resp = await orch.fetch(
            ProviderRequest(
                symbols=("600519",),
                market="CN",
                extra=(("days", 1), ("interval", "5min")),
            )
        )
        self.assertFalse(resp.success)
        self.assertEqual(daily.call_count, 0)
        self.assertIn("5min", resp.error)
        self.assertIn("no kline provider supports interval", resp.error)

    async def test_intraday_never_falls_back_to_eastmoney(self):
        """腾讯分钟线失败时不得回退到需要代理的东方财富。"""
        orch = KlineOrchestrator()
        tencent = _MockKlineProvider(
            "tencent",
            results=[ProviderResponse(success=False, error="tencent unavailable")],
        )
        tencent.supports_intraday = True
        eastmoney = _MockKlineProvider("eastmoney")
        eastmoney.supports_intraday = True
        orch.register("tencent", lambda cfg: tencent)
        orch.register("eastmoney", lambda cfg: eastmoney)
        orch._get_or_create_instance("tencent", {})
        orch._get_or_create_instance("eastmoney", {})
        _stub_sources(orch, ["tencent", "eastmoney"])

        resp = await orch.fetch(
            ProviderRequest(
                symbols=("600519",),
                market="CN",
                extra=(("days", 241), ("interval", "1min")),
            )
        )

        self.assertFalse(resp.success)
        self.assertEqual(tencent.call_count, 1)
        self.assertEqual(eastmoney.call_count, 0)


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
