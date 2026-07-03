"""AI 对话 API 端点。"""

import asyncio
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.config import Settings
from src.core.ai_client import AIClient
from src.models.market import MarketCode
from src.web.database import SessionLocal, get_db
from src.web.models import (
    AIModel,
    AIService,
    AnalysisHistory,
    ChatConversation,
    ChatMessage,
    PaperTradingPosition,
    Position,
    Stock,
    StockSuggestion,
    StrategyAnalysisPoolItem,
    StrategyPrompt,
)
from src.core.signals.structured_output import (
    reconcile_breakout_tag,
    strip_tagged_json,
    try_extract_tagged_json,
)

logger = logging.getLogger(__name__)
router = APIRouter()

SYSTEM_PROMPT = """你是 PanWatch 的 AI 投资助手。

你可以使用工具获取用户的投资数据。当用户的问题涉及具体数据时，主动调用工具获取，不要让用户自己提供。

规则：
- 需要数据时主动调用工具，不要反问用户要数据
- 基于工具返回的实时数据回答，不编造价格等具体数据
- 给出明确的观点和理由
- 涉及买卖建议时说明风险
- 用中文回答
- 保持简洁，避免冗余"""

MAX_HISTORY_MESSAGES = 20
MAX_TOOL_ROUNDS = 5

# 策略对话结束时附带的结构化标签，用于把关键信息标在股票上（用户不可见的 HTML 注释）
STRATEGY_TAG_INSTRUCTION = """在你正常的中文分析之后，务必在回复最末尾追加一段结构化标签（HTML 注释，用户看不到），格式严格如下：
<!--PANWATCH_JSON-->
{"prev_high": 数字或null, "breakout": "valid|pending|failed|expired|none", "gap_to_prev_high_pct": 数字或null, "support": 数字或null, "pullback_support": true或false, "volume_confirm": "strong|weak|neutral|none", "score": 整数或null, "status": "字符串或null", "event_age": 整数或null, "d0_vol_ratio": 数字或null, "blue_chip": true或false或null, "action": "buy|add|reduce|sell|hold|watch|avoid", "action_label": "建仓|加仓|减仓|清仓|持有|观望|回避", "reason": "一句话理由"}
<!--/PANWATCH_JSON-->
字段含义：prev_high=突破锚点前高价；breakout=突破有效性(valid有效/pending待确认/failed失败/expired已过期/none不符合)，此项必须与你正文的一句话结论保持一致（结论说"有效突破"就填 valid，说"待确认"才填 pending）；gap_to_prev_high_pct=现价相对前高的百分比(高于为正)；support=关键支撑位；pullback_support=是否已回踩到支撑附近；volume_confirm=量价确认强度；score=该股在本策略下的综合评分整数（即你评分卡算出的总分；若策略无评分体系、或该股未达可评分状态则给 null，不要编造）；status=该股当前状态标签（如 ValidActive/Pending/Failed/Expired/Extended/Invalidated 等，没有则 null）；event_age=事件年龄即距突破日的交易日数（用于排序平手裁决，无则 null）；d0_vol_ratio=突破日量比 V0/V_BASE（平手裁决用，无则 null）；blue_chip=是否沪深300成份股（平手裁决用，无法确定给 null）；action/action_label=对该持仓的操作建议。数值用数字，不确定用 null，不要编造。"""


def _apply_strategy_tags(db: Session, symbol: str, market: str, tags: dict) -> None:
    """把策略分析抽取到的标签写回策略池对应股票。"""
    if not tags:
        return
    item = (
        db.query(StrategyAnalysisPoolItem)
        .filter(
            StrategyAnalysisPoolItem.symbol == symbol,
            StrategyAnalysisPoolItem.market == (market or "CN").upper(),
        )
        .first()
    )
    if not item:
        return  # 不在策略池里（如从别处开的对话）则不落标签
    item.tags = tags
    item.tags_updated_at = datetime.now()

# ──────────────── Tool Definitions ────────────────

CHAT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_portfolio",
            "description": "获取用户的实盘持仓和模拟盘持仓。用于回答持仓相关问题（持仓健康吗、该调仓吗、盈亏情况等）。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_stock_quote",
            "description": "获取某只股票的实时行情（价格、涨跌幅、成交量等）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "股票代码，如 600519"},
                    "market": {"type": "string", "description": "市场代码：CN/HK/US", "default": "CN"},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_technical_analysis",
            "description": "获取股票的技术面分析（趋势、MACD、RSI、支撑位、压力位等）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "股票代码"},
                    "market": {"type": "string", "description": "市场代码：CN/HK/US", "default": "CN"},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_stock_suggestions",
            "description": "获取某只股票最近的 AI 建议和分析报告。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "股票代码"},
                    "market": {"type": "string", "description": "市场代码：CN/HK/US", "default": "CN"},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_watchlist",
            "description": "获取用户的自选股（关注列表）。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


