"""数据源 Provider 抽象层。

本包把"实时行情/K 线/新闻/资金流/事件"等数据源统一到 Provider + Orchestrator 模式:

- `base.py` 定义 ProviderRequest / ProviderResponse / Provider 协议
- `cache.py` 通用 TTL 缓存
- `orchestrator.py` 主备调度: 按 DataSource.priority 顺序尝试,首个成功返回
- `quote/` 行情类型的具体 Provider 实现(目前只有 tencent)

Phase 2 范围:仅 quote 类型抽象;其余 type 在后续 phase 接入。
"""

from src.core.providers.base import (
    CapitalFlowProvider,
    ChartProvider,
    DiscoveryProvider,
    EventsProvider,
    KlineProvider,
    NewsProvider,
    Provider,
    ProviderRequest,
    ProviderResponse,
    QuoteProvider,
)
from src.core.providers.orchestrator import (
    CapitalFlowOrchestrator,
    ChartOrchestrator,
    DiscoveryOrchestrator,
    EventsOrchestrator,
    KlineOrchestrator,
    NewsOrchestrator,
    QuoteOrchestrator,
    get_capital_flow_orchestrator,
    get_chart_orchestrator,
    get_discovery_orchestrator,
    get_events_orchestrator,
    get_kline_orchestrator,
    get_news_orchestrator,
    get_quote_orchestrator,
)

__all__ = [
    "Provider",
    "ProviderRequest",
    "ProviderResponse",
    "QuoteProvider",
    "KlineProvider",
    "NewsProvider",
    "CapitalFlowProvider",
    "EventsProvider",
    "DiscoveryProvider",
    "ChartProvider",
    "QuoteOrchestrator",
    "KlineOrchestrator",
    "NewsOrchestrator",
    "CapitalFlowOrchestrator",
    "EventsOrchestrator",
    "DiscoveryOrchestrator",
    "ChartOrchestrator",
    "get_quote_orchestrator",
    "get_kline_orchestrator",
    "get_news_orchestrator",
    "get_capital_flow_orchestrator",
    "get_events_orchestrator",
    "get_discovery_orchestrator",
    "get_chart_orchestrator",
]
