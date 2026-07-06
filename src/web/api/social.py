"""社交媒体舆论 API - X/Twitter 情感分析"""

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.web.database import get_db
from src.web.models import Stock
from src.config import Settings

logger = logging.getLogger(__name__)

router = APIRouter()


class SocialItemResponse(BaseModel):
    source: str
    external_id: str
    username: str
    display_name: str
    content: str
    publish_time: str = ""
    symbols: list[str] = Field(default_factory=list)
    metrics: dict[str, int] = Field(default_factory=dict)
    url: str = ""
    sentiment: str = ""
    sentiment_score: float = 0.0
    sentiment_reasoning: str = ""
    engagement: int = 0


class SentimentSummaryResponse(BaseModel):
    symbol: str
    name: str = ""
    total_posts: int
    positive_count: int = 0
    negative_count: int = 0
    neutral_count: int = 0
    avg_score: float = 0.0
    sentiment_label: str = "no_data"
    positive_ratio: float = 0.0
    negative_ratio: float = 0.0
    analyzed_at: str = ""
    top_posts: list[SocialItemResponse] = Field(default_factory=list)


class SocialSentimentRequest(BaseModel):
    symbols: list[str] = Field(default_factory=list)
    count: int = 20


@router.get("/status")
def get_social_status():
    """检查 X 舆论监控配置状态"""
    settings = Settings()
    return {
        "configured": bool(settings.x_username and settings.x_email and settings.x_password),
        "sentiment_enabled": settings.social_sentiment_enabled,
        "ai_configured": bool(settings.ai_api_key),
    }


@router.get("/sentiment", response_model=list[SentimentSummaryResponse])
async def get_social_sentiment(
    symbols: str = Query(
        default="",
        description="股票代码，逗号分隔，空则使用全部自选股",
    ),
    count: int = Query(default=20, ge=1, le=100, description="每只股票采集帖子数"),
    db: Session = Depends(get_db),
):
    """
    获取 X/Twitter 社交舆论情感分析

    - symbols: 股票代码过滤，逗号分隔，空则使用全部自选股
    - count: 每只股票采集的帖子数量
    """
    settings = Settings()
    if not settings.x_username:
        return []

    # 解析股票代码
    if symbols:
        symbol_list = [s.strip() for s in symbols.split(",") if s.strip()]
    else:
        symbol_list = [s.symbol for s in db.query(Stock).all()]

    if not symbol_list:
        return []

    # 获取股票名称映射
    stock_map = {s.symbol: s.name for s in db.query(Stock).all()}

    from src.core.data_collector import get_collector_manager

    manager = get_collector_manager()
    result = await manager.collect_social(symbols=symbol_list, count=count)

    if not result.success:
        logger.warning(f"社交舆论采集失败: {result.error}")
        return [
            SentimentSummaryResponse(
                symbol=s,
                name=stock_map.get(s, s),
                total_posts=0,
            )
            for s in symbol_list
        ]

    summaries = result.data.get("summaries", []) if result.data else []
    response = []

    for s in summaries:
        top_posts = [
            SocialItemResponse(
                source=p.source,
                external_id=p.external_id,
                username=p.username,
                display_name=p.display_name,
                content=p.content[:200],
                publish_time=p.publish_time.strftime("%Y-%m-%d %H:%M") if p.publish_time else "",
                symbols=p.symbols,
                metrics=p.metrics,
                url=p.url,
                sentiment=p.sentiment,
                sentiment_score=p.sentiment_score,
                sentiment_reasoning=p.sentiment_reasoning,
                engagement=p.engagement,
            )
            for p in s.top_posts
        ]

        response.append(
            SentimentSummaryResponse(
                symbol=s.symbol,
                name=stock_map.get(s.symbol, s.symbol),
                total_posts=s.total_posts,
                positive_count=s.positive_count,
                negative_count=s.negative_count,
                neutral_count=s.neutral_count,
                avg_score=s.avg_score,
                sentiment_label=s.sentiment_label,
                positive_ratio=s.positive_ratio,
                negative_ratio=s.negative_ratio,
                analyzed_at=s.analyzed_at.strftime("%Y-%m-%d %H:%M") if s.analyzed_at else "",
                top_posts=top_posts,
            )
        )

    return response


@router.post("/collect")
async def collect_social_sentiment(
    request: SocialSentimentRequest,
    db: Session = Depends(get_db),
):
    """
    手动触发 X/Twitter 舆论采集

    - symbols: 股票代码列表，空则使用全部自选股
    - count: 每只股票采集的帖子数量
    """
    settings = Settings()
    if not settings.x_username:
        return {
            "test_passed": False,
            "error": "X 账号未配置，请在 .env 中设置 X_USERNAME / X_EMAIL / X_PASSWORD",
        }

    symbol_list = request.symbols or [s.symbol for s in db.query(Stock).all()]
    if not symbol_list:
        return {"test_passed": False, "error": "没有自选股"}

    from src.core.data_collector import get_collector_manager

    manager = get_collector_manager()
    manager.clear_logs()
    result = await manager.collect_social(symbols=symbol_list, count=request.count)

    return {
        "test_passed": result.success,
        "count": result.count,
        "duration_ms": result.duration_ms,
        "error": result.error,
        "formatted": result.data.get("formatted", "") if result.data else "",
        "logs": manager.get_logs(),
    }


@router.get("/formatted")
async def get_social_sentiment_text(
    symbols: str = Query(
        default="",
        description="股票代码，逗号分隔，空则使用全部自选股",
    ),
    count: int = Query(default=20, ge=1, le=100, description="每只股票采集帖子数"),
    db: Session = Depends(get_db),
):
    """
    获取格式化后的社交媒体舆论文本（供 Agent 或用户阅读）

    返回 Markdown 格式的情感分析报告
    """
    settings = Settings()
    if not settings.x_username:
        return {"text": "X 舆论监控未配置", "configured": False}

    if symbols:
        symbol_list = [s.strip() for s in symbols.split(",") if s.strip()]
    else:
        symbol_list = [s.symbol for s in db.query(Stock).all()]

    if not symbol_list:
        return {"text": "没有自选股", "configured": True}

    from src.core.data_collector import get_collector_manager

    manager = get_collector_manager()
    result = await manager.collect_social(symbols=symbol_list, count=count)

    if not result.success:
        return {"text": f"采集失败: {result.error}", "configured": True}

    return {
        "text": result.data.get("formatted", "") if result.data else "",
        "configured": True,
        "total_posts": result.count,
        "duration_ms": result.duration_ms,
    }