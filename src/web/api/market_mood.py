"""大盘情绪与板块资金流 API。"""

import asyncio

from fastapi import APIRouter

from src.core.market_mood import get_market_mood

router = APIRouter()


@router.get("")
async def market_mood(top_n: int = 5, refresh: bool = False) -> dict:
    """获取大盘情绪评估与行业板块主力资金流入流出(5分钟缓存)。"""
    top_n = max(1, min(10, top_n))
    return await asyncio.to_thread(get_market_mood, top_n=top_n, force_refresh=refresh)