def _build_watchlist_context(db: Session) -> str:
    """构建用户自选股列表。"""
    stocks = db.query(Stock).order_by(Stock.sort_order.asc()).all()
    if not stocks:
        return "用户暂无自选股。"
    lines = [f"- {s.name}({s.market}:{s.symbol})" for s in stocks]
    return "自选股列表：\n" + "\n".join(lines)


async def _execute_tool(db: Session, name: str, args: dict) -> str:
    """执行工具调用，返回结果文本。"""
    try:
        if name == "get_portfolio":
            result = _build_portfolio_context(db)
            return result or "用户暂无持仓。"
        elif name == "get_stock_quote":
            symbol = args.get("symbol", "")
            market = args.get("market", "CN")
            result = await _fetch_realtime_context(symbol, market)
            return result or f"未能获取 {market}:{symbol} 的行情数据。"
        elif name == "get_technical_analysis":
            symbol = args.get("symbol", "")
            market = args.get("market", "CN")
            result = await _fetch_technical_context(symbol, market)
            return result or f"未能获取 {market}:{symbol} 的技术面数据。"
        elif name == "get_stock_suggestions":
            symbol = args.get("symbol", "")
            market = args.get("market", "CN")
            result = _build_stock_context(db, symbol, market)
            return result or f"暂无 {market}:{symbol} 的 AI 建议。"
        elif name == "get_watchlist":
            return _build_watchlist_context(db)
        else:
            return f"未知工具: {name}"
    except Exception as e:
        logger.error(f"工具执行失败 {name}: {e}")
        return f"工具执行出错: {e}"


class CreateConversationBody(BaseModel):
    stock_symbol: str | None = None
    stock_market: str | None = None
    initial_context: str | None = None
    strategy_id: int | None = None


class SendMessageBody(BaseModel):
    content: str


def _get_ai_client(db: Session, model_id: int | None = None) -> AIClient:
    """获取 AI 客户端实例。"""
    model = None
    service = None

    if model_id:
        model = db.query(AIModel).filter(AIModel.id == model_id).first()

    if not model:
        model = db.query(AIModel).filter(AIModel.is_default == True).first()  # noqa: E712

    if not model:
        model = db.query(AIModel).first()

    if model:
        service = db.query(AIService).filter(AIService.id == model.service_id).first()

    if model and service:
        return AIClient(
            base_url=service.base_url,
            api_key=service.api_key,
            model=model.model,
        )

    settings = Settings()
    return AIClient(
        base_url=settings.ai_base_url,
        api_key=settings.ai_api_key,
        model=settings.ai_model,
    )


def _build_stock_context(db: Session, symbol: str, market: str) -> str:
    """为绑定股票构建上下文摘要。"""
    parts = []

    # 最近建议
    suggestions = (
        db.query(StockSuggestion)
        .filter(
            StockSuggestion.stock_symbol == symbol,
            StockSuggestion.stock_market == market,
        )
        .order_by(StockSuggestion.created_at.desc())
        .limit(3)
        .all()
    )
    if suggestions:
        lines = []
        for s in suggestions:
            lines.append(f"- [{s.agent_label or s.agent_name}] {s.action_label}: {s.signal or s.reason or ''}")
        parts.append("最近 AI 建议：\n" + "\n".join(lines))

    # 最近分析报告
    histories = (
        db.query(AnalysisHistory)
        .filter(AnalysisHistory.stock_symbol == symbol)
        .order_by(AnalysisHistory.created_at.desc())
        .limit(1)
        .all()
    )
    if histories:
        h = histories[0]
        content_preview = (h.content or "")[:500]
        parts.append(f"最近分析（{h.agent_name}, {h.analysis_date}）：\n{content_preview}")

    if not parts:
        return ""
    return "\n\n".join(parts)


