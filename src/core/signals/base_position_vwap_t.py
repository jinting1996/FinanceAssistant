"""底仓 VWAP 回归做 T 的纯信号计算。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


def _value(row: Any, key: str, default: Any = None) -> Any:
    return row.get(key, default) if isinstance(row, dict) else getattr(row, key, default)


def _float(value: Any, default: float | None = None) -> float | None:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _atr(rows: list[Any], period: int = 14) -> float | None:
    if len(rows) < period + 1:
        return None
    values: list[float] = []
    for index in range(1, len(rows)):
        high = _float(_value(rows[index], "high"))
        low = _float(_value(rows[index], "low"))
        previous_close = _float(_value(rows[index - 1], "close"))
        if high is None or low is None or previous_close is None:
            continue
        values.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    return sum(values[-period:]) / period if len(values) >= period else None


def compute_intraday_vwap(rows: list[Any]) -> tuple[float | None, str]:
    """优先使用成交额，字段缺失或单位异常时回退到典型价成交量加权。"""
    total_volume = 0.0
    total_amount = 0.0
    estimated_amount = 0.0
    amount_complete = True
    last_close: float | None = None
    for row in rows:
        volume = max(_float(_value(row, "volume"), 0.0) or 0.0, 0.0)
        close = _float(_value(row, "close"))
        high = _float(_value(row, "high"), close)
        low = _float(_value(row, "low"), close)
        amount = _float(_value(row, "amount"))
        if close is None or high is None or low is None or volume <= 0:
            continue
        total_volume += volume
        estimated_amount += ((high + low + close) / 3.0) * volume
        last_close = close
        if amount is None or amount <= 0:
            amount_complete = False
        else:
            total_amount += amount
    if total_volume <= 0 or last_close is None:
        return None, "missing"
    if amount_complete and total_amount > 0:
        raw = total_amount / total_volume
        candidates = (raw, raw / 100.0)
        valid = [x for x in candidates if 0.25 <= x / last_close <= 4.0]
        if valid:
            return min(valid, key=lambda x: abs(x - last_close)), "amount"
    return estimated_amount / total_volume, "estimated"


@dataclass(frozen=True)
class TSignalResult:
    valid: bool
    action: str
    score: int
    reason: str
    evidence: list[str]
    hard_blocks: list[str]
    current_price: float | None
    vwap: float | None
    support_price: float | None
    stop_loss_price: float | None
    target_price: float | None
    data_quality: str
    metrics: dict[str, float | bool | None]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_base_position_vwap_t(
    daily_klines: list[Any],
    minute_klines: list[Any],
    *,
    min_score: int = 70,
    min_vwap_deviation_pct: float = 0.003,
    min_profit_pct: float = 0.008,
    max_stop_pct: float = 0.015,
) -> TSignalResult:
    """计算低吸 T 信号；持仓、资金与数量约束由状态机处理。"""
    if len(daily_klines) < 25 or len(minute_klines) < 3:
        return TSignalResult(False, "observe", 0, "K线数据不足", [], ["K线数据不足"], None, None, None, None, None, "missing", {})

    daily = daily_klines[-80:]
    minute = minute_klines[-320:]
    closes = [_float(_value(row, "close")) for row in daily]
    lows = [_float(_value(row, "low")) for row in daily]
    if any(value is None for value in closes[-25:] + lows[-20:]):
        return TSignalResult(False, "observe", 0, "日K字段不完整", [], ["日K字段不完整"], None, None, None, None, None, "missing", {})

    current = _float(_value(minute[-1], "close"))
    vwap, quality = compute_intraday_vwap(minute)
    atr = _atr(daily, 14)
    if current is None or current <= 0 or vwap is None or atr is None:
        return TSignalResult(False, "observe", 0, "无法计算当前价、VWAP或ATR", [], ["关键指标缺失"], current, vwap, None, None, None, quality, {})

    valid_closes = [float(x) for x in closes if x is not None]
    valid_lows = [float(x) for x in lows if x is not None]
    ma10 = sum(valid_closes[-10:]) / 10
    ma20 = sum(valid_closes[-20:]) / 20
    previous_ma20 = sum(valid_closes[-25:-5]) / 20
    ma20_slope = (ma20 / previous_ma20 - 1.0) if previous_ma20 else 0.0
    yesterday_low = valid_lows[-1]
    support_candidates = [ma10, ma20, min(valid_lows[-20:]), yesterday_low]
    below = [level for level in support_candidates if level <= current * 1.005]
    support = max(below) if below else min(support_candidates, key=lambda level: abs(level - current))
    support_distance = abs(current - support) / current
    vwap_deviation = (current / vwap) - 1.0

    last_three_lows = [_float(_value(row, "low")) for row in minute[-3:]]
    previous_close = _float(_value(minute[-2], "close"), current) or current
    reversal = bool(
        all(value is not None for value in last_three_lows)
        and last_three_lows[0] <= last_three_lows[1] <= last_three_lows[2]
        and current > previous_close
    )
    trend_ok = current >= ma20 * 0.985 and ma20_slope >= -0.003
    near_support = support_distance <= max(0.004, 0.15 * atr / current)
    below_vwap = vwap_deviation <= -max(min_vwap_deviation_pct, 0.2 * atr / current)

    stop = min(support - 0.1 * atr, current - 0.2 * atr)
    stop_risk = max((current - stop) / current, 0.0)
    target = max(vwap, current * (1.0 + min_profit_pct))
    reward_risk = (target - current) / max(current - stop, 1e-9)

    evidence: list[str] = []
    score = 0
    if trend_ok:
        score += 20
        evidence.append("日线趋势未破且 MA20 未明显向下")
    if near_support:
        score += 20
        evidence.append(f"当前价接近支撑位 {support:.3f}")
    if below_vwap:
        score += 15
        evidence.append(f"当前价低于 VWAP {vwap:.3f}")
    if reversal:
        score += 20
        evidence.append("最近三根分钟K低点抬高并出现止跌")
    if len(minute) >= 20:
        score += 10
        evidence.append("分钟数据覆盖满足盘中判断")
    if reward_risk >= 1.0:
        score += 15
        evidence.append(f"预期盈亏比 {reward_risk:.2f}")

    hard_blocks: list[str] = []
    if not trend_ok:
        hard_blocks.append("跌破 MA20 或 MA20 明显向下")
    if stop_risk > max_stop_pct:
        hard_blocks.append(f"止损距离 {stop_risk:.2%} 超过上限")
    if current <= support - 0.2 * atr:
        hard_blocks.append("已有效跌破关键支撑")
    action = "buy_t" if score >= min_score and not hard_blocks else "observe"
    reason = "；".join(evidence) if action == "buy_t" else "；".join(hard_blocks or evidence or ["条件未满足"])
    return TSignalResult(
        valid=not hard_blocks,
        action=action,
        score=min(score, 100),
        reason=reason,
        evidence=evidence,
        hard_blocks=hard_blocks,
        current_price=round(current, 4),
        vwap=round(vwap, 4),
        support_price=round(support, 4),
        stop_loss_price=round(stop, 4),
        target_price=round(target, 4),
        data_quality=quality,
        metrics={
            "ma10": round(ma10, 4),
            "ma20": round(ma20, 4),
            "ma20_slope": round(ma20_slope, 6),
            "atr14": round(atr, 4),
            "vwap_deviation": round(vwap_deviation, 6),
            "support_distance": round(support_distance, 6),
            "stop_risk": round(stop_risk, 6),
            "reward_risk": round(reward_risk, 4),
            "reversal": reversal,
        },
    )


def evaluate_t_exit(
    current_price: float,
    *,
    vwap: float,
    target_price: float,
    stop_loss_price: float,
) -> str:
    """返回 sell_t / invalidated / observe。"""
    if current_price <= stop_loss_price:
        return "invalidated"
    if current_price >= min(vwap, target_price):
        return "sell_t"
    return "observe"
