"""Provider 协议与请求/响应基础类型。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ProviderRequest:
    """通用 Provider 请求。

    不同 type 的 Provider 关心不同字段,通过 extra 透传额外参数。
    使用 frozen=True 让请求对象可哈希,便于缓存 key。
    """

    symbols: tuple[str, ...] = ()
    market: str = "CN"
    timeframe: str = "day"
    since_hours: int = 12
    extra: tuple[tuple[str, Any], ...] = ()  # 排序后的 kv tuple,保证可哈希

    def cache_key(self, type_: str) -> str:
        """生成稳定的缓存键。"""
        sym_part = ",".join(self.symbols)
        extra_part = ",".join(f"{k}={v}" for k, v in self.extra)
        return f"{type_}|{self.market}|{self.timeframe}|{self.since_hours}|{sym_part}|{extra_part}"


@dataclass
class ProviderResponse:
    """通用 Provider 响应。

    - success: True 才会被 Orchestrator 接受
    - data: 具体形态由各 type 定义。Quote provider 返回 list[dict],dict 必带 symbol/current_price
    - error: 失败原因,Orchestrator 失败链路会把它带到下游
    - provider/latency_ms: 由 Orchestrator 填写,Provider 自身不必管
    """

    success: bool
    data: Any = None
    error: str = ""
    provider: str = ""
    latency_ms: int = 0
    partial: bool = False  # 预留:部分成功(N 个 symbol 拿到 M 个)

    @property
    def is_empty(self) -> bool:
        """data 为空集合也算失败,触发故障转移。"""
        if self.data is None:
            return True
        if isinstance(self.data, (list, tuple, dict, set)) and len(self.data) == 0:
            return True
        return False


class Provider(ABC):
    """Provider 协议根基。具体 type 应继承 QuoteProvider/KlineProvider/... 添加语义化字段。"""

    #: 注册到 Orchestrator 用的名称,与 DataSource.provider 字段对齐。
    name: str = ""

    #: 该 provider 支持的市场集合。Orchestrator 会按市场过滤候选。
    supports_markets: set[str] = field(default_factory=set)

    def __init__(self, config: dict | None = None):
        self.config = config or {}

    @abstractmethod
    async def fetch(self, req: ProviderRequest) -> ProviderResponse:
        """实际拉数据。Provider 自身不抛异常应当尽量捕获并返回 success=False。

        允许抛异常 — Orchestrator 会捕获并视为失败。
        """
        ...

    async def health_check(self) -> bool:
        """快速探活,供 UI/调度器调用。默认实现:尝试拉一次最小请求。"""
        return True


class QuoteProvider(Provider):
    """行情 Provider 语义化基类。

    fetch() 应返回 ProviderResponse(success=True, data=list[dict]),每个 dict 必含:
    - symbol: 原始股票代码(不带前缀)
    - current_price: 当前价(float)
    可选字段: name / change_pct / change_amount / volume / turnover / open_price /
    high_price / low_price / prev_close / market 等。
    """

    pass


class KlineProvider(Provider):
    """K 线 Provider 语义化基类。

    fetch() 应返回 ProviderResponse(success=True, data=list[KlineData]),
    KlineData 来自 src.collectors.kline_collector(date/open/close/high/low/volume)。
    通过 req.extra 传 days(默认 60)。
    """

    pass


class NewsProvider(Provider):
    """新闻 Provider 语义化基类。fetch() 返回 list[NewsItem]。"""

    pass


class CapitalFlowProvider(Provider):
    """资金流向 Provider 语义化基类。fetch() 返回 list[dict] 或单 dict。"""

    pass


class EventsProvider(Provider):
    """事件日历 Provider 语义化基类。fetch() 返回 list[EventItem]。"""

    pass


class DiscoveryProvider(Provider):
    """热门发现 Provider 语义化基类。fetch() 返回 list[dict]。"""

    pass


class ChartProvider(Provider):
    """K 线截图 Provider 语义化基类。fetch() 返回 bytes 或 str(图片路径)。"""

    pass
