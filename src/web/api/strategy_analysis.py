from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.web.api.chat import _get_ai_client
from src.core.signals.structured_output import try_extract_tagged_json
from src.web.database import get_db
from src.web.models import (
    AppSettings,
    ChatConversation,
    Position,
    Stock,
    StrategyAnalysisPoolItem,
    StrategyAnalysisResult,
    StrategyPrompt,
)

logger = logging.getLogger(__name__)
router = APIRouter()

# 默认策略来源文档（首次自动灌入为「默认策略」）
_DEFAULT_STRATEGY_DOC = (
    Path(__file__).resolve().parents[3] / "docs" / "strategies" / "breakout-prev-high.md"
)
_DEFAULT_STRATEGY_NAME = "趋势突破前高有效性"
_DEFAULT_STRATEGY_DESC = "二次筛选：判断观察池里的突破是否已被市场确认有效"


# --------------------------------------------------------------------------- #
# 序列化
# --------------------------------------------------------------------------- #
def _iso(dt) -> str:
    if not dt:
        return ""
    try:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat(timespec="seconds")
    except Exception:
        return str(dt)


def _strategy_row(row: StrategyPrompt) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description or "",
        "prompt": row.prompt or "",
        "is_default": bool(row.is_default),
        "enabled": bool(row.enabled),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _pool_row(row: StrategyAnalysisPoolItem) -> dict:
    return {
        "id": row.id,
        "symbol": row.symbol,
        "market": row.market or "CN",
        "name": row.name or row.symbol,
        "source": row.source or "manual",
        "note": row.note or "",
        "tags": row.tags or {},
        "tags_updated_at": _iso(row.tags_updated_at),
        "created_at": _iso(row.created_at),
    }


def _result_row(row: StrategyAnalysisResult) -> dict:
    return {
        "id": row.id,
        "strategy_id": row.strategy_id,
        "strategy_name": row.strategy_name or "",
        "symbol": row.symbol,
        "market": row.market or "CN",
        "name": row.name or row.symbol,
        "verdict": row.verdict or "",
        "content": row.content or "",
        "model": row.model or "",
        "created_at": _iso(row.created_at),
    }


def _ensure_default_strategy(db: Session) -> None:
    """池策略表为空时，用文档灌入一条默认策略。"""
    if db.query(StrategyPrompt).count() > 0:
        return
    try:
        prompt_text = _DEFAULT_STRATEGY_DOC.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        logger.warning("读取默认策略文档失败：%s", e)
        prompt_text = (
            "你是一名严谨的 A 股技术分析师。请基于给定行情数据，"
            "判断该股票当前是否符合「趋势突破前高且突破有效」的选股标准，给出明确结论与理由。"
        )
    row = StrategyPrompt(
        name=_DEFAULT_STRATEGY_NAME,
        description=_DEFAULT_STRATEGY_DESC,
        prompt=prompt_text,
        is_default=True,
        enabled=True,
    )
    db.add(row)
    db.commit()


# --------------------------------------------------------------------------- #
# 策略提示词 CRUD
# --------------------------------------------------------------------------- #
class StrategyIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    description: str = ""
    prompt: str = Field(..., min_length=1)
    enabled: bool = True


@router.get("/strategies")
def list_strategies(db: Session = Depends(get_db)):
    _ensure_default_strategy(db)
    rows = (
        db.query(StrategyPrompt)
        .order_by(StrategyPrompt.is_default.desc(), StrategyPrompt.updated_at.desc())
        .all()
    )
    return {"items": [_strategy_row(r) for r in rows]}


