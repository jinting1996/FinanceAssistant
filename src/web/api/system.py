from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter

from src.core.providers import (
    get_capital_flow_orchestrator,
    get_chart_orchestrator,
    get_discovery_orchestrator,
    get_events_orchestrator,
    get_kline_orchestrator,
    get_news_orchestrator,
    get_quote_orchestrator,
)

router = APIRouter()


def _status(snapshot: dict) -> str:
    count = int(snapshot.get("count") or 0)
    success_rate = snapshot.get("success_rate")
    if count <= 0:
        return "unknown"
    if success_rate is None:
        return "unknown"
    if float(success_rate) >= 0.8:
        return "healthy"
    if float(success_rate) >= 0.5:
        return "degraded"
    return "down"


def _format_ts(value) -> str | None:
    try:
        ts = float(value or 0)
    except Exception:
        return None
    if ts <= 0:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _orchestrator_payload(name: str, orch) -> dict:
    metrics = []
    snapshots = orch.metrics_snapshot()
    for provider in sorted(orch.registered_providers()):
        snapshot = dict(snapshots.get(provider) or {"count": 0, "success_rate": None, "p50_latency_ms": None})
        snapshot["last_success_at"] = _format_ts(snapshot.get("last_success_at"))
        metrics.append(
            {
                "provider": provider,
                "status": _status(snapshot),
                **snapshot,
            }
        )
    return {
        "type": name,
        "providers": metrics,
    }


@router.get("/datasource-health")
def datasource_health():
    """Return in-memory provider health metrics collected by orchestrators."""

    orchestrators = [
        ("quote", get_quote_orchestrator()),
        ("kline", get_kline_orchestrator()),
        ("capital_flow", get_capital_flow_orchestrator()),
        ("events", get_events_orchestrator()),
        ("discovery", get_discovery_orchestrator()),
        ("chart", get_chart_orchestrator()),
        ("news", get_news_orchestrator()),
    ]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sources": [_orchestrator_payload(name, orch) for name, orch in orchestrators],
    }
