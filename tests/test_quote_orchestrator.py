"""QuoteOrchestrator 单元测试 — 主备链、缓存、健康度。

所有测试用 mock provider,不依赖真实 DataSource 表(通过 monkeypatch 注入)。
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

import pytest

from src.core.providers.base import ProviderRequest, ProviderResponse, QuoteProvider
from src.core.providers.cache import TTLCache
from src.core.providers.orchestrator import QuoteOrchestrator


# ---- Mock providers ----


class _MockProvider(QuoteProvider):
    """可配置的 mock provider:每次调用按预设结果序列返回。"""

    def __init__(
        self,
        name: str,
        markets=("CN", "HK", "US"),
        results=None,
        raise_exc=None,
        latency_sec: float = 0,
        config=None,
    ):
        super().__init__(config=config)
        self.name = name
        self.supports_markets = set(markets)
        self._results = list(results or [])
        self._raise = raise_exc
        self._latency = latency_sec
        self.call_count = 0

    async def fetch(self, req: ProviderRequest) -> ProviderResponse:
        self.call_count += 1
        if self._latency:
            await asyncio.sleep(self._latency)
        if self._raise:
            raise self._raise
        if self._results:
            return self._results.pop(0)
        return ProviderResponse(success=True, data=[{"symbol": s, "current_price": 1.0} for s in req.symbols])


def _stub_sources(orch: QuoteOrchestrator, names: list[str]) -> None:
    """绕过 DB,直接给 orchestrator 注入"已启用源"列表。

    保留 market 过滤逻辑,与真实 _load_enabled_sources 一致。
    """
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


# ---- Tests ----


class TestOrchestratorFallback(unittest.IsolatedAsyncioTestCase):
    async def test_primary_success(self):
        """主源成功 — 不应调用备份源"""
        orch = QuoteOrchestrator()
        a = _MockProvider("a")
        b = _MockProvider("b")
        orch.register("a", lambda cfg: a)
        orch.register("b", lambda cfg: b)
        # 触发实例化
        orch._get_or_create_instance("a", {})
        orch._get_or_create_instance("b", {})
        _stub_sources(orch, ["a", "b"])

        resp = await orch.fetch(ProviderRequest(symbols=("600519",), market="CN"))
        self.assertTrue(resp.success)
        self.assertEqual(resp.provider, "a")
        self.assertEqual(a.call_count, 1)
        self.assertEqual(b.call_count, 0)

    async def test_primary_fails_secondary_succeeds(self):
        """主源失败 — 自动切到备份源"""
        orch = QuoteOrchestrator()
        a = _MockProvider("a", results=[ProviderResponse(success=False, error="timeout")])
        b = _MockProvider("b")
        orch.register("a", lambda cfg: a)
        orch.register("b", lambda cfg: b)
        orch._get_or_create_instance("a", {})
        orch._get_or_create_instance("b", {})
        _stub_sources(orch, ["a", "b"])

        resp = await orch.fetch(ProviderRequest(symbols=("600519",), market="CN"))
        self.assertTrue(resp.success)
        self.assertEqual(resp.provider, "b")
        self.assertEqual(a.call_count, 1)
        self.assertEqual(b.call_count, 1)

    async def test_primary_exception_secondary_succeeds(self):
        """主源抛异常 — 不应中断,继续尝试备份源"""
        orch = QuoteOrchestrator()
        a = _MockProvider("a", raise_exc=RuntimeError("boom"))
        b = _MockProvider("b")
        orch.register("a", lambda cfg: a)
        orch.register("b", lambda cfg: b)
        orch._get_or_create_instance("a", {})
        orch._get_or_create_instance("b", {})
        _stub_sources(orch, ["a", "b"])

        resp = await orch.fetch(ProviderRequest(symbols=("600519",), market="CN"))
        self.assertTrue(resp.success)
        self.assertEqual(resp.provider, "b")

    async def test_empty_data_treated_as_failure(self):
        """主源返回空 list — 视为失败,触发故障转移"""
        orch = QuoteOrchestrator()
        a = _MockProvider("a", results=[ProviderResponse(success=True, data=[])])
        b = _MockProvider("b")
        orch.register("a", lambda cfg: a)
        orch.register("b", lambda cfg: b)
        orch._get_or_create_instance("a", {})
        orch._get_or_create_instance("b", {})
        _stub_sources(orch, ["a", "b"])

        resp = await orch.fetch(ProviderRequest(symbols=("600519",), market="CN"))
        self.assertTrue(resp.success)
        self.assertEqual(resp.provider, "b")

    async def test_all_providers_fail(self):
        """所有 provider 都失败 — 返回 success=False 并带最后一个 error"""
        orch = QuoteOrchestrator()
        a = _MockProvider("a", results=[ProviderResponse(success=False, error="err_a")])
        b = _MockProvider("b", results=[ProviderResponse(success=False, error="err_b")])
        orch.register("a", lambda cfg: a)
        orch.register("b", lambda cfg: b)
        orch._get_or_create_instance("a", {})
        orch._get_or_create_instance("b", {})
        _stub_sources(orch, ["a", "b"])

        resp = await orch.fetch(ProviderRequest(symbols=("600519",), market="CN"))
        self.assertFalse(resp.success)
        self.assertEqual(resp.error, "err_b")

    async def test_market_filter(self):
        """market 不支持 — 跳过该 provider"""
        orch = QuoteOrchestrator()
        # a 只支持 CN,b 支持 US
        a = _MockProvider("a", markets=("CN",))
        b = _MockProvider("b", markets=("US",))
        orch.register("a", lambda cfg: a)
        orch.register("b", lambda cfg: b)
        orch._get_or_create_instance("a", {})
        orch._get_or_create_instance("b", {})
        _stub_sources(orch, ["a", "b"])

        resp = await orch.fetch(ProviderRequest(symbols=("AAPL",), market="US"))
        self.assertTrue(resp.success)
        self.assertEqual(resp.provider, "b")
        self.assertEqual(a.call_count, 0)


class TestOrchestratorCache(unittest.IsolatedAsyncioTestCase):
    async def test_cache_hit_avoids_provider_call(self):
        """缓存命中 — 不应再次调用 provider"""
        orch = QuoteOrchestrator()
        a = _MockProvider("a")
        orch.register("a", lambda cfg: a)
        orch._get_or_create_instance("a", {})
        _stub_sources(orch, ["a"])

        req = ProviderRequest(symbols=("600519",), market="CN")
        resp1 = await orch.fetch(req)
        resp2 = await orch.fetch(req)
        self.assertTrue(resp1.success)
        self.assertTrue(resp2.success)
        self.assertEqual(a.call_count, 1, "缓存命中应只调用 1 次")

    async def test_cache_ttl_zero_skips_write(self):
        """cache_ttl_sec=0 — 不写缓存,下次仍打 provider"""
        orch = QuoteOrchestrator()
        a = _MockProvider("a")
        orch.register("a", lambda cfg: a)
        orch._get_or_create_instance("a", {})
        _stub_sources(orch, ["a"])

        req = ProviderRequest(symbols=("600519",), market="CN")
        await orch.fetch(req, cache_ttl_sec=0)
        await orch.fetch(req, cache_ttl_sec=0)
        self.assertEqual(a.call_count, 2)


class TestOrchestratorHealth(unittest.IsolatedAsyncioTestCase):
    async def test_health_tracks_success_and_failure(self):
        """健康度统计 — 成功/失败都被记录"""
        orch = QuoteOrchestrator()
        a = _MockProvider("a", results=[
            ProviderResponse(success=True, data=[{"symbol": "X", "current_price": 1}]),
            ProviderResponse(success=False, error="x"),
        ])
        orch.register("a", lambda cfg: a)
        orch._get_or_create_instance("a", {})
        _stub_sources(orch, ["a"])

        await orch.fetch(ProviderRequest(symbols=("X",), market="CN"), cache_ttl_sec=0)
        await orch.fetch(ProviderRequest(symbols=("Y",), market="CN"), cache_ttl_sec=0)

        health = orch.health()
        self.assertIn("a", health)
        self.assertEqual(health["a"]["count"], 2)
        self.assertEqual(health["a"]["success_rate"], 0.5)


class TestTTLCache(unittest.TestCase):
    def test_basic_get_set(self):
        """TTL 缓存 — 基本 get/set"""
        c = TTLCache(default_ttl_sec=10)
        c.set("k", "v")
        self.assertEqual(c.get("k"), "v")
        self.assertIsNone(c.get("missing"))

    def test_expiry(self):
        """TTL 缓存 — TTL=0.01 立即过期"""
        import time as _t
        c = TTLCache(default_ttl_sec=0.01)
        c.set("k", "v")
        _t.sleep(0.05)
        self.assertIsNone(c.get("k"))

    def test_zero_ttl_skips_write(self):
        """TTL 缓存 — set(ttl=0) 跳过写入"""
        c = TTLCache(default_ttl_sec=10)
        c.set("k", "v", ttl_sec=0)
        self.assertIsNone(c.get("k"))


if __name__ == "__main__":
    unittest.main()
