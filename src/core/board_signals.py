"""Board kline technical signal helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class BoardBar:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None
    turnover: float | None = None


def _num(value: Any) -> float | None:
    try:
        n = float(value)
        return n
    except Exception:
        return None


def _bar_from_any(item: Any) -> BoardBar | None:
    if isinstance(item, dict):
        date = str(item.get("date") or "")
        open_v = _num(item.get("open"))
        high_v = _num(item.get("high"))
        low_v = _num(item.get("low"))
        close_v = _num(item.get("close"))
        volume = _num(item.get("volume"))
        turnover = _num(item.get("turnover"))
    else:
        date = str(getattr(item, "date", "") or "")
        open_v = _num(getattr(item, "open", None))
        high_v = _num(getattr(item, "high", None))
        low_v = _num(getattr(item, "low", None))
        close_v = _num(getattr(item, "close", None))
        volume = _num(getattr(item, "volume", None))
        turnover = _num(getattr(item, "turnover", None))
    if not date or open_v is None or high_v is None or low_v is None or close_v is None:
        return None
    return BoardBar(
        date=date,
        open=open_v,
        high=high_v,
        low=low_v,
        close=close_v,
        volume=volume,
        turnover=turnover,
    )


def normalize_bars(items: list[Any]) -> list[BoardBar]:
    bars = [_bar_from_any(x) for x in items or []]
    out = [x for x in bars if x is not None]
    out.sort(key=lambda x: x.date)
    return out


def _sma(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period <= 0:
        return out
    rolling = 0.0
    for i, value in enumerate(values):
        rolling += value
        if i >= period:
            rolling -= values[i - period]
        if i >= period - 1:
            out[i] = rolling / period
    return out


def _ema(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if not values or period <= 0:
        return out
    k = 2 / (period + 1)
    prev = values[0]
    for i, value in enumerate(values):
        if i == 0:
            prev = value
        else:
            prev = value * k + prev * (1 - k)
        out[i] = prev
    return out


def _macd(closes: list[float]) -> tuple[list[float | None], list[float | None], list[float | None]]:
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    dif: list[float | None] = []
    for a, b in zip(ema12, ema26):
        dif.append(None if a is None or b is None else a - b)
    signal_input = [x if x is not None else 0.0 for x in dif]
    dea = _ema(signal_input, 9)
    hist: list[float | None] = []
    for d, e in zip(dif, dea):
        hist.append(None if d is None or e is None else (d - e) * 2)
    return dif, dea, hist


def _rsi(closes: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return out
    gain = 0.0
    loss = 0.0
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gain += diff
        else:
            loss += -diff
    avg_gain = gain / period
    avg_loss = loss / period
    out[period] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        g = max(diff, 0.0)
        l = max(-diff, 0.0)
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
        out[i] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    return out


def _pct(curr: float, prev: float | None) -> float | None:
    if prev is None or prev == 0:
        return None
    return (curr - prev) / prev * 100


def _macd_label(dif: float | None, dea: float | None, prev_dif: float | None, prev_dea: float | None) -> str:
    if dif is None or dea is None:
        return "insufficient"
    if prev_dif is not None and prev_dea is not None:
        if prev_dif <= prev_dea and dif > dea:
            return "golden_cross"
        if prev_dif >= prev_dea and dif < dea:
            return "dead_cross"
    if dif > dea and dif > 0:
        return "bullish_above_zero"
    if dif > dea:
        return "bullish_repair"
    if dif < dea and dif < 0:
        return "bearish_below_zero"
    return "bearish_fade"


def _rsi_label(value: float | None) -> str:
    if value is None:
        return "insufficient"
    if value >= 80:
        return "overbought"
    if value >= 65:
        return "strong"
    if value <= 20:
        return "oversold"
    if value <= 35:
        return "weak"
    return "neutral"


def _trend_state(score: float, change_5d: float | None, macd_state: str, rsi_state: str) -> tuple[str, str]:
    if score >= 72:
        return "strong_inflow", "资金轮动偏强，趋势和动量共振"
    if score >= 58:
        return "active", "板块处在活跃轮动区间"
    if macd_state.startswith("bearish") or (change_5d is not None and change_5d < -3):
        return "cooling", "趋势降温，短线资金有撤退迹象"
    if rsi_state in ("oversold", "weak"):
        return "repair_watch", "处于修复观察区，等待放量确认"
    return "neutral", "信号中性，以观察量价配合为主"


def build_board_signal(items: list[Any]) -> dict:
    bars = normalize_bars(items)
    closes = [x.close for x in bars]
    ma5 = _sma(closes, 5)
    ma10 = _sma(closes, 10)
    ma20 = _sma(closes, 20)
    dif, dea, hist = _macd(closes)
    rsi6 = _rsi(closes, 6)
    rsi12 = _rsi(closes, 12)

    series = []
    for i, bar in enumerate(bars):
        series.append(
            {
                "date": bar.date,
                "open": round(bar.open, 4),
                "high": round(bar.high, 4),
                "low": round(bar.low, 4),
                "close": round(bar.close, 4),
                "volume": bar.volume,
                "turnover": bar.turnover,
                "ma5": None if ma5[i] is None else round(ma5[i], 4),
                "ma10": None if ma10[i] is None else round(ma10[i], 4),
                "ma20": None if ma20[i] is None else round(ma20[i], 4),
                "macd_dif": None if dif[i] is None else round(dif[i], 4),
                "macd_dea": None if dea[i] is None else round(dea[i], 4),
                "macd_hist": None if hist[i] is None else round(hist[i], 4),
                "rsi6": None if rsi6[i] is None else round(rsi6[i], 2),
                "rsi12": None if rsi12[i] is None else round(rsi12[i], 2),
            }
        )

    if not bars:
        return {
            "available": False,
            "asof": None,
            "last_close": None,
            "change_1d_pct": None,
            "change_5d_pct": None,
            "change_20d_pct": None,
            "macd_state": "insufficient",
            "macd_label": "数据不足",
            "rsi_state": "insufficient",
            "rsi_label": "数据不足",
            "trend_score": 0.0,
            "rotation_state": "insufficient",
            "rotation_label": "数据不足",
            "summary": "板块日K数据不足，暂不能生成技术信号。",
            "series": [],
        }

    last_i = len(bars) - 1
    last_close = closes[-1]
    change_1d = _pct(last_close, closes[-2] if len(closes) >= 2 else None)
    change_5d = _pct(last_close, closes[-6] if len(closes) >= 6 else None)
    change_20d = _pct(last_close, closes[-21] if len(closes) >= 21 else None)

    macd_state = (
        "insufficient"
        if len(closes) < 35
        else _macd_label(
            dif[last_i],
            dea[last_i],
            dif[last_i - 1] if last_i >= 1 else None,
            dea[last_i - 1] if last_i >= 1 else None,
        )
    )
    rsi_state = _rsi_label(rsi6[last_i])
    ma_score = 0.0
    if ma5[last_i] is not None and ma10[last_i] is not None and last_close >= ma5[last_i] >= ma10[last_i]:
        ma_score += 22
    if ma20[last_i] is not None and last_close >= ma20[last_i]:
        ma_score += 14
    macd_score = 24 if macd_state in ("golden_cross", "bullish_above_zero") else 12 if macd_state == "bullish_repair" else 0
    rsi_score = 18 if rsi_state in ("strong", "neutral") else 8 if rsi_state in ("oversold", "weak") else 10
    momentum_score = max(0.0, min(22.0, 11.0 + (change_5d or 0.0) * 2.2))
    trend_score = round(max(0.0, min(100.0, ma_score + macd_score + rsi_score + momentum_score)), 1)
    rotation_state, rotation_label = _trend_state(trend_score, change_5d, macd_state, rsi_state)

    macd_labels = {
        "golden_cross": "MACD金叉",
        "dead_cross": "MACD死叉",
        "bullish_above_zero": "多头零轴上方",
        "bullish_repair": "多头修复",
        "bearish_below_zero": "空头零轴下方",
        "bearish_fade": "动能走弱",
        "insufficient": "数据不足",
    }
    rsi_labels = {
        "overbought": "RSI超买",
        "strong": "RSI强势",
        "neutral": "RSI中性",
        "weak": "RSI偏弱",
        "oversold": "RSI超卖",
        "insufficient": "数据不足",
    }
    summary = (
        f"{rotation_label}。近5日涨跌幅"
        f"{'--' if change_5d is None else f'{change_5d:+.2f}%'}，"
        f"{macd_labels.get(macd_state, macd_state)}，{rsi_labels.get(rsi_state, rsi_state)}。"
    )

    return {
        "available": len(bars) >= 30,
        "asof": bars[-1].date,
        "last_close": round(last_close, 4),
        "change_1d_pct": None if change_1d is None else round(change_1d, 2),
        "change_5d_pct": None if change_5d is None else round(change_5d, 2),
        "change_20d_pct": None if change_20d is None else round(change_20d, 2),
        "macd_state": macd_state,
        "macd_label": macd_labels.get(macd_state, macd_state),
        "rsi_state": rsi_state,
        "rsi_label": rsi_labels.get(rsi_state, rsi_state),
        "rsi6": None if rsi6[last_i] is None else round(rsi6[last_i], 2),
        "rsi12": None if rsi12[last_i] is None else round(rsi12[last_i], 2),
        "trend_score": trend_score,
        "rotation_state": rotation_state,
        "rotation_label": rotation_label,
        "summary": summary,
        "series": series,
    }
