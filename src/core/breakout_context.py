"""突破结构与量价识别。

给策略 AI 分析喂结构化信息，解决两个老问题：
1. 放量涨停识别不到 —— 之前只喂裸 OHLCV，AI 拿不到涨跌幅/量比/涨停线。
2. 前高乱锚定 —— 之前压力位一直是空的，AI 把每天新高都当前高、或把突破后的
   新高当成本次突破前高。这里用「已确认摆动高点(swing high)」定位突破*前*的
   前高锚点，突破后不随新高上移。

纯函数、无 I/O，便于单测。输入为按时间升序的日K（KlineData 或含
date/open/high/low/close/volume 的对象/字典均可）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.core.signals.base_position_vwap_t import cn_price_limit_ratio


def _value(row: Any, key: str, default: Any = None) -> Any:
    return row.get(key, default) if isinstance(row, dict) else getattr(row, key, default)


def _f(value: Any, default: float | None = None) -> float | None:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _pct_change(close: float | None, prev_close: float | None) -> float | None:
    if close is None or not prev_close:
        return None
    return (close / prev_close - 1.0) * 100.0


def is_limit_move(change_pct: float | None, limit_ratio: float, *, tol: float = 0.3) -> int:
    """返回 +1=涨停 / -1=跌停 / 0=否。tol 容忍集合竞价/四舍五入的零点几个百分点。"""
    if change_pct is None or not limit_ratio:
        return 0
    line = limit_ratio * 100.0
    if change_pct >= line - tol:
        return 1
    if change_pct <= -line + tol:
        return -1
    return 0


def find_pivot_highs(highs: list[float], k: int = 5) -> list[int]:
    """已确认摆动高点：该K高点是 [i-k, i+k] 窗口内最大值。

    需要右侧 k 根确认，因此最近 k 根不可能成为 pivot —— 这正好避免把当前上涨
    途中的新高误当成「前高」。
    """
    n = len(highs)
    out: list[int] = []
    for i in range(k, n - k):
        h = highs[i]
        if h is None or h <= 0:
            continue
        window = highs[i - k : i + k + 1]
        if all(v is not None for v in window) and h >= max(window):
            out.append(i)
    return out


@dataclass(frozen=True)
class BreakoutInfo:
    has_data: bool
    prev_high: float | None = None          # 突破前高锚点（突破确认后固定，不随新高移动）
    prev_high_date: str = ""
    broke: bool = False                      # 现价是否已站上该前高
    breakout_date: str = ""                  # 首次收盘突破前高的日期
    days_since_breakout: int | None = None
    confirmed: bool = False                  # 突破后现价仍站在前高之上
    post_breakout_high: float | None = None  # 突破后创出的最高价（≠前高）
    gap_to_prev_high_pct: float | None = None  # 现价相对前高的百分比
    summary: str = ""


def analyze_breakout(
    klines: list[Any],
    *,
    pivot_k: int = 5,
    min_bars: int = 20,
) -> BreakoutInfo:
    """定位突破前高锚点与突破状态。"""
    if not klines or len(klines) < min_bars:
        return BreakoutInfo(has_data=False)

    highs = [_f(_value(k, "high")) for k in klines]
    closes = [_f(_value(k, "close")) for k in klines]
    dates = [str(_value(k, "date") or "")[:10] for k in klines]
    n = len(klines)
    current = closes[-1]
    if current is None or current <= 0:
        return BreakoutInfo(has_data=False)

    pivots = find_pivot_highs(highs, k=pivot_k)
    if not pivots:
        return BreakoutInfo(has_data=False)

    broken = [i for i in pivots if highs[i] is not None and highs[i] < current]
    if broken:
        # 已突破：锚点取「被突破的最强前高」（被清除的最高阻力）
        anchor = max(broken, key=lambda i: highs[i])
        prev_high = highs[anchor]
        breakout_i = next(
            (j for j in range(anchor + 1, n) if closes[j] is not None and closes[j] > prev_high),
            None,
        )
        seg = highs[(breakout_i if breakout_i is not None else anchor + 1) :]
        post_high = max((h for h in seg if h is not None), default=None)
        days_since = (n - 1 - breakout_i) if breakout_i is not None else None
        confirmed = current > prev_high
        gap = _pct_change(current, prev_high)
        if confirmed:
            summary = (
                f"已突破前高 {prev_high:.3f}（{dates[anchor]} 形成）"
                + (f"，突破日 {dates[breakout_i]}、距今 {days_since} 日" if breakout_i is not None else "")
                + f"；突破后最高 {post_high:.3f}，现价 {current:.3f}（距前高 {gap:+.1f}%）。"
                "前高锚点固定为该值，突破后的新高不改变前高。"
            )
        else:
            summary = (
                f"曾上破前高 {prev_high:.3f}（{dates[anchor]} 形成）但现价 {current:.3f} 已回落至其下方"
                f"（{gap:+.1f}%），突破有效性存疑。"
            )
        return BreakoutInfo(
            has_data=True,
            prev_high=round(prev_high, 4),
            prev_high_date=dates[anchor],
            broke=True,
            breakout_date=dates[breakout_i] if breakout_i is not None else "",
            days_since_breakout=days_since,
            confirmed=confirmed,
            post_breakout_high=round(post_high, 4) if post_high is not None else None,
            gap_to_prev_high_pct=round(gap, 2) if gap is not None else None,
            summary=summary,
        )

    # 未突破：现价仍在前高之下，取最近的上方阻力为「待突破前高」（等于现价的平台不算上方）
    above = [i for i in pivots if highs[i] is not None and highs[i] > current]
    anchor = min(above, key=lambda i: highs[i]) if above else max(pivots, key=lambda i: highs[i])
    prev_high = highs[anchor]
    gap = _pct_change(current, prev_high)
    return BreakoutInfo(
        has_data=True,
        prev_high=round(prev_high, 4),
        prev_high_date=dates[anchor],
        broke=False,
        gap_to_prev_high_pct=round(gap, 2) if gap is not None else None,
        summary=(
            f"尚未突破上方前高 {prev_high:.3f}（{dates[anchor]} 形成），"
            f"现价 {current:.3f} 距前高 {gap:+.1f}%，属突破前/待突破。"
        ),
    )


def format_daily_klines(
    klines: list[Any],
    *,
    symbol: str = "",
    name: str = "",
    vol_window: int = 5,
    vol_surge: float = 1.5,
) -> list[str]:
    """把日K格式化为带涨跌幅、涨停/跌停、放量标记的行，供 AI 定位放量涨停。"""
    limit_ratio = cn_price_limit_ratio(symbol, name)
    lines = ["## 最近日K（日期 开 高 低 收 量 涨跌幅 标记）"]
    closes = [_f(_value(k, "close")) for k in klines]
    vols = [_f(_value(k, "volume"), 0.0) or 0.0 for k in klines]
    for i, k in enumerate(klines):
        o = _f(_value(k, "open"))
        h = _f(_value(k, "high"))
        low = _f(_value(k, "low"))
        c = closes[i]
        vol = int(vols[i]) if vols[i] else 0
        chg = _pct_change(c, closes[i - 1]) if i > 0 else None
        marks: list[str] = []
        lm = is_limit_move(chg, limit_ratio)
        if lm > 0:
            marks.append("涨停")
        elif lm < 0:
            marks.append("跌停")
        if i >= vol_window:
            avg = sum(vols[i - vol_window : i]) / vol_window
            if avg > 0 and vols[i] / avg >= vol_surge:
                marks.append(f"放量x{vols[i] / avg:.1f}")
        chg_str = f"{chg:+.1f}%" if chg is not None else "--"
        mark_str = (" " + "/".join(marks)) if marks else ""
        lines.append(f"{str(_value(k, 'date'))[:10]} {o} {h} {low} {c} {vol} {chg_str}{mark_str}")
    return lines


def build_limit_line(
    change_pct: float | None,
    prev_close: float | None,
    *,
    symbol: str = "",
    name: str = "",
) -> str:
    """当日涨跌停线 + 是否触及，供实时行情块使用。"""
    ratio = cn_price_limit_ratio(symbol, name)
    if not ratio or not prev_close:
        return ""
    up = round(prev_close * (1 + ratio), 2)
    down = round(prev_close * (1 - ratio), 2)
    lm = is_limit_move(change_pct, ratio)
    status = "涨停" if lm > 0 else ("跌停" if lm < 0 else "未触及涨跌停")
    return f"- 涨跌停线：{up} / {down}（{int(ratio * 100)}%），当前{status}"
