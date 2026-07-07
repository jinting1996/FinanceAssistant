"""大盘情绪与板块资金流:基于 akshare(东财/乐咕)接口,带内存缓存。"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

CACHE_TTL_SEC = 300  # 盘中资金流/情绪 5 分钟刷新足够

_cache_lock = threading.Lock()
_cache: dict[str, Any] = {"ts": 0.0, "data": None}

YI = 100_000_000.0  # 亿


def _pick_column(columns: list[str], *keywords: str) -> str | None:
    for col in columns:
        if all(kw in str(col) for kw in keywords):
            return str(col)
    return None


def fetch_sector_flows(top_n: int = 5) -> list[dict[str, Any]]:
    """行业板块今日主力净流入排行(前 top_n 流入 + 后 top_n 流出)。"""
    import akshare as ak

    import pandas as pd

    df = ak.stock_sector_fund_flow_rank(indicator="今日", sector_type="行业资金流")
    if df is None or df.empty:
        raise ValueError("行业资金流返回为空")
    cols = df.columns.tolist()
    name_col = _pick_column(cols, "名称")
    change_col = _pick_column(cols, "涨跌幅")
    amount_col = _pick_column(cols, "主力净流入", "净额")
    ratio_col = _pick_column(cols, "主力净流入", "净占比")
    if not name_col or not amount_col:
        raise ValueError(f"行业资金流字段不符合预期: {cols}")

    # 数据源里 '-' 之类的脏值按行跳过,不拖垮整个列表
    df = df.copy()
    df[amount_col] = pd.to_numeric(df[amount_col], errors="coerce")
    df = df.dropna(subset=[amount_col]).sort_values(amount_col, ascending=False)
    if df.empty:
        raise ValueError("行业资金流净额列无有效数值")

    def _num(row, col) -> float | None:
        if not col:
            return None
        try:
            value = float(row[col])
        except Exception:
            return None
        return value if value == value else None  # NaN 过滤

    rows = []
    picked = list(df.head(top_n).iterrows()) + list(df.tail(top_n).iterrows())
    seen: set[str] = set()
    for _, row in picked:
        name = str(row[name_col])
        if name in seen:
            continue
        seen.add(name)
        ratio = _num(row, ratio_col)
        change = _num(row, change_col)
        rows.append(
            {
                "name": name,
                "main_net_inflow_yi": round(float(row[amount_col]) / YI, 2),
                "main_net_ratio_pct": round(ratio, 2) if ratio is not None else None,
                "change_pct": round(change, 2) if change is not None else None,
            }
        )
    return rows


def fetch_market_flow() -> dict[str, Any]:
    """两市大盘主力净流入(当日,单位亿)与沪深指数涨跌幅。"""
    import akshare as ak

    df = ak.stock_market_fund_flow()
    if df is None or df.empty:
        raise ValueError("大盘资金流为空")
    cols = df.columns.tolist()
    main_col = _pick_column(cols, "主力净流入", "净额")
    sh_change_col = _pick_column(cols, "上证", "涨跌幅")
    sz_change_col = _pick_column(cols, "深证", "涨跌幅")
    last = df.iloc[-1]

    def _num(col) -> float | None:
        if not col:
            return None
        try:
            value = float(last[col])
        except Exception:
            return None
        return value if value == value else None  # NaN 过滤

    main = _num(main_col)
    sh = _num(sh_change_col)
    sz = _num(sz_change_col)
    return {
        "date": str(last[cols[0]]),
        "main_net_inflow_yi": round(main / YI, 2) if main is not None else None,
        "sh_change_pct": round(sh, 2) if sh is not None else None,
        "sz_change_pct": round(sz, 2) if sz is not None else None,
    }


def fetch_market_activity() -> dict[str, Any]:
    """全市场赚钱效应:上涨/下跌/涨停/跌停家数与活跃度。"""
    import akshare as ak

    df = ak.stock_market_activity_legu()
    kv = {str(row["item"]).strip(): row["value"] for _, row in df.iterrows()}

    def _int(key: str) -> int | None:
        try:
            return int(float(kv[key]))
        except Exception:
            return None

    activity_raw = kv.get("活跃度")
    try:
        activity = float(str(activity_raw).replace("%", ""))
    except Exception:
        activity = None
    return {
        "up_count": _int("上涨"),
        "down_count": _int("下跌"),
        "limit_up_count": _int("涨停"),
        "limit_down_count": _int("跌停"),
        "activity_pct": activity,
        "stat_date": str(kv.get("统计日期") or ""),
    }


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def compute_sentiment(
    *,
    up_count: int | None,
    down_count: int | None,
    limit_up_count: int | None,
    limit_down_count: int | None,
    main_net_inflow_yi: float | None,
) -> dict[str, Any]:
    """合成大盘情绪分(0-100):广度50% + 涨跌停强度25% + 主力资金25%。

    各输入缺失时该项按中性(0.5)计,并降低置信度。
    """
    parts: dict[str, float] = {}
    missing = 0

    if up_count is not None and down_count is not None and (up_count + down_count) > 0:
        parts["breadth"] = up_count / (up_count + down_count)
    else:
        parts["breadth"] = 0.5
        missing += 1

    if limit_up_count is not None and limit_down_count is not None:
        # 涨停显著多于跌停 → 偏热;+1 防除零并弱化小样本
        raw = (limit_up_count - limit_down_count) / (limit_up_count + limit_down_count + 1)
        parts["limit"] = (_clamp(raw, -1.0, 1.0) + 1.0) / 2.0
    else:
        parts["limit"] = 0.5
        missing += 1

    if main_net_inflow_yi is not None:
        # 两市主力净流入 ±300 亿视为满格
        parts["flow"] = (_clamp(main_net_inflow_yi / 300.0, -1.0, 1.0) + 1.0) / 2.0
    else:
        parts["flow"] = 0.5
        missing += 1

    score = 100.0 * (0.5 * parts["breadth"] + 0.25 * parts["limit"] + 0.25 * parts["flow"])
    score = round(_clamp(score, 0.0, 100.0), 1)
    if score < 20:
        label = "冰点"
    elif score < 40:
        label = "偏冷"
    elif score < 60:
        label = "中性"
    elif score < 80:
        label = "偏暖"
    else:
        label = "亢奋"
    return {
        "score": score,
        "label": label,
        "confidence": round(1.0 - missing / 3.0, 2),
        "parts": {k: round(v, 4) for k, v in parts.items()},
    }


def _build_snapshot(top_n: int) -> dict[str, Any]:
    data: dict[str, Any] = {
        "sector_flows": [],
        "market_flow": None,
        "activity": None,
        "sentiment": None,
        "errors": [],
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        data["sector_flows"] = fetch_sector_flows(top_n=top_n)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[market_mood] 板块资金流获取失败: %s", exc)
        data["errors"].append(f"板块资金流: {exc}")
    try:
        data["market_flow"] = fetch_market_flow()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[market_mood] 大盘资金流获取失败: %s", exc)
        data["errors"].append(f"大盘资金流: {exc}")
    try:
        data["activity"] = fetch_market_activity()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[market_mood] 市场活跃度获取失败: %s", exc)
        data["errors"].append(f"市场活跃度: {exc}")

    activity = data["activity"] or {}
    market_flow = data["market_flow"] or {}
    data["sentiment"] = compute_sentiment(
        up_count=activity.get("up_count"),
        down_count=activity.get("down_count"),
        limit_up_count=activity.get("limit_up_count"),
        limit_down_count=activity.get("limit_down_count"),
        main_net_inflow_yi=market_flow.get("main_net_inflow_yi"),
    )
    return data


def get_market_mood(*, top_n: int = 5, force_refresh: bool = False) -> dict[str, Any]:
    """带 5 分钟缓存的大盘情绪+板块资金流快照。"""
    now = time.time()
    with _cache_lock:
        cached = _cache["data"]
        if not force_refresh and cached is not None and now - _cache["ts"] < CACHE_TTL_SEC:
            return cached
    data = _build_snapshot(top_n)
    with _cache_lock:
        # 全部失败时不覆盖上一次的有效缓存
        if data["errors"] and not any([data["sector_flows"], data["market_flow"], data["activity"]]):
            if _cache["data"] is not None:
                return _cache["data"]
        _cache["data"] = data
        _cache["ts"] = now
    return data
