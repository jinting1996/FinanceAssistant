"""Market events, sector rotation, and watched board APIs."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from src.collectors.discovery_collector import EastMoneyDiscoveryCollector
from src.collectors.news_collector import NewsCollector, NewsItem
from src.config import Settings
from src.core.board_signals import build_board_signal
from src.core.notifier import get_global_proxy
from src.web.database import get_db
from src.core.sector_pool import (
    SECTOR_CATEGORIES,
    fetch_all_boards,
    seed_sector_pool,
)
from src.core.sector_valuation import (
    compute_valuation,
    compute_valuation_map,
    tencent_code_to_sw,
)
from src.web.models import BoardEventMark, BoardKlineCache, SectorValuationDaily, Stock, WatchedBoard

router = APIRouter()
logger = logging.getLogger(__name__)

MAX_WATCHED_BOARDS = 8
DEFAULT_BOARD_DAYS = 120
MAX_BOARD_DAYS = 1300  # ~5 年交易日,供板块池长周期K线
EVENT_TYPES = ("policy", "industry", "earnings", "macro", "case")

# A 股事件统一按北京时间处理,避免服务器在 UTC 时区时日期/时间偏移。
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def _now_bj() -> datetime:
    return datetime.now(SHANGHAI_TZ).replace(tzinfo=None)

SOURCE_LABELS = {
    "xueqiu": "雪球",
    "eastmoney_news": "东方财富资讯",
    "eastmoney": "东方财富公告",
    "newsnow": "NewsNow 财经快讯",
    "macro_calendar": "默认宏观日历",
}

NEWSNOW_DEFAULT_BASE_URL = "https://newsnow.busiyi.world"
NEWSNOW_DEFAULT_CHANNELS = (
    "wallstreetcn-quick",
    "cls-telegraph",
    "jin10",
    "mktnews-flash",
    "fastbull-express",
)
NEWSNOW_CHANNEL_LABELS = {
    "wallstreetcn-quick": "华尔街见闻快讯",
    "cls-telegraph": "财联社电报",
    "jin10": "金十数据",
    "mktnews-flash": "MKTNews 快讯",
    "fastbull-express": "法布财经快讯",
}

MAJOR_KEYWORDS = (
    "重大",
    "并购",
    "重组",
    "停牌",
    "复牌",
    "业绩",
    "预告",
    "分红",
    "回购",
    "减持",
    "增持",
    "监管",
    "处罚",
    "中标",
    "签约",
    "涨价",
    "降价",
    "政策",
    "会议",
    "央行",
    "利率",
    "出口",
    "制裁",
)

POSITIVE_KEYWORDS = (
    "利好",
    "增长",
    "上调",
    "增持",
    "回购",
    "中标",
    "签约",
    "突破",
    "超预期",
    "盈利",
    "复苏",
    "扩产",
    "涨价",
)

NEGATIVE_KEYWORDS = (
    "利空",
    "下调",
    "减持",
    "亏损",
    "处罚",
    "调查",
    "诉讼",
    "监管",
    "违约",
    "退市",
    "暴跌",
    "风险",
    "降价",
)

THEME_HINTS = {
    "人工智能": ("AI", "算力", "大模型", "机器人", "智能", "芯片", "服务器"),
    "半导体": ("半导体", "芯片", "晶圆", "光刻", "封测", "存储"),
    "新能源": ("新能源", "光伏", "储能", "锂电", "电池", "风电", "充电"),
    "汽车": ("汽车", "整车", "零部件", "智能驾驶", "无人驾驶"),
    "医药": ("医药", "创新药", "医疗", "器械", "疫苗", "药品"),
    "消费": ("消费", "白酒", "食品", "旅游", "酒店", "零售"),
    "金融": ("银行", "证券", "保险", "金融", "券商"),
    "地产": ("地产", "房地产", "物业", "城中村", "基建"),
    "军工": ("军工", "航天", "航空", "卫星", "无人机"),
    "传媒": ("传媒", "游戏", "影视", "出版", "短剧"),
}


class WatchBoardRequest(BaseModel):
    market: str = Field(default="CN")
    board_code: str
    board_name: str


class PoolBoardRequest(BaseModel):
    market: str = Field(default="CN")
    board_code: str
    board_name: str
    category: str = Field(default="")
    scope: str = Field(default="industry")


class PoolBoardUpdateRequest(BaseModel):
    market: str = Field(default="CN")
    category: str | None = None
    tier: str | None = None  # pool / pinned
    enabled: bool | None = None


class BoardEventMarkRequest(BaseModel):
    market: str = Field(default="CN")
    date: str  # YYYY-MM-DD
    event_type: str = Field(default="case")
    title: str
    summary: str | None = None
    importance: int = Field(default=1, ge=1, le=2)


class BoardEventMarkUpdateRequest(BaseModel):
    date: str | None = None
    event_type: str | None = None
    title: str | None = None
    summary: str | None = None
    importance: int | None = Field(default=None, ge=1, le=2)


class BoardRefreshRequest(BaseModel):
    market: str = Field(default="CN")
    board_codes: list[str] | None = None
    days: int = Field(default=DEFAULT_BOARD_DAYS, ge=30, le=MAX_BOARD_DAYS)


def _resolve_proxy() -> str:
    try:
        return (get_global_proxy() or "").strip() or (Settings().http_proxy or "").strip()
    except Exception:
        return ""


def _collector() -> EastMoneyDiscoveryCollector:
    return EastMoneyDiscoveryCollector(timeout_s=12.0, proxy=_resolve_proxy() or None, retries=1)


def _period_start(period: str) -> tuple[datetime, str]:
    now = _now_bj()
    key = (period or "week").lower()
    if key == "month":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0), "month"
    if key == "rolling_month":
        return now - timedelta(days=30), "rolling_month"
    monday = now - timedelta(days=now.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0), "week"


def _period_window(period: str) -> tuple[datetime, datetime, str]:
    start, key = _period_start(period)
    now = _now_bj()
    if key == "month":
        if start.month == 12:
            next_month = start.replace(year=start.year + 1, month=1, day=1)
        else:
            next_month = start.replace(month=start.month + 1, day=1)
        return start, next_month - timedelta(seconds=1), key
    if key == "rolling_month":
        return start, now, key
    return start, start + timedelta(days=7) - timedelta(seconds=1), key


def _safe_number(value) -> float | None:
    if value is None:
        return None
    try:
        n = float(value)
        if not math.isfinite(n):
            return None
        return n
    except Exception:
        return None


def _pct_label(value: float | None) -> str:
    if value is None:
        return "--"
    return f"{value:+.2f}%"


def _amount_label(value: float | None) -> str:
    if value is None:
        return "--"
    abs_v = abs(value)
    if abs_v >= 100_000_000:
        return f"{value / 100_000_000:.1f}亿"
    if abs_v >= 10_000:
        return f"{value / 10_000:.1f}万"
    return f"{value:.0f}"


def _to_naive(dt: datetime) -> datetime:
    # tz-aware 一律转北京时间再去 tz;naive 视为已是北京时间。
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(SHANGHAI_TZ).replace(tzinfo=None)


def _parse_newsnow_time(value) -> datetime:
    if value is None:
        return _now_bj()
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 10_000_000_000:  # 毫秒时间戳
            ts = ts / 1000.0
        try:
            return datetime.fromtimestamp(ts, SHANGHAI_TZ).replace(tzinfo=None)
        except Exception:
            return _now_bj()
    text = str(value).strip()
    if not text:
        return _now_bj()
    if text.isdigit():
        return _parse_newsnow_time(float(text))
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return parsed.astimezone(SHANGHAI_TZ).replace(tzinfo=None)
        return parsed  # 无 tz 视为已是北京时间
    except Exception:
        return _now_bj()


def _clean_newsnow_text(value: str) -> str:
    text = re.sub(r"<[^>]+>", "", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()


def _newsnow_importance(title: str) -> int:
    if any(k in title for k in ("突发", "重磅", "紧急", "重大", "美联储", "央行", "降息", "加息", "制裁")):
        return 3
    if any(k in title for k in ("快讯", "政策", "数据", "会议", "通胀", "就业", "PMI", "CPI", "PPI", "社融", "LPR")):
        return 2
    return 1


async def _fetch_newsnow_news(*, since: datetime, limit: int = 60) -> list[NewsItem]:
    base_url = (os.getenv("NEWSNOW_BASE_URL") or NEWSNOW_DEFAULT_BASE_URL).strip().rstrip("/")
    channels_env = (os.getenv("NEWSNOW_CHANNELS") or "").strip()
    channels = [x.strip() for x in channels_env.split(",") if x.strip()] or list(NEWSNOW_DEFAULT_CHANNELS)
    channels = channels[:8]
    headers = {
        "User-Agent": "panwatcher/1.0 (+https://github.com/PotatoChipking/finance)",
        "Accept": "application/json,text/plain,*/*",
    }
    proxy = _resolve_proxy()
    transport = httpx.AsyncHTTPTransport(proxy=proxy) if proxy else None
    timeout = httpx.Timeout(10.0, connect=5.0)
    since_cmp = _to_naive(since)
    out: list[NewsItem] = []
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            headers=headers,
            follow_redirects=True,
            verify=False,
            transport=transport,
        ) as client:
            tasks = [
                client.get(f"{base_url}/api/s", params={"id": channel})
                for channel in channels
            ]
            responses = await asyncio.gather(*tasks, return_exceptions=True)
    except Exception as e:
        logger.debug(f"NewsNow 财经快讯采集失败: {e}")
        return []

    for channel, resp in zip(channels, responses):
        if isinstance(resp, Exception):
            logger.debug(f"NewsNow channel {channel} failed: {resp}")
            continue
        try:
            if resp.status_code >= 400:
                logger.debug(f"NewsNow channel {channel} status={resp.status_code}")
                continue
            payload = resp.json()
            items = payload.get("items") or []
            channel_label = NEWSNOW_CHANNEL_LABELS.get(channel, channel)
            for item in items:
                title = _clean_newsnow_text(item.get("title") or "")
                if not title:
                    continue
                pub_date = _parse_newsnow_time(item.get("pubDate") or (item.get("extra") or {}).get("date"))
                if pub_date < since_cmp:
                    continue
                external_id = str(item.get("id") or item.get("url") or f"{channel}:{title}")
                content = _clean_newsnow_text((item.get("extra") or {}).get("hover") or channel_label)
                out.append(
                    NewsItem(
                        source="newsnow",
                        external_id=f"{channel}:{external_id}",
                        title=title,
                        content=content[:300],
                        publish_time=pub_date,
                        symbols=[],
                        importance=_newsnow_importance(title),
                        url=str(item.get("url") or item.get("mobileUrl") or ""),
                    )
                )
        except Exception as e:
            logger.debug(f"NewsNow channel {channel} parse failed: {e}")
            continue

    out.sort(key=lambda x: x.publish_time, reverse=True)
    return out[: max(1, min(int(limit), 120))]


def _sentiment(text: str) -> tuple[str, int, int]:
    pos = sum(1 for k in POSITIVE_KEYWORDS if k in text)
    neg = sum(1 for k in NEGATIVE_KEYWORDS if k in text)
    if pos > neg:
        return "positive", pos, neg
    if neg > pos:
        return "negative", pos, neg
    return "neutral", pos, neg


def _impact_level(score: float) -> str:
    if score >= 55:
        return "high"
    if score >= 30:
        return "medium"
    return "low"


def _dedupe_news(items: list[NewsItem]) -> list[NewsItem]:
    seen: set[tuple[str, str, str]] = set()
    out: list[NewsItem] = []
    for item in items:
        key = (item.source or "", item.external_id or "", item.title or "")
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _related_theme_names(text: str, board_names: list[str]) -> list[str]:
    matched: list[str] = []
    for name in board_names:
        if name and name in text:
            matched.append(name)
    for theme, hints in THEME_HINTS.items():
        if any(h in text for h in hints):
            matched.append(theme)
    out: list[str] = []
    for name in matched:
        if name not in out:
            out.append(name)
        if len(out) >= 4:
            break
    return out


def _event_prediction(sentiment: str, impact: str, themes: list[str]) -> str:
    theme_text = "、".join(themes[:2]) if themes else "相关标的"
    if sentiment == "positive":
        if impact == "high":
            return f"消息强度较高，短线可能强化{theme_text}的风险偏好，但高开后要观察成交额能否持续放大。"
        return f"消息偏正面，可能带来{theme_text}的结构性活跃，持续性取决于后续成交和政策、订单验证。"
    if sentiment == "negative":
        if impact == "high":
            return f"消息偏负面且影响较高，{theme_text}可能面临估值压制或资金回避，关注是否扩散到同类板块。"
        return "消息偏谨慎，短线更适合降低追高预期，等待价格和资金面重新确认。"
    if impact == "high":
        return f"事件重要性较高但方向未明，资金可能先做分歧交易，重点看{theme_text}是否放量选择方向。"
    return "消息方向中性，预计更多影响市场预期修正，暂以跟踪后续披露和板块成交变化为主。"


def _event_ai_conclusion(sentiment: str, themes: list[str]) -> str:
    theme_text = "、".join(themes[:3]) if themes else "相关板块"
    if sentiment == "positive":
        return f"结论：偏利好 {theme_text}，但需要成交额和政策/数据兑现确认。"
    if sentiment == "negative":
        return f"结论：偏利空 {theme_text}，短线注意资金避险和高位板块回撤。"
    return f"结论：中性观察 {theme_text}，等待数据或政策细节给出方向。"


def _macro_event(
    *,
    event_date: datetime,
    category: str,
    title: str,
    sentiment: str,
    impact_level: str,
    impact_score: float,
    related_boards: list[str],
    prediction: str,
    content: str = "",
) -> dict:
    return {
        "id": f"macro:{category}:{event_date.strftime('%Y%m%d%H%M')}:{title}",
        "title": title,
        "content": content,
        "source": "macro_calendar",
        "source_label": "默认宏观日历",
        "event_category": category,
        "event_date": event_date.strftime("%Y-%m-%d %H:%M"),
        "symbols": [],
        "importance": 3 if impact_level == "high" else 2 if impact_level == "medium" else 1,
        "sentiment": sentiment,
        "impact_level": impact_level,
        "impact_score": round(float(impact_score), 1),
        "impact_summary": _event_ai_conclusion(sentiment, related_boards),
        "prediction": prediction,
        "ai_conclusion": _event_ai_conclusion(sentiment, related_boards),
        "related_boards": related_boards,
        "url": "",
    }


def _build_default_macro_events(*, start: datetime, end: datetime, market: str, limit: int) -> list[dict]:
    """Default macro calendar shown even when no watchlist/news source exists.

    这些是“观察窗口”而不是官方精确日历。真实日历源接入后可替换这里的规则。
    """
    if market != "CN":
        return []
    start_day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    end_day = end.replace(hour=0, minute=0, second=0, microsecond=0)
    days = []
    cur = start_day
    while cur <= end_day and len(days) <= 45:
        days.append(cur)
        cur += timedelta(days=1)

    events: list[dict] = []
    for day in days:
        # 周度固定观察项。
        if day.weekday() == 0:
            events.append(
                _macro_event(
                    event_date=day.replace(hour=10, minute=0),
                    category="国内政策与流动性",
                    title="国内政策与资金面观察",
                    sentiment="neutral",
                    impact_level="medium",
                    impact_score=42,
                    related_boards=["银行", "证券", "地产", "基建"],
                    prediction="观察央行公开市场操作、财政发力和稳增长表述。若流动性边际宽松，通常利好券商、地产链和基建；若资金面收紧，高估值成长板块承压。",
                )
            )
        if day.weekday() == 2:
            events.append(
                _macro_event(
                    event_date=day.replace(hour=21, minute=30),
                    category="海外经济数据",
                    title="美国通胀、就业与 PMI 数据窗口",
                    sentiment="neutral",
                    impact_level="high",
                    impact_score=62,
                    related_boards=["半导体", "人工智能", "黄金", "出口链"],
                    prediction="若数据强于预期，美元利率上行压力可能压制成长股估值，黄金和高估值科技承压；若数据走弱，降息交易升温，利好 AI、半导体等风险资产。",
                )
            )
        if day.weekday() == 4:
            events.append(
                _macro_event(
                    event_date=day.replace(hour=22, minute=0),
                    category="美联储与全球央行",
                    title="美联储利率路径与官员讲话观察",
                    sentiment="neutral",
                    impact_level="high",
                    impact_score=65,
                    related_boards=["半导体", "人工智能", "有色金属", "黄金"],
                    prediction="鹰派表态通常利空成长和资源品估值，鸽派表态利好风险偏好。重点看美元、美债收益率和北向/外资风险偏好变化。",
                )
            )

        # 月度国内宏观高频窗口。
        if day.day == 1:
            events.append(
                _macro_event(
                    event_date=day.replace(hour=9, minute=30),
                    category="国内经济数据",
                    title="国内 PMI 景气度观察",
                    sentiment="neutral",
                    impact_level="medium",
                    impact_score=48,
                    related_boards=["制造业", "工业母机", "新能源", "消费"],
                    prediction="PMI 回升利好顺周期和制造业链条，回落则提示需求偏弱，资金可能转向防御或政策预期方向。",
                )
            )
        if day.day == 7:
            events.append(
                _macro_event(
                    event_date=day.replace(hour=11, minute=0),
                    category="国内经济数据",
                    title="进出口与外需链观察",
                    sentiment="neutral",
                    impact_level="medium",
                    impact_score=45,
                    related_boards=["出口链", "家电", "汽车", "航运"],
                    prediction="出口强于预期利好外需链和港口航运，弱于预期则关注稳外贸政策和人民币汇率变化。",
                )
            )
        if day.day == 10:
            events.append(
                _macro_event(
                    event_date=day.replace(hour=9, minute=30),
                    category="国内经济数据",
                    title="CPI/PPI 通胀数据观察",
                    sentiment="neutral",
                    impact_level="medium",
                    impact_score=50,
                    related_boards=["消费", "食品饮料", "有色金属", "化工"],
                    prediction="CPI 回升利好消费定价权，PPI 改善利好周期品利润修复；若通胀偏弱，政策宽松预期可能升温。",
                )
            )
        if day.day == 15:
            events.append(
                _macro_event(
                    event_date=day.replace(hour=10, minute=0),
                    category="国内经济数据",
                    title="社融、M2 与月度经济数据窗口",
                    sentiment="neutral",
                    impact_level="high",
                    impact_score=68,
                    related_boards=["银行", "地产", "基建", "消费"],
                    prediction="社融和经济数据强，利好顺周期、银行和地产链；若低于预期，市场可能交易政策加码，短线关注券商和稳增长方向。",
                )
            )
        if day.day == 20:
            events.append(
                _macro_event(
                    event_date=day.replace(hour=9, minute=15),
                    category="国内政策与流动性",
                    title="LPR 与利率政策观察",
                    sentiment="neutral",
                    impact_level="high",
                    impact_score=64,
                    related_boards=["银行", "地产", "证券", "高股息"],
                    prediction="LPR 下调通常利好地产链、券商和风险偏好，但银行净息差可能承压；维持不变则关注市场是否转向业绩主线。",
                )
            )
        if day.day in (25, 26):
            events.append(
                _macro_event(
                    event_date=day.replace(hour=15, minute=0),
                    category="国内重要政策",
                    title="月末重要政策与产业方向观察",
                    sentiment="neutral",
                    impact_level="medium",
                    impact_score=55,
                    related_boards=["人工智能", "半导体", "新能源", "地产"],
                    prediction="若会议或部委表态强调产业扶持，相关主题可能获得资金回流；若重心偏防风险，地产、金融和高股息可能更受关注。",
                )
            )

    # 去重并按日期、重要度截取，避免月视图被默认事件淹没。
    unique: dict[str, dict] = {}
    for ev in events:
        unique[ev["id"]] = ev
    rows = list(unique.values())
    rows.sort(key=lambda x: (x["event_date"], -float(x["impact_score"])))
    return rows[: max(1, min(int(limit), 24))]


def _build_events(
    *,
    news_items: list[NewsItem],
    start: datetime,
    end: datetime,
    board_names: list[str],
    limit: int,
    market: str = "CN",
) -> list[dict]:
    now = _now_bj()
    events: list[dict] = []
    start_cmp = _to_naive(start)
    for item in _dedupe_news(news_items):
        publish_time = _to_naive(item.publish_time)
        if publish_time < start_cmp:
            continue
        text = f"{item.title} {item.content or ''}"
        sentiment, pos, neg = _sentiment(text)
        keyword_hits = sum(1 for k in MAJOR_KEYWORDS if k in text)
        age_hours = max((now - publish_time).total_seconds() / 3600, 0.0)
        recency_score = max(0.0, 18.0 - min(age_hours / 6.0, 18.0))
        score = float(item.importance or 0) * 18.0 + keyword_hits * 8.0 + recency_score
        if item.source == "eastmoney":
            score += 8.0
        if pos or neg:
            score += 5.0
        themes = _related_theme_names(text, board_names)
        impact = _impact_level(score)
        direction = "偏多" if sentiment == "positive" else "偏空" if sentiment == "negative" else "中性"
        impact_summary = (
            f"{direction}事件，重要性 {impact}，"
            f"命中 {keyword_hits} 个关键线索，关联 {len(item.symbols or [])} 只关注标的。"
        )
        events.append(
            {
                "id": f"{item.source}:{item.external_id}",
                "title": item.title,
                "content": item.content or "",
                "source": item.source,
                "source_label": SOURCE_LABELS.get(item.source, item.source),
                "event_category": "财经快讯" if item.source == "newsnow" else "关注池消息",
                "event_date": publish_time.strftime("%Y-%m-%d %H:%M"),
                "symbols": item.symbols or [],
                "importance": int(item.importance or 0),
                "sentiment": sentiment,
                "impact_level": impact,
                "impact_score": round(score, 1),
                "impact_summary": impact_summary,
                "prediction": _event_prediction(sentiment, impact, themes),
                "ai_conclusion": _event_ai_conclusion(sentiment, themes),
                "related_boards": themes,
                "url": item.url or "",
            }
        )

    macro_limit = max(8, min(int(limit), 16))
    events.extend(_build_default_macro_events(start=start, end=end, market=market, limit=macro_limit))
    limit_n = max(1, min(int(limit), 80))
    selected = sorted(
        events,
        key=lambda x: (x["event_date"], float(x["impact_score"])),
        reverse=True,
    )[:limit_n]
    selected.sort(key=lambda x: (x["event_date"], -float(x["impact_score"])))
    return selected


def _board_flow_state(change_pct: float | None, score: float) -> tuple[str, str]:
    if change_pct is not None and change_pct < -1.0:
        return "cooling", "资金退潮"
    if score >= 75:
        return "inflow", "资金聚焦"
    if score >= 48:
        return "active", "轮动活跃"
    return "neutral", "观察"


def _board_reason(board: dict, leaders: list[dict], max_turnover: float) -> tuple[float, str, str, str]:
    change_pct = _safe_number(board.get("change_pct"))
    turnover = _safe_number(board.get("turnover")) or 0.0
    turnover_score = min(45.0, turnover / max_turnover * 45.0) if max_turnover > 0 else 0.0
    change_score = 35.0 + (change_pct or 0.0) * 7.0
    leader_hits = sum(1 for x in leaders[:5] if (_safe_number(x.get("change_pct")) or 0.0) > 3.0)
    leader_score = min(20.0, leader_hits * 4.0)
    flow_score = max(0.0, min(100.0, change_score + turnover_score + leader_score))
    state, label = _board_flow_state(change_pct, flow_score)
    signal_map = {
        "inflow": "资金正在集中，优先观察龙头承接和后排扩散。",
        "active": "板块处在轮动活跃区，适合跟踪强弱切换。",
        "cooling": "涨跌幅走弱，短线资金可能转向其他高弹性方向。",
        "neutral": "热度一般，暂以观察为主。",
    }
    reason = f"涨跌幅 {_pct_label(change_pct)}，成交额 {_amount_label(turnover)}，强势成分股 {leader_hits} 只。"
    return round(flow_score, 1), state, label, f"{reason}{signal_map[state]}"


async def _load_tencent_boards(limit: int) -> list[dict]:
    """腾讯行业 + 概念板块排行,合并去重后构建轮动卡片。失败返回空由上层回退。"""
    from src.collectors.tencent_board_collector import TencentBoardCollector

    collector = TencentBoardCollector(timeout_s=12.0, proxy=_resolve_proxy() or None, retries=1)
    try:
        pool = min(max(limit * 3, 40), 100)
        industry, concept = await asyncio.gather(
            collector.fetch_hot_boards(scope="industry", limit=pool),
            collector.fetch_hot_boards(scope="concept", limit=pool),
        )
    except Exception:
        return []

    merged: dict[str, dict] = {}
    for board in [*(industry or []), *(concept or [])]:
        code = str(board.get("code") or "")
        if code:
            merged.setdefault(code, board)
    boards = list(merged.values())
    if not boards:
        return []

    boards.sort(key=lambda x: -(float(x.get("change_pct") or 0.0)))
    selected = boards[: max(1, min(int(limit), 20))]
    max_turnover = max((_safe_number(x.get("turnover")) or 0.0) for x in selected) if selected else 0.0

    enriched: list[dict] = []
    for rank, board in enumerate(selected):
        leaders: list[dict] = []
        if board.get("leader_name"):
            leaders = [
                {
                    "symbol": board.get("leader_code") or "",
                    "market": "CN",
                    "name": board.get("leader_name"),
                    "price": None,
                    "change_pct": board.get("leader_change_pct"),
                    "turnover": None,
                }
            ]
        score, state, label, reason = _board_reason(board, leaders, max_turnover)
        enriched.append(
            {
                "code": board.get("code"),
                "name": board.get("name"),
                "change_pct": _safe_number(board.get("change_pct")),
                "turnover": _safe_number(board.get("turnover")),
                "rank_gainers": rank + 1,
                "rank_turnover": None,
                "flow_score": score,
                "flow_state": state,
                "flow_label": label,
                "rotation_signal": reason,
                "leaders": leaders,
                "source": "tencent",
            }
        )
    enriched.sort(key=lambda x: float(x.get("flow_score") or 0.0), reverse=True)
    return enriched


async def _load_boards(market: str, limit: int, db: Session) -> list[dict]:
    # CN 优先用腾讯板块排行(行业+概念);拉不到再回退东方财富,最后才是合成板块。
    if market == "CN":
        tencent_boards = await _load_tencent_boards(limit)
        if tencent_boards:
            return tencent_boards

    collector = _collector()

    async def fetch(mode: str):
        try:
            return await collector.fetch_hot_boards(market=market, mode=mode, limit=max(limit, 12))
        except Exception:
            return []

    gainers, turnover_boards = await asyncio.gather(fetch("gainers"), fetch("turnover"))
    by_code: dict[str, dict] = {}
    for rank, item in enumerate(gainers):
        by_code[item.code] = {
            "code": item.code,
            "name": item.name,
            "change_pct": item.change_pct,
            "change_amount": item.change_amount,
            "turnover": item.turnover,
            "rank_gainers": rank + 1,
            "rank_turnover": None,
        }
    for rank, item in enumerate(turnover_boards):
        row = by_code.setdefault(
            item.code,
            {
                "code": item.code,
                "name": item.name,
                "change_pct": item.change_pct,
                "change_amount": item.change_amount,
                "turnover": item.turnover,
                "rank_gainers": None,
            },
        )
        row["turnover"] = item.turnover if item.turnover is not None else row.get("turnover")
        row["rank_turnover"] = rank + 1

    if not by_code:
        from src.web.api.discovery import get_hot_boards

        fallback = await get_hot_boards(market=market, mode="gainers", limit=limit, db=db)
        for rank, item in enumerate(fallback):
            code = str(item.get("code") or "")
            if code:
                by_code[code] = {**item, "rank_gainers": rank + 1, "rank_turnover": None}

    boards = list(by_code.values())
    boards.sort(
        key=lambda x: (
            0 if x.get("rank_turnover") else 1,
            x.get("rank_turnover") or 999,
            -(float(x.get("change_pct") or 0.0)),
        )
    )
    selected = boards[: max(1, min(int(limit), 20))]
    max_turnover = max((_safe_number(x.get("turnover")) or 0.0) for x in selected) if selected else 0.0

    async def leaders_for(board: dict) -> tuple[str, list[dict]]:
        code = str(board.get("code") or "")
        if not code or code.startswith(("CN_", "HK_", "US_")):
            return code, []
        try:
            items = await collector.fetch_board_stocks(board_code=code, mode="gainers", limit=6)
            return code, [
                {
                    "symbol": x.symbol,
                    "market": x.market,
                    "name": x.name,
                    "price": x.price,
                    "change_pct": x.change_pct,
                    "turnover": x.turnover,
                }
                for x in items
            ]
        except Exception:
            return code, []

    leader_pairs = await asyncio.gather(*[leaders_for(board) for board in selected])
    leader_map = {code: leaders for code, leaders in leader_pairs}
    enriched: list[dict] = []
    for board in selected:
        leaders = leader_map.get(str(board.get("code") or ""), [])
        score, state, label, reason = _board_reason(board, leaders, max_turnover)
        enriched.append(
            {
                "code": board.get("code"),
                "name": board.get("name"),
                "change_pct": _safe_number(board.get("change_pct")),
                "turnover": _safe_number(board.get("turnover")),
                "rank_gainers": board.get("rank_gainers"),
                "rank_turnover": board.get("rank_turnover"),
                "flow_score": score,
                "flow_state": state,
                "flow_label": label,
                "rotation_signal": reason,
                "leaders": leaders,
            }
        )
    enriched.sort(key=lambda x: float(x.get("flow_score") or 0.0), reverse=True)
    return enriched


def _rotation_summary(boards: list[dict], events: list[dict]) -> dict:
    inflow = [x for x in boards if x.get("flow_state") in ("inflow", "active")]
    cooling = [x for x in boards if x.get("flow_state") == "cooling"]
    hot_names = [x.get("name") for x in inflow[:3] if x.get("name")]
    cooling_names = [x.get("name") for x in cooling[:3] if x.get("name")]
    event_counter = Counter()
    for ev in events:
        for b in ev.get("related_boards") or []:
            event_counter[b] += 1
    event_topics = [name for name, _ in event_counter.most_common(3)]

    if hot_names:
        summary = f"当前资金主要集中在 {'、'.join(hot_names)}。"
    else:
        summary = "当前板块资金集中度不高，更多是分散轮动。"
    if event_topics:
        summary += f" 消息面高频主题包括 {'、'.join(event_topics)}。"
    if cooling_names:
        summary += f" {'、'.join(cooling_names)} 出现降温迹象。"

    watch_points = []
    if hot_names:
        watch_points.append("强势板块需要成交额继续放大，否则容易从主升切换为高位震荡。")
    if cooling_names:
        watch_points.append("降温板块若午后或次日无法收复跌幅，资金可能继续外流。")
    if event_topics:
        watch_points.append("消息驱动方向要关注后续公告、政策或订单是否验证。")
    if not watch_points:
        watch_points.append("等待新的政策、业绩或成交额线索确认主线。")

    return {
        "summary": summary,
        "hot_boards": hot_names,
        "cooling_boards": cooling_names,
        "event_topics": event_topics,
        "watch_points": watch_points,
    }


def _board_to_dict(board: WatchedBoard) -> dict:
    return {
        "id": board.id,
        "market": board.market,
        "board_code": board.board_code,
        "board_name": board.board_name,
        "sort_order": board.sort_order or 0,
        "enabled": bool(board.enabled),
        "category": board.category or "",
        "tier": board.tier or "pool",
        "scope": board.scope or "industry",
        "tags": list(board.tags or []),
        "created_at": board.created_at.isoformat() if board.created_at else None,
        "updated_at": board.updated_at.isoformat() if board.updated_at else None,
    }


def _event_mark_to_dict(mark: BoardEventMark) -> dict:
    return {
        "id": mark.id,
        "market": mark.market,
        "board_code": mark.board_code,
        "date": mark.date,
        "event_type": mark.event_type or "case",
        "title": mark.title,
        "summary": mark.summary or "",
        "importance": int(mark.importance or 1),
        "source": mark.source or "manual",
    }


def _serialize_kline_rows(rows: list[BoardKlineCache]) -> list[dict]:
    return [
        {
            "date": x.date,
            "open": x.open,
            "high": x.high,
            "low": x.low,
            "close": x.close,
            "volume": x.volume,
            "turnover": x.turnover,
        }
        for x in rows
    ]


def _query_cached_klines(db: Session, market: str, board_code: str, days: int) -> list[BoardKlineCache]:
    rows = (
        db.query(BoardKlineCache)
        .filter(BoardKlineCache.market == market, BoardKlineCache.board_code == board_code)
        .order_by(BoardKlineCache.date.desc())
        .limit(max(1, min(int(days or DEFAULT_BOARD_DAYS), MAX_BOARD_DAYS)))
        .all()
    )
    rows.reverse()
    return rows


def _aggregate_weekly(rows: list[dict]) -> list[dict]:
    """日K聚合为周K(ISO 周),date 取周内最后一个交易日。"""
    out: list[dict] = []
    current_key: tuple[int, int] | None = None
    for row in rows:
        try:
            d = datetime.strptime(str(row["date"])[:10], "%Y-%m-%d")
        except ValueError:
            continue
        iso = d.isocalendar()
        key = (iso[0], iso[1])
        if key != current_key:
            out.append(dict(row))
            current_key = key
            continue
        last = out[-1]
        last["date"] = row["date"]
        last["high"] = max(last["high"], row["high"])
        last["low"] = min(last["low"], row["low"])
        last["close"] = row["close"]
        for k in ("volume", "turnover"):
            if row.get(k) is not None:
                last[k] = (last.get(k) or 0) + row[k]
    return out


async def _refresh_one_board(
    db: Session,
    *,
    market: str,
    board_code: str,
    days: int,
    collector: EastMoneyDiscoveryCollector | None = None,
) -> int:
    if market != "CN":
        return 0
    # 优先腾讯板块日K(与轮动主线同源、同 pt 代码),拉不到再回退东方财富。
    bars = []
    source = "tencent"
    try:
        from src.collectors.tencent_board_collector import TencentBoardCollector

        tcoll = TencentBoardCollector(proxy=_resolve_proxy() or None, retries=1)
        bars = await tcoll.fetch_board_klines(code=board_code, days=days)
    except Exception:
        bars = []
    if not bars:
        coll = collector or _collector()
        try:
            bars = await coll.fetch_board_klines(board_code=board_code, days=days)
            source = "eastmoney"
        except Exception:
            bars = []
    updated = 0
    now = datetime.now()
    for bar in bars:
        row = (
            db.query(BoardKlineCache)
            .filter(
                BoardKlineCache.market == market,
                BoardKlineCache.board_code == board_code,
                BoardKlineCache.date == bar.date,
            )
            .first()
        )
        if row is None:
            row = BoardKlineCache(market=market, board_code=board_code, date=bar.date)
            db.add(row)
        row.open = bar.open
        row.high = bar.high
        row.low = bar.low
        row.close = bar.close
        row.volume = bar.volume
        row.turnover = bar.turnover
        row.source = source
        row.fetched_at = now
        updated += 1
    db.commit()
    return updated


async def _ensure_board_klines(db: Session, market: str, board_code: str, days: int) -> list[BoardKlineCache]:
    days = max(30, min(int(days or DEFAULT_BOARD_DAYS), MAX_BOARD_DAYS))
    rows = _query_cached_klines(db, market, board_code, days)
    if len(rows) >= min(days, 60):
        # 缓存覆盖不足请求跨度(如首次请求 3-5 年)时补拉全量。全量拉取会重写最老的行,
        # 所以用"最老一行的 fetched_at"判断近 6 小时内是否已尝试过全量——
        # 板块历史可能本身短于请求跨度,时间闸门避免每次请求都打上游。
        if len(rows) >= days - 30:
            return rows
        oldest_fetch = min((r.fetched_at for r in rows if r.fetched_at), default=None)
        if oldest_fetch and (datetime.now() - oldest_fetch) < timedelta(hours=6):
            return rows
    await _refresh_one_board(db, market=market, board_code=board_code, days=days)
    return _query_cached_klines(db, market, board_code, days)


async def _leaders_for_board(board_code: str, limit: int = 5) -> list[dict]:
    try:
        items = await _collector().fetch_board_stocks(board_code=board_code, mode="gainers", limit=limit)
    except Exception:
        return []
    return [
        {
            "symbol": x.symbol,
            "market": x.market,
            "name": x.name,
            "price": x.price,
            "change_pct": x.change_pct,
            "turnover": x.turnover,
        }
        for x in items
    ]


@router.get("/overview")
async def get_market_events_overview(
    market: str = Query("CN", description="Market code: CN/HK/US"),
    period: str = Query("week", description="week/month/rolling_month"),
    event_limit: int = Query(12, ge=3, le=80),
    board_limit: int = Query(10, ge=4, le=20),
    db: Session = Depends(get_db),
):
    market = (market or "CN").strip().upper()
    if market not in ("CN", "HK", "US"):
        market = "CN"
    start, end, period_key = _period_window(period)
    since_hours = max(24, int((datetime.now() - start).total_seconds() // 3600))

    stocks = db.query(Stock).filter(Stock.market == market).all()
    symbols = [str(s.symbol) for s in stocks if s.symbol]
    symbol_names = {str(s.symbol): str(s.name or s.symbol) for s in stocks if s.symbol}

    boards = await _load_boards(market, board_limit, db)
    board_names = [str(x.get("name") or "") for x in boards if x.get("name")]

    news_items: list[NewsItem] = []
    newsnow_items = await _fetch_newsnow_news(since=start, limit=60) if market == "CN" else []
    if symbols:
        collector = NewsCollector.from_database()
        news_items = await collector.fetch_all(
            symbols=symbols,
            since_hours=since_hours,
            symbol_names=symbol_names,
        )
    news_items.extend(newsnow_items)

    events = _build_events(
        news_items=news_items,
        start=start,
        end=end,
        board_names=board_names,
        limit=event_limit,
        market=market,
    )

    return {
        "market": market,
        "period": period_key,
        "start_date": start.strftime("%Y-%m-%d"),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "events": events,
        "boards": boards,
        "rotation": _rotation_summary(boards, events),
        "coverage": {
            "watchlist_symbols": len(symbols),
            "news_items": len(news_items),
            "newsnow_items": len(newsnow_items),
            "board_count": len(boards),
        },
    }


@router.get("/boards/search")
async def search_boards(
    q: str = Query("", description="Board code or name"),
    limit: int = Query(20, ge=1, le=50),
):
    query = (q or "").strip().lower()

    # 优先腾讯板块池(行业全量 + 概念分页),与轮动主线/板块K线同源;拉不到再回退东方财富。
    tencent_rows = await _search_tencent_boards()
    if tencent_rows:
        rows = [
            r for r in tencent_rows
            if not query or query in f"{r['board_code']} {r['board_name']}".lower()
        ]
        rows.sort(key=lambda x: abs(float(x.get("change_pct") or 0.0)), reverse=True)
        return rows[:limit]

    coll = _collector()
    industry_gainers, industry_turnover, concept_gainers, concept_turnover = await asyncio.gather(
        coll.fetch_hot_boards(market="CN", mode="gainers", limit=500, scope="industry"),
        coll.fetch_hot_boards(market="CN", mode="turnover", limit=500, scope="industry"),
        coll.fetch_hot_boards(market="CN", mode="gainers", limit=500, scope="concept"),
        coll.fetch_hot_boards(market="CN", mode="turnover", limit=500, scope="concept"),
    )
    by_code = {}
    for item in (
        list(industry_gainers)
        + list(industry_turnover)
        + list(concept_gainers)
        + list(concept_turnover)
    ):
        if item.code not in by_code:
            by_code[item.code] = item
    rows = []
    for item in by_code.values():
        hay = f"{item.code} {item.name}".lower()
        if query and query not in hay:
            continue
        rows.append(
            {
                "market": "CN",
                "board_code": item.code,
                "board_name": item.name,
                "change_pct": item.change_pct,
                "turnover": item.turnover,
            }
        )
    rows.sort(key=lambda x: abs(float(x.get("change_pct") or 0.0)), reverse=True)
    return rows[:limit]


async def _search_tencent_boards() -> list[dict]:
    """腾讯行业(全量)+ 概念(分页)板块池,用于搜索过滤。失败返回空由上层回退。"""
    from src.collectors.tencent_board_collector import TencentBoardCollector

    collector = TencentBoardCollector(timeout_s=12.0, proxy=_resolve_proxy() or None, retries=1)
    tasks = [
        collector.fetch_hot_boards(scope="industry", limit=100, offset=0),
        collector.fetch_hot_boards(scope="concept", limit=100, offset=0),
        collector.fetch_hot_boards(scope="concept", limit=100, offset=100),
        collector.fetch_hot_boards(scope="concept", limit=100, offset=200),
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    by_code: dict[str, dict] = {}
    for result in results:
        if isinstance(result, Exception) or not result:
            continue
        for board in result:
            code = str(board.get("code") or "")
            if code and code not in by_code:
                by_code[code] = {
                    "market": "CN",
                    "board_code": code,
                    "board_name": board.get("name"),
                    "change_pct": board.get("change_pct"),
                    "turnover": board.get("turnover"),
                }
    return list(by_code.values())


@router.get("/boards/watchlist")
def get_board_watchlist(db: Session = Depends(get_db)):
    rows = (
        db.query(WatchedBoard)
        .filter(WatchedBoard.enabled == True, WatchedBoard.tier == "pinned")  # noqa: E712
        .order_by(WatchedBoard.sort_order.asc(), WatchedBoard.id.asc())
        .all()
    )
    return [_board_to_dict(x) for x in rows]


@router.post("/boards/watchlist")
def add_board_to_watchlist(payload: WatchBoardRequest, db: Session = Depends(get_db)):
    market = (payload.market or "CN").strip().upper()
    if market != "CN":
        raise HTTPException(400, "v1 仅支持 A 股板块")
    code = (payload.board_code or "").strip()
    name = (payload.board_name or "").strip()
    if not code or not name:
        raise HTTPException(400, "board_code 和 board_name 不能为空")

    existing = (
        db.query(WatchedBoard)
        .filter(WatchedBoard.market == market, WatchedBoard.board_code == code)
        .first()
    )
    pinned_count = (
        db.query(WatchedBoard)
        .filter(WatchedBoard.enabled == True, WatchedBoard.tier == "pinned")  # noqa: E712
        .count()
    )
    if existing:
        if existing.tier != "pinned" and pinned_count >= MAX_WATCHED_BOARDS:
            raise HTTPException(400, f"最多关注 {MAX_WATCHED_BOARDS} 个板块")
        existing.board_name = name
        existing.enabled = True
        existing.tier = "pinned"
        db.commit()
        db.refresh(existing)
        return _board_to_dict(existing)

    if pinned_count >= MAX_WATCHED_BOARDS:
        raise HTTPException(400, f"最多关注 {MAX_WATCHED_BOARDS} 个板块")
    max_order = db.query(func.max(WatchedBoard.sort_order)).scalar() or 0
    row = WatchedBoard(
        market=market,
        board_code=code,
        board_name=name,
        tier="pinned",
        sort_order=int(max_order) + 1,
        enabled=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _board_to_dict(row)


@router.delete("/boards/watchlist/{board_code}")
def delete_board_from_watchlist(
    board_code: str,
    market: str = Query("CN"),
    hard: bool = Query(False, description="true=彻底删除;默认板块池成员仅降级为 pool"),
    db: Session = Depends(get_db),
):
    market = (market or "CN").strip().upper()
    code = (board_code or "").strip()  # 不转大写:腾讯板块代码为小写 pt…,东方财富已是大写 BK…
    row = (
        db.query(WatchedBoard)
        .filter(WatchedBoard.market == market, WatchedBoard.board_code == code)
        .first()
    )
    if not row:
        raise HTTPException(404, "板块未关注")
    # 属于板块池(有分类)的板块,取消关注只降级回池,不从池里删掉
    if not hard and (row.category or ""):
        row.tier = "pool"
        db.commit()
        return {"ok": True, "demoted": True}
    db.delete(row)
    db.commit()
    return {"ok": True}


@router.get("/boards/pool")
async def get_board_pool(
    market: str = Query("CN"),
    auto_seed: bool = Query(True),
    db: Session = Depends(get_db),
):
    """板块池:按六大分类分组返回全部池内板块(含当日涨跌幅)。池为空时自动播种。"""
    market = (market or "CN").strip().upper()
    if market != "CN":
        raise HTTPException(400, "v1 仅支持 A 股板块")

    rows = (
        db.query(WatchedBoard)
        .filter(
            WatchedBoard.market == market,
            WatchedBoard.enabled == True,  # noqa: E712
            WatchedBoard.category != "",
        )
        .order_by(WatchedBoard.sort_order.asc(), WatchedBoard.id.asc())
        .all()
    )

    live_boards: list[dict] = []
    seed_report = None
    if not rows and auto_seed:
        try:
            live_boards = await fetch_all_boards(proxy=_resolve_proxy() or None)
            seed_report = seed_sector_pool(db, live_boards)
            rows = (
                db.query(WatchedBoard)
                .filter(
                    WatchedBoard.market == market,
                    WatchedBoard.enabled == True,  # noqa: E712
                    WatchedBoard.category != "",
                )
                .order_by(WatchedBoard.sort_order.asc(), WatchedBoard.id.asc())
                .all()
            )
        except Exception as e:
            logger.warning("板块池自动播种失败: %s", e)

    if not live_boards:
        try:
            live_boards = await fetch_all_boards(proxy=_resolve_proxy() or None)
        except Exception:
            live_boards = []
    quote_by_code = {str(b.get("code") or ""): b for b in live_boards}

    # 批量估值分位:行业板块 -> 申万码,一次性算 PE 分位
    sw_by_board = {
        row.board_code: sw
        for row in rows
        if (sw := tencent_code_to_sw(row.board_code, row.scope or "industry"))
    }
    val_map = compute_valuation_map(db, list(sw_by_board.values()))

    categories = []
    for key, label in SECTOR_CATEGORIES:
        boards = []
        for row in rows:
            if (row.category or "") != key:
                continue
            item = _board_to_dict(row)
            quote = quote_by_code.get(row.board_code) or {}
            item["change_pct"] = quote.get("change_pct")
            item["turnover"] = quote.get("turnover")
            item["leader_name"] = quote.get("leader_name")
            sw = sw_by_board.get(row.board_code)
            item["valuation"] = val_map.get(sw) if sw else None
            boards.append(item)
        if boards:
            categories.append({"key": key, "label": label, "boards": boards})

    return {
        "market": market,
        "categories": categories,
        "board_count": sum(len(c["boards"]) for c in categories),
        "seed_report": seed_report,
    }


@router.post("/boards/pool/seed")
async def reseed_board_pool(db: Session = Depends(get_db)):
    """手动重新播种板块池(幂等,只补缺)。"""
    boards = await fetch_all_boards(proxy=_resolve_proxy() or None)
    if not boards:
        raise HTTPException(502, "板块名单拉取失败,请稍后重试")
    report = seed_sector_pool(db, boards)
    return report


@router.get("/boards/{board_code}/valuation")
def get_board_valuation(
    board_code: str,
    market: str = Query("CN"),
    db: Session = Depends(get_db),
):
    """板块 PE/PB 估值 + 历史分位(3年/5年)。仅行业板块可算,概念板块返回 available=False。"""
    code = (board_code or "").strip()
    row = (
        db.query(WatchedBoard)
        .filter(WatchedBoard.market == market, WatchedBoard.board_code == code)
        .first()
    )
    scope = (row.scope if row else "industry") or "industry"
    sw = tencent_code_to_sw(code, scope)
    if not sw:
        return {"board_code": code, "available": False, "reason": "非行业板块,无申万估值口径"}
    val = compute_valuation(db, sw)
    if not val:
        return {"board_code": code, "available": False, "reason": "估值历史为空,请先回填"}
    from src.core.sector_valuation import valuation_label

    return {
        "board_code": code,
        "available": True,
        "sw_code": sw,
        **val,
        "label": valuation_label(val["pe_percentile"].get("3y")),
    }


class ValuationBackfillRequest(BaseModel):
    start_year: int = Field(default=2021, ge=2005, le=2100)
    end_year: int | None = None


@router.post("/boards/valuation/backfill")
def backfill_board_valuation(payload: ValuationBackfillRequest, db: Session = Depends(get_db)):
    """回填申万一级行业估值历史(逐年拉取,可能耗时数分钟——建议后台或低频调用)。"""
    from src.core.sector_valuation import backfill_years

    report = backfill_years(db, start_year=payload.start_year, end_year=payload.end_year)
    return report


@router.get("/boards/valuation/status")
def board_valuation_status(db: Session = Depends(get_db)):
    """估值库覆盖情况:行业数、日期范围、总行数。用于前端判断是否需要回填。"""
    total = db.query(func.count(SectorValuationDaily.id)).scalar() or 0
    codes = db.query(func.count(func.distinct(SectorValuationDaily.sw_code))).scalar() or 0
    min_date = db.query(func.min(SectorValuationDaily.date)).scalar()
    max_date = db.query(func.max(SectorValuationDaily.date)).scalar()
    return {
        "rows": int(total),
        "industries": int(codes),
        "min_date": min_date,
        "max_date": max_date,
    }


@router.post("/boards/pool")
def add_board_to_pool(payload: PoolBoardRequest, db: Session = Depends(get_db)):
    """把任意板块(来自搜索)加入板块池并指定分类。"""
    market = (payload.market or "CN").strip().upper()
    if market != "CN":
        raise HTTPException(400, "v1 仅支持 A 股板块")
    code = (payload.board_code or "").strip()
    name = (payload.board_name or "").strip()
    category = (payload.category or "").strip()
    if not code or not name:
        raise HTTPException(400, "board_code 和 board_name 不能为空")
    valid_keys = {k for k, _ in SECTOR_CATEGORIES}
    if category and category not in valid_keys:
        raise HTTPException(400, f"category 必须是 {'/'.join(sorted(valid_keys))} 之一")

    row = (
        db.query(WatchedBoard)
        .filter(WatchedBoard.market == market, WatchedBoard.board_code == code)
        .first()
    )
    if row:
        row.board_name = name
        row.enabled = True
        if category:
            row.category = category
        if payload.scope in ("industry", "concept"):
            row.scope = payload.scope
    else:
        row = WatchedBoard(
            market=market,
            board_code=code,
            board_name=name,
            category=category or "other",
            tier="pool",
            scope=payload.scope if payload.scope in ("industry", "concept") else "industry",
            enabled=True,
        )
        db.add(row)
    db.commit()
    db.refresh(row)
    return _board_to_dict(row)


@router.patch("/boards/pool/{board_code}")
def update_pool_board(
    board_code: str,
    payload: PoolBoardUpdateRequest,
    db: Session = Depends(get_db),
):
    """更新池内板块:改分类 / pin与unpin(tier) / 停启用。"""
    market = (payload.market or "CN").strip().upper()
    code = (board_code or "").strip()
    row = (
        db.query(WatchedBoard)
        .filter(WatchedBoard.market == market, WatchedBoard.board_code == code)
        .first()
    )
    if not row:
        raise HTTPException(404, "板块不在池中")

    if payload.category is not None:
        valid_keys = {k for k, _ in SECTOR_CATEGORIES}
        if payload.category and payload.category not in valid_keys:
            raise HTTPException(400, f"category 必须是 {'/'.join(sorted(valid_keys))} 之一")
        row.category = payload.category
    if payload.tier is not None:
        if payload.tier not in ("pool", "pinned"):
            raise HTTPException(400, "tier 必须是 pool 或 pinned")
        if payload.tier == "pinned" and row.tier != "pinned":
            pinned_count = (
                db.query(WatchedBoard)
                .filter(WatchedBoard.enabled == True, WatchedBoard.tier == "pinned")  # noqa: E712
                .count()
            )
            if pinned_count >= MAX_WATCHED_BOARDS:
                raise HTTPException(400, f"最多关注 {MAX_WATCHED_BOARDS} 个板块")
        row.tier = payload.tier
    if payload.enabled is not None:
        row.enabled = bool(payload.enabled)
    db.commit()
    db.refresh(row)
    return _board_to_dict(row)


@router.get("/boards/{board_code}/events")
def list_board_event_marks(
    board_code: str,
    market: str = Query("CN"),
    db: Session = Depends(get_db),
):
    market = (market or "CN").strip().upper()
    code = (board_code or "").strip()
    rows = (
        db.query(BoardEventMark)
        .filter(BoardEventMark.market == market, BoardEventMark.board_code == code)
        .order_by(BoardEventMark.date.asc(), BoardEventMark.id.asc())
        .all()
    )
    return [_event_mark_to_dict(x) for x in rows]


@router.post("/boards/{board_code}/events")
def create_board_event_mark(
    board_code: str,
    payload: BoardEventMarkRequest,
    db: Session = Depends(get_db),
):
    market = (payload.market or "CN").strip().upper()
    code = (board_code or "").strip()
    if not code:
        raise HTTPException(400, "board_code 不能为空")
    date_str = (payload.date or "").strip()[:10]
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "date 格式必须是 YYYY-MM-DD")
    event_type = (payload.event_type or "case").strip()
    if event_type not in EVENT_TYPES:
        raise HTTPException(400, f"event_type 必须是 {'/'.join(EVENT_TYPES)} 之一")
    title = (payload.title or "").strip()
    if not title:
        raise HTTPException(400, "title 不能为空")
    row = BoardEventMark(
        market=market,
        board_code=code,
        date=date_str,
        event_type=event_type,
        title=title,
        summary=(payload.summary or "").strip() or None,
        importance=int(payload.importance or 1),
        source="manual",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _event_mark_to_dict(row)


@router.patch("/boards/events/{mark_id}")
def update_board_event_mark(
    mark_id: int,
    payload: BoardEventMarkUpdateRequest,
    db: Session = Depends(get_db),
):
    row = db.query(BoardEventMark).filter(BoardEventMark.id == mark_id).first()
    if not row:
        raise HTTPException(404, "事件标注不存在")
    if payload.date is not None:
        date_str = payload.date.strip()[:10]
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(400, "date 格式必须是 YYYY-MM-DD")
        row.date = date_str
    if payload.event_type is not None:
        if payload.event_type not in EVENT_TYPES:
            raise HTTPException(400, f"event_type 必须是 {'/'.join(EVENT_TYPES)} 之一")
        row.event_type = payload.event_type
    if payload.title is not None:
        title = payload.title.strip()
        if not title:
            raise HTTPException(400, "title 不能为空")
        row.title = title
    if payload.summary is not None:
        row.summary = payload.summary.strip() or None
    if payload.importance is not None:
        row.importance = int(payload.importance)
    db.commit()
    db.refresh(row)
    return _event_mark_to_dict(row)


@router.delete("/boards/events/{mark_id}")
def delete_board_event_mark(mark_id: int, db: Session = Depends(get_db)):
    row = db.query(BoardEventMark).filter(BoardEventMark.id == mark_id).first()
    if not row:
        raise HTTPException(404, "事件标注不存在")
    db.delete(row)
    db.commit()
    return {"ok": True}


@router.post("/boards/refresh")
async def refresh_watched_boards(payload: BoardRefreshRequest, db: Session = Depends(get_db)):
    market = (payload.market or "CN").strip().upper()
    if market != "CN":
        raise HTTPException(400, "v1 仅支持 A 股行业板块")
    days = max(30, min(int(payload.days or DEFAULT_BOARD_DAYS), 250))
    codes = [x.strip() for x in (payload.board_codes or []) if x.strip()]
    if not codes:
        rows = (
            db.query(WatchedBoard)
            .filter(WatchedBoard.market == market, WatchedBoard.enabled == True)  # noqa: E712
            .order_by(WatchedBoard.sort_order.asc(), WatchedBoard.id.asc())
            .all()
        )
        codes = [x.board_code for x in rows]
    collector = _collector()
    results = []
    for code in codes[:MAX_WATCHED_BOARDS]:
        try:
            updated = await _refresh_one_board(db, market=market, board_code=code, days=days, collector=collector)
            results.append({"board_code": code, "updated": updated, "ok": updated > 0})
        except Exception as e:
            db.rollback()
            results.append({"board_code": code, "updated": 0, "ok": False, "error": str(e)})
    return {
        "market": market,
        "days": days,
        "count": len(results),
        "updated": sum(int(x.get("updated") or 0) for x in results),
        "results": results,
    }


@router.get("/boards/{board_code}/kline")
async def get_board_kline(
    board_code: str,
    market: str = Query("CN"),
    days: int = Query(DEFAULT_BOARD_DAYS, ge=30, le=MAX_BOARD_DAYS),
    interval: str = Query("1d", pattern="^(1d|1w)$"),
    db: Session = Depends(get_db),
):
    market = (market or "CN").strip().upper()
    code = (board_code or "").strip()  # 不转大写:腾讯板块代码为小写 pt…,东方财富已是大写 BK…
    if market != "CN":
        raise HTTPException(400, "v1 仅支持 A 股行业板块")
    rows = await _ensure_board_klines(db, market, code, days)
    klines = _serialize_kline_rows(rows)
    if interval == "1w":
        klines = _aggregate_weekly(klines)
    return {
        "symbol": code,
        "market": market,
        "days": days,
        "interval": interval,
        "klines": klines,
    }


@router.get("/boards/{board_code}/signals")
async def get_board_signals(
    board_code: str,
    market: str = Query("CN"),
    days: int = Query(DEFAULT_BOARD_DAYS, ge=30, le=MAX_BOARD_DAYS),
    db: Session = Depends(get_db),
):
    market = (market or "CN").strip().upper()
    code = (board_code or "").strip()  # 不转大写:腾讯板块代码为小写 pt…,东方财富已是大写 BK…
    if market != "CN":
        raise HTTPException(400, "v1 仅支持 A 股行业板块")
    rows = await _ensure_board_klines(db, market, code, days)
    signal = build_board_signal(_serialize_kline_rows(rows))
    watch = (
        db.query(WatchedBoard)
        .filter(WatchedBoard.market == market, WatchedBoard.board_code == code)
        .first()
    )
    leaders = await _leaders_for_board(code, limit=5)
    return {
        "market": market,
        "board_code": code,
        "board_name": watch.board_name if watch else code,
        "days": days,
        "leaders": leaders,
        **signal,
    }
