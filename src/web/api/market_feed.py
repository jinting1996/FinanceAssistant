"""Unified market news/feed API used by news, events, and strategy factors."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query

from src.web.database import SessionLocal
from src.web.models import NewsCache

router = APIRouter()

SOURCE_LABELS = {
    "eastmoney_news": "东方财富资讯",
    "eastmoney": "东方财富公告",
    "newsnow": "NewsNow 财经快讯",
    "ifind": "iFinD",
    "tdx_mcp": "通达信 MCP",
}


def _iso(dt) -> str:
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat(timespec="seconds")


def _source_type(source: str) -> str:
    if source == "eastmoney":
        return "announcement"
    if source == "newsnow":
        return "flash"
    if source in {"ifind", "tdx_mcp"}:
        return "mcp"
    return "news"


def _feed_item(*, source: str, title: str, content: str, publish_time, symbols, url: str = "", raw=None) -> dict:
    return {
        "source": source,
        "source_label": SOURCE_LABELS.get(source, source),
        "source_type": _source_type(source),
        "title": title,
        "content": content or "",
        "publish_time": _iso(publish_time),
        "symbols": symbols or [],
        "boards": [],
        "importance": 0,
        "sentiment": "neutral",
        "url": url,
        "raw_payload": raw or {},
    }


@router.get("")
async def market_feed(
    market: str = Query("CN", description="市场代码"),
    since_hours: int = Query(24, ge=1, le=168),
    limit: int = Query(80, ge=1, le=300),
):
    since = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    items: list[dict] = []

    if (market or "CN").upper() == "CN":
        try:
            from src.web.api.market_events import _fetch_newsnow_news

            newsnow = await _fetch_newsnow_news(since=since, limit=min(limit, 80))
            for n in newsnow:
                items.append(
                    _feed_item(
                        source="newsnow",
                        title=n.title,
                        content=n.content,
                        publish_time=n.publish_time,
                        symbols=n.symbols,
                        url=n.url,
                        raw={"external_id": n.external_id},
                    )
                )
        except Exception:
            pass

    db = SessionLocal()
    try:
        rows = (
            db.query(NewsCache)
            .filter(NewsCache.publish_time >= since.replace(tzinfo=None))
            .order_by(NewsCache.publish_time.desc())
            .limit(limit)
            .all()
        )
        for row in rows:
            items.append(
                _feed_item(
                    source=row.source,
                    title=row.title,
                    content=row.content,
                    publish_time=row.publish_time,
                    symbols=row.symbols or [],
                    raw={"external_id": row.external_id, "importance": row.importance},
                )
            )
    finally:
        db.close()

    items.sort(key=lambda x: x.get("publish_time") or "", reverse=True)
    return {
        "market": (market or "CN").upper(),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "items": items[:limit],
        "coverage": {
            "count": min(len(items), limit),
            "sources": sorted({x.get("source") for x in items[:limit] if x.get("source")}),
        },
    }