def _build_portfolio_context(db: Session) -> str:
    """构建用户全部持仓摘要。"""
    lines: list[str] = []

    # 实盘持仓
    positions = db.query(Position).all()
    if positions:
        real_lines = []
        for p in positions:
            stock = db.query(Stock).filter(Stock.id == p.stock_id).first()
            if not stock:
                continue
            real_lines.append(
                f"- {stock.name}({stock.market}:{stock.symbol}) "
                f"{p.quantity}股 成本{p.cost_price} 风格{p.trading_style or '波段'}"
            )
        if real_lines:
            lines.append("实盘持仓：\n" + "\n".join(real_lines))

    # 模拟盘持仓
    paper_positions = (
        db.query(PaperTradingPosition)
        .filter(PaperTradingPosition.status == "open")
        .all()
    )
    if paper_positions:
        paper_lines = []
        for pp in paper_positions:
            pnl_str = f"浮盈{pp.unrealized_pnl:.1f}" if pp.unrealized_pnl else ""
            paper_lines.append(
                f"- {pp.stock_name or pp.stock_symbol}({pp.stock_market}:{pp.stock_symbol}) "
                f"{pp.quantity}股 入场价{pp.entry_price}"
                f"{f' 止损{pp.stop_loss}' if pp.stop_loss else ''}"
                f"{f' 目标{pp.target_price}' if pp.target_price else ''}"
                f"{f' {pnl_str}' if pnl_str else ''}"
            )
        if paper_lines:
            lines.append("模拟盘持仓：\n" + "\n".join(paper_lines))

    if not lines:
        return ""
    return "\n\n".join(lines)


async def _fetch_realtime_context(symbol: str, market: str) -> str:
    """异步获取实时行情和技术面。"""
    try:
        from src.collectors.akshare_collector import _fetch_tencent_quotes, _tencent_symbol
        from src.models.market import MarketCode

        mc = MarketCode(market) if market in ("CN", "HK", "US") else MarketCode.CN
        tsym = _tencent_symbol(symbol, mc)
        rows = await asyncio.to_thread(_fetch_tencent_quotes, [tsym])
        if not rows:
            return ""
        q = rows[0]
        price = q.get("current_price", "--")
        change = q.get("change_pct", "--")
        volume = q.get("volume", "--")
        name = q.get("name", symbol)
        return f"实时行情：{name}（{market}:{symbol}）价格 {price}，涨跌幅 {change}%，成交量 {volume}"
    except Exception as e:
        logger.debug(f"获取实时行情失败: {e}")
        return ""


async def _fetch_technical_context(symbol: str, market: str) -> str:
    """获取技术面摘要。"""
    try:
        from src.core.data_collector import DataCollector

        collector = DataCollector()
        summary = await asyncio.to_thread(
            collector.get_kline_summary, symbol, market
        )
        if not summary or summary.get("error"):
            return ""
        s = summary.get("summary", {})
        trend = s.get("trend", "--")
        macd = s.get("macd_status", "--")
        rsi = s.get("rsi_14", "--")
        support = s.get("support_level", "--")
        resistance = s.get("resistance_level", "--")
        return f"技术面：趋势 {trend}，MACD {macd}，RSI {rsi}，支撑位 {support}，压力位 {resistance}"
    except Exception as e:
        logger.debug(f"获取技术面失败: {e}")
        return ""


@router.get("/suggested-questions")
def suggested_questions(
    symbol: str = Query(..., description="股票代码"),
    market: str = Query("CN", description="市场"),
    db: Session = Depends(get_db),
):
    """根据股票当前状态生成推荐问题（纯模板，不调 AI）。"""
    questions: list[str] = []

    # 查最近建议
    latest_suggestion = (
        db.query(StockSuggestion)
        .filter(
            StockSuggestion.stock_symbol == symbol,
            StockSuggestion.stock_market == market,
        )
        .order_by(StockSuggestion.created_at.desc())
        .first()
    )
    if latest_suggestion:
        action = (latest_suggestion.action or "").lower()
        label = latest_suggestion.action_label or latest_suggestion.action or ""
        if action in ("buy", "add"):
            questions.append(f"最新的「{label}」信号可靠吗？入场时机如何？")
        elif action in ("sell", "reduce"):
            questions.append(f"最新给出了「{label}」建议，现在该操作吗？")
        elif action == "alert":
            questions.append("最近的异动提醒是什么情况？需要关注吗？")

    # 查持仓（Position 通过 stock_id 关联 Stock 表）
    has_position = (
        db.query(Position)
        .join(Stock, Position.stock_id == Stock.id)
        .filter(Stock.symbol == symbol, Stock.market == market)
        .first()
    ) is not None
    if has_position:
        questions.append("当前持仓该继续持有还是考虑减仓？")
    else:
        questions.append("现在适合建仓吗？")

    # 通用问题
    questions.append("分析近期走势和关键支撑压力位")
    questions.append("有什么值得关注的消息或事件？")

    return {"questions": questions[:5]}