@router.post("/strategies")
def create_strategy(payload: StrategyIn, db: Session = Depends(get_db)):
    row = StrategyPrompt(
        name=payload.name.strip(),
        description=payload.description.strip(),
        prompt=payload.prompt,
        enabled=payload.enabled,
        is_default=False,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _strategy_row(row)


@router.put("/strategies/{strategy_id}")
def update_strategy(strategy_id: int, payload: StrategyIn, db: Session = Depends(get_db)):
    row = db.query(StrategyPrompt).filter(StrategyPrompt.id == strategy_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="策略不存在")
    row.name = payload.name.strip()
    row.description = payload.description.strip()
    row.prompt = payload.prompt
    row.enabled = payload.enabled
    db.commit()
    db.refresh(row)
    return _strategy_row(row)


@router.delete("/strategies/{strategy_id}")
def delete_strategy(strategy_id: int, db: Session = Depends(get_db)):
    row = db.query(StrategyPrompt).filter(StrategyPrompt.id == strategy_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="策略不存在")
    if row.is_default:
        raise HTTPException(status_code=400, detail="默认策略不可删除，可直接编辑其内容")
    db.delete(row)
    db.commit()
    return {"ok": True}


# --------------------------------------------------------------------------- #
# 策略池
# --------------------------------------------------------------------------- #
class PoolItemIn(BaseModel):
    symbol: str = Field(..., min_length=1)
    market: str = "CN"
    name: str = ""
    note: str = ""


@router.get("/pool")
def list_pool(db: Session = Depends(get_db)):
    rows = (
        db.query(StrategyAnalysisPoolItem)
        .order_by(StrategyAnalysisPoolItem.created_at.desc())
        .all()
    )
    return {"items": [_pool_row(r) for r in rows]}


def _add_pool_item(db: Session, symbol: str, market: str, name: str, source: str, note: str = "") -> bool:
    """加入池子，返回是否新增（已存在则跳过）。"""
    symbol = symbol.strip()
    market = (market or "CN").strip().upper()
    if not symbol:
        return False
    exists = (
        db.query(StrategyAnalysisPoolItem)
        .filter(
            StrategyAnalysisPoolItem.symbol == symbol,
            StrategyAnalysisPoolItem.market == market,
        )
        .first()
    )
    if exists:
        return False
    db.add(
        StrategyAnalysisPoolItem(
            symbol=symbol,
            market=market,
            name=name or symbol,
            source=source,
            note=note,
        )
    )
    return True


@router.post("/pool")
def add_pool_item(payload: PoolItemIn, db: Session = Depends(get_db)):
    created = _add_pool_item(
        db, payload.symbol, payload.market, payload.name, "manual", payload.note
    )
    db.commit()
    return {"created": int(created)}


@router.post("/pool/import-positions")
def import_positions(db: Session = Depends(get_db)):
    """从实盘持仓一键导入到策略池（已存在的跳过）。"""
    positions = db.query(Position).all()
    created = 0
    seen: set[tuple[str, str]] = set()
    for pos in positions:
        stock: Stock | None = pos.stock
        if not stock or not stock.symbol:
            continue
        key = (stock.market or "CN", stock.symbol)
        if key in seen:
            continue
        seen.add(key)
        if _add_pool_item(db, stock.symbol, stock.market or "CN", stock.name or "", "position"):
            created += 1
    db.commit()
    return {"created": created, "scanned": len(seen)}


@router.delete("/pool/{item_id}")
def delete_pool_item(item_id: int, db: Session = Depends(get_db)):
    row = db.query(StrategyAnalysisPoolItem).filter(StrategyAnalysisPoolItem.id == item_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="池内股票不存在")
    db.delete(row)
    db.commit()
    return {"ok": True}


# --------------------------------------------------------------------------- #
# 行情上下文构建（当天 + 最近行情）
# --------------------------------------------------------------------------- #
async def _build_market_context(symbol: str, market: str, kline_days: int = 60) -> str:
    """构建喂给 AI 的行情上下文：实时 quote + 最近 N 根日K + 技术摘要。"""
    from src.collectors.kline_collector import KlineCollector
    from src.models.market import MarketCode

    mc = MarketCode(market) if market in ("CN", "HK", "US") else MarketCode.CN
    parts: list[str] = []

    # 实时行情（当天）
    try:
        from src.collectors.akshare_collector import _fetch_tencent_quotes, _tencent_symbol

        tsym = _tencent_symbol(symbol, mc)
        rows = await asyncio.to_thread(_fetch_tencent_quotes, [tsym])
        if rows:
            q = rows[0]
            parts.append(
                "## 当天实时行情\n"
                f"- 名称：{q.get('name', symbol)}\n"
                f"- 现价：{q.get('current_price', '--')}\n"
                f"- 涨跌幅：{q.get('change_pct', '--')}%\n"
                f"- 今开/昨收：{q.get('open_price', '--')} / {q.get('prev_close', '--')}\n"
                f"- 最高/最低：{q.get('high_price', '--')} / {q.get('low_price', '--')}\n"
                f"- 成交量：{q.get('volume', '--')}\n"
                f"- 成交额：{q.get('turnover', '--')}"
            )
    except Exception as e:  # noqa: BLE001
        logger.debug("实时行情获取失败 %s: %s", symbol, e)

    collector = KlineCollector(mc)

    # 技术摘要（趋势/MA/支撑压力等）
    try:
        summary = await asyncio.to_thread(collector.get_kline_summary, symbol)
        s = (summary or {}).get("summary") or summary or {}
        if s and not (summary or {}).get("error"):
            parts.append(
                "## 技术摘要\n"
                f"- 趋势：{s.get('trend', '--')}\n"
                f"- MACD：{s.get('macd_status', '--')}\n"
                f"- RSI14：{s.get('rsi_14', s.get('rsi6', '--'))}\n"
                f"- 支撑位：{s.get('support_level', '--')}\n"
                f"- 压力位：{s.get('resistance_level', '--')}"
            )
    except Exception as e:  # noqa: BLE001
        logger.debug("技术摘要获取失败 %s: %s", symbol, e)

    # 最近 N 根日K
    try:
        klines = await asyncio.to_thread(collector.get_klines, symbol, kline_days + 10)
        klines = klines[-kline_days:] if klines else []
        if klines:
            lines = ["## 最近日K（日期 开 高 低 收 成交量）"]
            for k in klines:
                lines.append(
                    f"{k.date} {k.open} {k.high} {k.low} {k.close} {int(k.volume) if k.volume else 0}"
                )
            parts.append("\n".join(lines))
    except Exception as e:  # noqa: BLE001
        logger.debug("日K获取失败 %s: %s", symbol, e)

    if not parts:
        return ""
    return "\n\n".join(parts)


def _extract_verdict(content: str) -> str:
    """从 AI 正文里抽取一句话结论。"""
    for line in (content or "").splitlines():
        line = line.strip().lstrip("#").strip()
        if not line:
            continue
        for tag in ("【结论】", "结论：", "结论:", "判定：", "判定:"):
            if line.startswith(tag):
                return line[len(tag):].strip()[:120]
        # 首个非空行兜底
        return line.replace("**", "")[:120]
    return ""


# --------------------------------------------------------------------------- #
# 逐票分析
# --------------------------------------------------------------------------- #
class AnalyzeIn(BaseModel):
    strategy_id: int
    symbol: str = Field(..., min_length=1)
    market: str = "CN"
    name: str = ""
    kline_days: int = 60


@router.post("/analyze")
async def analyze_one(payload: AnalyzeIn, db: Session = Depends(get_db)):
    """对单只股票用指定策略做一次 AI 分析（逐票调用）。"""
    strategy = (
        db.query(StrategyPrompt).filter(StrategyPrompt.id == payload.strategy_id).first()
    )
    if not strategy:
        raise HTTPException(status_code=404, detail="策略不存在")

    market = (payload.market or "CN").upper()
    days = max(20, min(int(payload.kline_days or 60), 120))
    market_context = await _build_market_context(payload.symbol, market, days)
    if not market_context:
        raise HTTPException(status_code=502, detail="未能获取该股票的行情数据，稍后再试")

    name = payload.name or payload.symbol
    user_content = (
        f"# 待分析股票：{name}（{market}:{payload.symbol}）\n"
        f"分析时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
        f"{market_context}\n\n"
        "---\n"
        "请严格依据上述策略，对该股票当前状态给出判断。\n"
        "输出要求：第一行用 `【结论】xxx` 给出一句话明确结论"
        "（如：有效突破/突破待确认/突破失败/不符合/观望），随后分点说明依据（量价、点位、时间）。"
    )

    ai_client = _get_ai_client(db)
    try:
        content = await ai_client.chat(strategy.prompt, user_content, temperature=0.3)
    except Exception as e:  # noqa: BLE001
        logger.error("策略 AI 分析失败 %s: %s", payload.symbol, e)
        raise HTTPException(status_code=502, detail=f"AI 分析失败：{e}") from e

    verdict = _extract_verdict(content)
    row = StrategyAnalysisResult(
        strategy_id=strategy.id,
        strategy_name=strategy.name,
        symbol=payload.symbol,
        market=market,
        name=name,
        verdict=verdict,
        content=content,
        model=getattr(ai_client, "model", ""),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _result_row(row)


@router.get("/results")
def list_results(strategy_id: int | None = None, limit: int = 100, db: Session = Depends(get_db)):
    """返回每只股票的最新分析结果。"""
    query = db.query(StrategyAnalysisResult)
    if strategy_id:
        query = query.filter(StrategyAnalysisResult.strategy_id == strategy_id)
    rows = (
        query.order_by(StrategyAnalysisResult.created_at.desc())
        .limit(max(1, min(int(limit or 100), 500)))
        .all()
    )
    latest: dict[tuple[str, str], dict] = {}
    for r in rows:
        key = (r.market or "CN", r.symbol)
        if key not in latest:
            latest[key] = _result_row(r)
    return {"items": list(latest.values())}


@router.get("/last-conversations")
def last_conversations(strategy_id: int, db: Session = Depends(get_db)):
    """返回该策略下每只股票最近一次策略对话（用于「查看上次分析」）。

    key 形如 "CN:600118" → {conversation_id, updated_at, title}
    """
    rows = (
        db.query(ChatConversation)
        .filter(
            ChatConversation.strategy_id == strategy_id,
            ChatConversation.stock_symbol.isnot(None),
        )
        .order_by(ChatConversation.updated_at.desc(), ChatConversation.id.desc())
        .all()
    )
    latest: dict[str, dict] = {}
    for c in rows:
        key = f"{(c.stock_market or 'CN').upper()}:{c.stock_symbol}"
        if key not in latest:
            latest[key] = {
                "conversation_id": c.id,
                "updated_at": _iso(c.updated_at),
                "title": c.title or "",
            }
    return {"items": latest}


# --------------------------------------------------------------------------- #
# 池子总览：汇总各票结论并排序
# --------------------------------------------------------------------------- #
class OverviewIn(BaseModel):
    strategy_id: int


def _overview_key(strategy_id: int) -> str:
    return f"strategy_overview:{strategy_id}"


def _load_overview(db: Session, strategy_id: int) -> dict | None:
    row = (
        db.query(AppSettings)
        .filter(AppSettings.key == _overview_key(strategy_id))
        .first()
    )
    if not row or not row.value:
        return None
    try:
        return json.loads(row.value)
    except Exception:
        return None


def _save_overview(db: Session, strategy_id: int, payload: dict) -> None:
    key = _overview_key(strategy_id)
    val = json.dumps(payload, ensure_ascii=False)
    row = db.query(AppSettings).filter(AppSettings.key == key).first()
    if row:
        row.value = val
    else:
        db.add(AppSettings(key=key, value=val, description="策略池总览排序快照"))
    db.commit()


def _tags_brief(name: str, symbol: str, market: str, tags: dict) -> str:
    t = tags or {}

    def g(k, dflt="—"):
        v = t.get(k)
        return dflt if v is None or v == "" else v

    return (
        f"{name}（{market}:{symbol}）｜突破有效性={g('breakout')}｜"
        f"距前高={g('gap_to_prev_high_pct')}%｜前高={g('prev_high')}｜支撑={g('support')}｜"
        f"回踩支撑={g('pullback_support')}｜量价={g('volume_confirm')}｜"
        f"建议={g('action_label') or g('action')}｜理由={g('reason')}"
    )


@router.post("/overview")
async def overview(payload: OverviewIn, db: Session = Depends(get_db)):
    """把池内各票的最近结论汇总给 AI，按当前吸引力从高到低排序。"""
    strategy = (
        db.query(StrategyPrompt).filter(StrategyPrompt.id == payload.strategy_id).first()
    )
    if not strategy:
        raise HTTPException(status_code=404, detail="策略不存在")

    items = (
        db.query(StrategyAnalysisPoolItem)
        .order_by(StrategyAnalysisPoolItem.created_at.desc())
        .all()
    )
    analyzed = [it for it in items if it.tags]
    unanalyzed = [
        {"symbol": it.symbol, "market": it.market or "CN", "name": it.name or it.symbol}
        for it in items
        if not it.tags
    ]
    if not analyzed:
        raise HTTPException(
            status_code=400, detail="策略池内暂无已分析的股票，请先对个股点「分析」生成结论"
        )

    by_key = {f"{(it.market or 'CN').upper()}:{it.symbol}": it for it in analyzed}
    brief_lines = [
        f"{i}. " + _tags_brief(it.name or it.symbol, it.symbol, it.market or "CN", it.tags or {})
        for i, it in enumerate(analyzed, 1)
    ]
    user_content = (
        "以下是策略池内各股票最近一次的分析结论：\n"
        + "\n".join(brief_lines)
        + "\n\n请依据上述策略标准，把这些股票按「当前买入/持有吸引力」从高到低排序，"
        "综合考虑突破有效性、量价确认、回踩支撑与位置。\n"
        "先用中文写一段总体点评，然后在末尾追加结构化排序（HTML 注释，用户看不到），格式严格如下：\n"
        "<!--PANWATCH_JSON-->\n"
        '{"summary": "一句话总览", "ranking": [{"symbol": "代码", "market": "CN", "score": 0-100整数, "reason": "一句话"}]}\n'
        "<!--/PANWATCH_JSON-->\n"
        "ranking 必须覆盖上面所有股票，best 在前。"
    )

    ai_client = _get_ai_client(db)
    try:
        content = await ai_client.chat(strategy.prompt, user_content, temperature=0.3)
    except Exception as e:  # noqa: BLE001
        logger.error("池子总览分析失败: %s", e)
        raise HTTPException(status_code=502, detail=f"AI 分析失败：{e}") from e

    parsed = try_extract_tagged_json(content) or {}
    ranking_raw = parsed.get("ranking") if isinstance(parsed, dict) else None
    summary = (parsed.get("summary") if isinstance(parsed, dict) else "") or ""

    ranked: list[dict] = []
    seen: set[str] = set()
    for r in ranking_raw or []:
        if not isinstance(r, dict):
            continue
        sym = str(r.get("symbol") or "").strip()
        mkt = str(r.get("market") or "CN").strip().upper()
        key = f"{mkt}:{sym}"
        it = by_key.get(key)
        if not it or key in seen:
            continue
        seen.add(key)
        ranked.append(
            {
                "rank": len(ranked) + 1,
                "symbol": it.symbol,
                "market": it.market or "CN",
                "name": it.name or it.symbol,
                "score": r.get("score"),
                "reason": str(r.get("reason") or ""),
                "tags": it.tags or {},
            }
        )
    # AI 未覆盖的已分析股票，兜底追加到末尾
    for it in analyzed:
        key = f"{(it.market or 'CN').upper()}:{it.symbol}"
        if key in seen:
            continue
        ranked.append(
            {
                "rank": len(ranked) + 1,
                "symbol": it.symbol,
                "market": it.market or "CN",
                "name": it.name or it.symbol,
                "score": None,
                "reason": "",
                "tags": it.tags or {},
            }
        )

    result = {
        "summary": summary or content.strip()[:400],
        "ranked": ranked,
        "unanalyzed": unanalyzed,
        "model": getattr(ai_client, "model", ""),
        "analyzed_at": _iso(datetime.now(timezone.utc)),
    }
    _save_overview(db, strategy.id, result)
    return result


@router.get("/overview")
def get_overview(strategy_id: int, db: Session = Depends(get_db)):
    """读取该策略最近一次的总览排序快照（不触发 AI）。"""
    cached = _load_overview(db, strategy_id)
    if cached is None:
        return {"summary": "", "ranked": [], "unanalyzed": [], "model": "", "analyzed_at": ""}
    return cached
