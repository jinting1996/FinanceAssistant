"""MCP datasource capability descriptors.

This module intentionally defines the integration contract only. Real tdx/iFinD
MCP adapters can implement these capabilities as MCP servers become available.
"""

from __future__ import annotations

from dataclasses import dataclass


MCP_CAPABILITIES = (
    "quote",
    "kline",
    "intraday_kline",
    "news",
    "announcement",
    "board",
    "board_stocks",
    "financial",
    "research",
    "screener",
)


@dataclass(frozen=True)
class McpDatasourceSpec:
    provider: str
    label: str
    priority_capabilities: tuple[str, ...]
    description: str
    configured: bool = False


DEFAULT_MCP_SPECS = (
    McpDatasourceSpec(
        provider="tdx_mcp",
        label="通达信 MCP",
        priority_capabilities=(
            "quote",
            "kline",
            "intraday_kline",
            "board",
            "board_stocks",
            "screener",
        ),
        description="预留通达信 MCP 接入位，优先增强 A 股行情、K 线、分钟线、板块和公式选股。",
    ),
    McpDatasourceSpec(
        provider="ifind",
        label="iFinD MCP",
        priority_capabilities=(
            "news",
            "announcement",
            "research",
            "financial",
            "board",
            "screener",
        ),
        description="预留同花顺/iFinD MCP 接入位，优先增强新闻、公告、研报、财务和行业事件。",
    ),
)


def mcp_datasource_catalog() -> dict:
    return {
        "capabilities": list(MCP_CAPABILITIES),
        "items": [
            {
                "name": spec.provider,
                "provider": spec.provider,
                "label": spec.label,
                "type": "mcp",
                "status": "reserved",
                "available": False,
                "enabled": False,
                "priority": 100,
                "capabilities": list(spec.priority_capabilities),
                "priority_capabilities": list(spec.priority_capabilities),
                "description": spec.description,
                "configured": spec.configured,
                "config_schema": {},
            }
            for spec in DEFAULT_MCP_SPECS
        ],
    }