@router.post("/conversations")
def create_conversation(
    body: CreateConversationBody | None = None,
    db: Session = Depends(get_db),
):
    conv = ChatConversation(
        stock_symbol=body.stock_symbol if body else None,
        stock_market=body.stock_market if body else None,
        initial_context=body.initial_context if body else None,
        strategy_id=body.strategy_id if body else None,
    )
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return {
        "id": conv.id,
        "title": conv.title or "",
        "stock_symbol": conv.stock_symbol,
        "stock_market": conv.stock_market,
        "strategy_id": conv.strategy_id,
        "created_at": str(conv.created_at or ""),
    }


@router.get("/conversations")
def list_conversations(
    limit: int = Query(30, ge=1, le=100),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(ChatConversation)
        .order_by(ChatConversation.updated_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": c.id,
            "title": c.title or "",
            "stock_symbol": c.stock_symbol,
            "stock_market": c.stock_market,
            "strategy_id": c.strategy_id,
            "created_at": str(c.created_at or ""),
        }
        for c in rows
    ]


@router.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: int, db: Session = Depends(get_db)):
    conv = db.query(ChatConversation).filter(ChatConversation.id == conversation_id).first()
    if not conv:
        raise HTTPException(404, "对话不存在")
    messages = (
        db.query(ChatMessage)
        .filter(ChatMessage.conversation_id == conversation_id)
        .order_by(ChatMessage.created_at.asc())
        .all()
    )
    return {
        "conversation": {
            "id": conv.id,
            "title": conv.title or "",
            "stock_symbol": conv.stock_symbol,
            "stock_market": conv.stock_market,
            "strategy_id": conv.strategy_id,
            "created_at": str(conv.created_at or ""),
        },
        "messages": [
            {
                "id": m.id,
                "role": m.role,
                "content": m.content,
                "created_at": str(m.created_at or ""),
            }
            for m in messages
        ],
    }


@router.delete("/conversations/{conversation_id}")
def delete_conversation(conversation_id: int, db: Session = Depends(get_db)):
    conv = db.query(ChatConversation).filter(ChatConversation.id == conversation_id).first()
    if not conv:
        raise HTTPException(404, "对话不存在")
    db.query(ChatMessage).filter(ChatMessage.conversation_id == conversation_id).delete()
    db.delete(conv)
    db.commit()
    return {"ok": True}


@router.post("/conversations/{conversation_id}/messages")
async def send_message(
    conversation_id: int,
    body: SendMessageBody,
):
    """发送消息并获取 AI 回复。"""
    db = SessionLocal()
    try:
        conv = db.query(ChatConversation).filter(ChatConversation.id == conversation_id).first()
        if not conv:
            raise HTTPException(404, "对话不存在")

        # 保存用户消息
        user_msg = ChatMessage(
            conversation_id=conversation_id,
            role="user",
            content=body.content,
        )
        db.add(user_msg)

        # 更新对话标题（首条消息取前 20 字）
        if not conv.title:
            conv.title = body.content[:20]

        db.commit()
        db.refresh(user_msg)

        # 构建消息列表
        messages_for_ai: list[dict] = []

        # System prompt：策略对话用策略提示词做人格，普通对话用通用助手
        strategy = None
        if conv.strategy_id:
            strategy = (
                db.query(StrategyPrompt).filter(StrategyPrompt.id == conv.strategy_id).first()
            )
        if strategy:
            # 排查「策略更新是否生效」：打印实际使用的策略 id/更新时间/正文指纹
            import hashlib

            _fp = hashlib.md5((strategy.prompt or "").encode("utf-8")).hexdigest()[:8]
            logger.info(
                "策略对话使用策略 id=%s name=%s updated_at=%s prompt_len=%s fp=%s",
                strategy.id, strategy.name, strategy.updated_at,
                len(strategy.prompt or ""), _fp,
            )
        if strategy and strategy.prompt:
            system_content = (
                strategy.prompt
                + "\n\n---\n你现在是上述策略的分析助手。请严格依据该策略，"
                "结合下方提供的行情数据回答用户问题；数据不足时可调用工具补充。\n\n"
                + STRATEGY_TAG_INSTRUCTION
            )
        else:
            system_content = SYSTEM_PROMPT

        # 绑定股票提示
        if conv.stock_symbol and conv.stock_market:
            system_content += f"\n\n当前对话关联股票：{conv.stock_market}:{conv.stock_symbol}"

        # 前端页面快照（对话创建时传入）
        if conv.initial_context:
            system_content += "\n\n--- 用户页面快照（对话创建时） ---\n" + conv.initial_context

        messages_for_ai.append({"role": "system", "content": system_content})

        # 历史消息
        history = (
            db.query(ChatMessage)
            .filter(ChatMessage.conversation_id == conversation_id)
            .order_by(ChatMessage.created_at.asc())
            .all()
        )
        recent = history[-MAX_HISTORY_MESSAGES:] if len(history) > MAX_HISTORY_MESSAGES else history
        for m in recent:
            if m.role in ("user", "assistant"):
                messages_for_ai.append({"role": m.role, "content": m.content})

        # 注入基础上下文（持仓 + 绑定股票的行情/建议）
        context_parts: list[str] = []

        # 用户持仓
        portfolio_ctx = _build_portfolio_context(db)
        if portfolio_ctx:
            context_parts.append(portfolio_ctx)

        # 绑定股票的实时数据
        if conv.stock_symbol and conv.stock_market:
            if strategy:
                # 策略对话：注入当天实时行情 + 最近120根日K + 技术摘要（供策略逐项判断）
                try:
                    from src.web.api.strategy_analysis import _build_market_context

                    market_ctx = await _build_market_context(
                        conv.stock_symbol, conv.stock_market, 120
                    )
                    if market_ctx:
                        context_parts.append(market_ctx)
                except Exception as e:
                    logger.warning(f"策略对话行情上下文构建失败: {e}")
            else:
                realtime = await _fetch_realtime_context(conv.stock_symbol, conv.stock_market)
                if realtime:
                    context_parts.append(realtime)
                technical = await _fetch_technical_context(conv.stock_symbol, conv.stock_market)
                if technical:
                    context_parts.append(technical)
            stock_ctx = _build_stock_context(db, conv.stock_symbol, conv.stock_market)
            if stock_ctx:
                context_parts.append(stock_ctx)

        if context_parts:
            # 把上下文追加到 system message
            messages_for_ai[0]["content"] += "\n\n--- 当前数据 ---\n" + "\n\n".join(context_parts)

        # 调用 AI（带 tool use，用于按需获取更多数据）
        ai_client = _get_ai_client(db, conv.ai_model_id)
        ai_response = ""
        try:
            for _round in range(MAX_TOOL_ROUNDS):
                try:
                    response_msg = await ai_client.chat_with_tools(
                        messages_for_ai, tools=CHAT_TOOLS, temperature=0.5,
                    )
                except Exception:
                    # 模型不支持 tool use → 直接用 chat_multi
                    logger.info("Tool use 不可用，使用普通对话")
                    ai_response = await ai_client.chat_multi(messages_for_ai, temperature=0.5)
                    break

                if not response_msg.tool_calls:
                    ai_response = response_msg.content or ""
                    break

                # 执行 tool calls
                messages_for_ai.append({
                    "role": "assistant",
                    "content": response_msg.content or None,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                        }
                        for tc in response_msg.tool_calls
                    ],
                })

                for tc in response_msg.tool_calls:
                    tool_args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                    logger.info(f"Tool call: {tc.function.name}({tool_args})")
                    result = await _execute_tool(db, tc.function.name, tool_args)
                    messages_for_ai.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result,
                    })
            else:
                ai_response = response_msg.content or "抱歉，处理轮次过多，请精简问题再试。"

        except Exception as e:
            logger.error(f"AI 对话失败: {e}")
            ai_response = f"抱歉，AI 服务暂时不可用：{e}"

        # 策略对话：抽取结构化标签叠加到策略池股票，并从正文剥离标签块
        if strategy and conv.stock_symbol and conv.stock_market:
            try:
                tags = try_extract_tagged_json(ai_response)
                if tags:
                    prose = strip_tagged_json(ai_response)
                    # 以文字结论为准校正 breakout，避免徽章与正文结论不一致
                    tags = reconcile_breakout_tag(prose, tags)
                    _apply_strategy_tags(db, conv.stock_symbol, conv.stock_market, tags)
                    ai_response = prose
            except Exception as e:
                logger.warning(f"策略标签抽取失败: {e}")

        # 保存 AI 回复
        assistant_msg = ChatMessage(
            conversation_id=conversation_id,
            role="assistant",
            content=ai_response,
        )
        db.add(assistant_msg)

        # 更新对话时间
        conv.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(assistant_msg)

        return {
            "id": assistant_msg.id,
            "role": "assistant",
            "content": assistant_msg.content,
            "created_at": str(assistant_msg.created_at or ""),
        }
    finally:
        db.close()
