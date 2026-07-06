"""底仓 VWAP 回归做 T 的纯信号计算。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
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


def _latest_day_minutes(rows: list[Any]) -> list[Any]:
    """只取最新交易日当天的分钟K。分钟接口约返回 1.3 天数据,跨日会污染
    VWAP(应每日开盘归零)和分钟反转确认(早盘会跨隔夜)。"""
    if not rows:
        return rows
    last_day = str(_value(rows[-1], "date") or "")[:10]
    if not last_day:
        return rows
    return [row for row in rows if str(_value(row, "date") or "")[:10] == last_day]


def _exclude_today(daily_rows: list[Any], today: str) -> list[Any]:
    """剔除今日未收盘的日K。盘中日K的高/低/收仍在变动,若计入会让早盘 ATR
    被压低、昨日高低点取成今日盘中值——ATR/支撑压力须基于已收盘的完整日K,
    今日实时价由分钟数据的 current 代表。"""
    if not daily_rows or not today:
        return daily_rows
    if str(_value(daily_rows[-1], "date") or "")[:10] == today:
        return daily_rows[:-1]
    return daily_rows


def cn_price_limit_ratio(symbol: str, name: str = "") -> float:
    """A股涨跌停比例:创业板/科创板 20%,北交所 30%,ST 5%,其余主板 10%。"""
    s = (symbol or "").strip()
    if s.startswith(("300", "301", "688", "689")):
        return 0.20
    if s.startswith(("920", "8", "4")):
        return 0.30
    return 0.05 if "ST" in (name or "").upper() else 0.10


def _limit_position_block(
    *,
    direction: str,
    current: float,
    prev_close: float,
    ma20: float,
    closes: list[float],
    daily: list[Any],
    minute: list[Any],
    limit_ratio: float,
) -> str | None:
    """涨跌停 + 位置 + 量价闸门,返回拦截原因或 None。

    - 涨停时禁低吸(买不进);高抛仅在"高位+放量"(出货风险)才放行,否则强势持有不卖。
    - 跌停时禁高抛(卖不出);低吸仅在"低位+放量"(恐慌见底)才放行,否则不接刀。
    direction: 'long' 低吸(正T先买) / 'short' 高抛(倒T先卖)。
    """
    if not limit_ratio or prev_close <= 0:
        return None
    up_limit = round(prev_close * (1 + limit_ratio), 2)
    down_limit = round(prev_close * (1 - limit_ratio), 2)
    at_up = current >= up_limit - 0.01
    at_down = current <= down_limit + 0.01
    if not (at_up or at_down):
        return None
    dev_ma20 = (current / ma20 - 1.0) if ma20 else 0.0
    gain_10d = (current / closes[-10] - 1.0) if len(closes) >= 10 and closes[-10] else 0.0
    low_pos = dev_ma20 <= 0.08 and gain_10d <= 0.15
    high_pos = dev_ma20 >= 0.15 or gain_10d >= 0.30
    vols = [v for v in (_float(_value(x, "volume")) for x in daily) if v]
    avg5 = sum(vols[-5:]) / min(len(vols), 5) if vols else 0.0
    today_vol = sum((_float(_value(x, "volume")) or 0.0) for x in minute)
    vol_ratio = today_vol / avg5 if avg5 else 0.0
    heavy_vol = vol_ratio >= 1.8
    if direction == "short":  # 高抛/倒T:先卖
        if at_down:
            return "已跌停,无法卖出"
        if at_up and not (high_pos and heavy_vol):
            return (
                f"已涨停且非高位放量(乖离MA20 {dev_ma20:.1%}/量比 {vol_ratio:.2f}),"
                "强势持有不宜高抛"
            )
    else:  # 低吸/正T:先买
        if at_up:
            return "已涨停,无法买入低吸"
        if at_down and not (low_pos and heavy_vol):
            return "已跌停且非低位放量,不宜接刀低吸"
    return None


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
    # 各加分项是否得分(trend/support/vwap/reversal/coverage/reward),供前端标签展示
    score_detail: dict[str, bool] = field(default_factory=dict)

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
    profit_atr_mult: float = 0.5,
    stop_atr_mult: float = 0.5,
    limit_ratio: float | None = None,
) -> TSignalResult:
    """计算低吸 T 信号；持仓、资金与数量约束由状态机处理。"""
    if len(daily_klines) < 25 or len(minute_klines) < 3:
        return TSignalResult(False, "observe", 0, "K线数据不足", [], ["K线数据不足"], None, None, None, None, None, "missing", {})

    minute = _latest_day_minutes(minute_klines)[-320:]
    if len(minute) < 3:
        return TSignalResult(False, "observe", 0, "今日分钟数据不足", [], ["今日分钟数据不足"], None, None, None, None, None, "missing", {})
    today = str(_value(minute[-1], "date") or "")[:10]
    daily = _exclude_today(daily_klines, today)[-80:]
    if len(daily) < 25:
        return TSignalResult(False, "observe", 0, "K线数据不足", [], ["K线数据不足"], None, None, None, None, None, "missing", {})
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
    trend_ok = current >= ma20 * 0.97 and ma20_slope >= -0.02
    near_support = support_distance <= max(0.004, 0.15 * atr / current)
    below_vwap = vwap_deviation <= -max(min_vwap_deviation_pct, 0.2 * atr / current)

    # 止盈/止损上限按 ATR 自适应:固定值作地板,波动大时自动放大。
    atr_ratio = atr / current
    eff_profit = max(min_profit_pct, profit_atr_mult * atr_ratio)
    eff_stop_cap = max(max_stop_pct, stop_atr_mult * atr_ratio)
    stop = min(support - 0.1 * atr, current - 0.2 * atr)
    stop_risk = max((current - stop) / current, 0.0)
    target = max(vwap, current * (1.0 + eff_profit))
    reward_risk = (target - current) / max(current - stop, 1e-9)

    checks = {
        "trend": trend_ok,
        "support": near_support,
        "vwap": below_vwap,
        "reversal": reversal,
        "coverage": len(minute) >= 20,
        "reward": reward_risk >= 1.0,
    }
    evidence: list[str] = []
    score = 0
    if checks["trend"]:
        score += 20
        evidence.append("日线趋势未破且 MA20 未明显向下")
    if checks["support"]:
        score += 20
        evidence.append(f"当前价接近支撑位 {support:.3f}")
    if checks["vwap"]:
        score += 15
        evidence.append(f"当前价低于 VWAP {vwap:.3f}")
    if checks["reversal"]:
        score += 20
        evidence.append("最近三根分钟K低点抬高并出现止跌")
    if checks["coverage"]:
        score += 10
        evidence.append("分钟数据覆盖满足盘中判断")
    if checks["reward"]:
        score += 15
        evidence.append(f"预期盈亏比 {reward_risk:.2f}")

    hard_blocks: list[str] = []
    if not trend_ok:
        hard_blocks.append("跌破 MA20 或 MA20 明显向下")
    if stop_risk > eff_stop_cap:
        hard_blocks.append(f"止损距离 {stop_risk:.2%} 超过上限 {eff_stop_cap:.2%}")
    if current <= support - 0.2 * atr:
        hard_blocks.append("已有效跌破关键支撑")
    limit_block = _limit_position_block(
        direction="long", current=current, prev_close=valid_closes[-1], ma20=ma20,
        closes=valid_closes, daily=daily, minute=minute, limit_ratio=limit_ratio or 0.0,
    )
    if limit_block:
        hard_blocks.append(limit_block)
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
            "eff_profit_pct": round(eff_profit, 6),
            "eff_stop_cap_pct": round(eff_stop_cap, 6),
            "reversal": reversal,
        },
        score_detail={key: bool(value) for key, value in checks.items()},
    )


def evaluate_t_exit(
    current_price: float,
    *,
    vwap: float,
    target_price: float,
    stop_loss_price: float,
) -> str:
    """正T(先买后卖)离场:返回 sell_t / invalidated / observe。

    target_price 已在入场时取 max(vwap, 入场价×(1+eff_profit)),既保证至少回到
    VWAP 才止盈,又让 ATR 自适应目标真正驱动离场——故此处直接用 target_price,
    不再套 min(vwap, target)(那会把放宽的目标吞回 VWAP)。vwap 仅保留兼容签名。
    """
    if current_price <= stop_loss_price:
        return "invalidated"
    if current_price >= target_price:
        return "sell_t"
    return "observe"


def compute_base_position_vwap_t_short(
    daily_klines: list[Any],
    minute_klines: list[Any],
    *,
    min_score: int = 70,
    min_vwap_deviation_pct: float = 0.003,
    min_profit_pct: float = 0.008,
    max_stop_pct: float = 0.015,
    profit_atr_mult: float = 0.5,
    stop_atr_mult: float = 0.5,
    limit_ratio: float | None = None,
) -> TSignalResult:
    """计算高抛(倒T先卖)信号;正T 的镜像版本,卖出底仓等回落买回。"""
    if len(daily_klines) < 25 or len(minute_klines) < 3:
        return TSignalResult(False, "observe", 0, "K线数据不足", [], ["K线数据不足"], None, None, None, None, None, "missing", {})

    minute = _latest_day_minutes(minute_klines)[-320:]
    if len(minute) < 3:
        return TSignalResult(False, "observe", 0, "今日分钟数据不足", [], ["今日分钟数据不足"], None, None, None, None, None, "missing", {})
    today = str(_value(minute[-1], "date") or "")[:10]
    daily = _exclude_today(daily_klines, today)[-80:]
    if len(daily) < 25:
        return TSignalResult(False, "observe", 0, "K线数据不足", [], ["K线数据不足"], None, None, None, None, None, "missing", {})
    closes = [_float(_value(row, "close")) for row in daily]
    highs = [_float(_value(row, "high")) for row in daily]
    if any(value is None for value in closes[-25:] + highs[-20:]):
        return TSignalResult(False, "observe", 0, "日K字段不完整", [], ["日K字段不完整"], None, None, None, None, None, "missing", {})

    current = _float(_value(minute[-1], "close"))
    vwap, quality = compute_intraday_vwap(minute)
    atr = _atr(daily, 14)
    if current is None or current <= 0 or vwap is None or atr is None:
        return TSignalResult(False, "observe", 0, "无法计算当前价、VWAP或ATR", [], ["关键指标缺失"], current, vwap, None, None, None, quality, {})

    valid_closes = [float(x) for x in closes if x is not None]
    valid_highs = [float(x) for x in highs if x is not None]
    ma10 = sum(valid_closes[-10:]) / 10
    ma20 = sum(valid_closes[-20:]) / 20
    previous_ma20 = sum(valid_closes[-25:-5]) / 20
    ma20_slope = (ma20 / previous_ma20 - 1.0) if previous_ma20 else 0.0
    yesterday_high = valid_highs[-1]
    resistance_candidates = [ma10, ma20, max(valid_highs[-20:]), yesterday_high]
    above = [level for level in resistance_candidates if level >= current * 0.995]
    resistance = min(above) if above else min(resistance_candidates, key=lambda level: abs(level - current))
    resistance_distance = abs(current - resistance) / current
    vwap_deviation = (current / vwap) - 1.0

    last_three_highs = [_float(_value(row, "high")) for row in minute[-3:]]
    previous_close = _float(_value(minute[-2], "close"), current) or current
    reversal = bool(
        all(value is not None for value in last_three_highs)
        and last_three_highs[0] >= last_three_highs[1] >= last_three_highs[2]
        and current < previous_close
    )
    trend_ok = current <= ma20 * 1.03 and ma20_slope <= 0.02
    near_resistance = resistance_distance <= max(0.004, 0.15 * atr / current)
    above_vwap = vwap_deviation >= max(min_vwap_deviation_pct, 0.2 * atr / current)

    atr_ratio = atr / current
    eff_profit = max(min_profit_pct, profit_atr_mult * atr_ratio)
    eff_stop_cap = max(max_stop_pct, stop_atr_mult * atr_ratio)
    stop = max(resistance + 0.1 * atr, current + 0.2 * atr)
    stop_risk = max((stop - current) / current, 0.0)
    target = min(vwap, current * (1.0 - eff_profit))
    reward_risk = (current - target) / max(stop - current, 1e-9)

    checks = {
        "trend": trend_ok,
        "support": near_resistance,
        "vwap": above_vwap,
        "reversal": reversal,
        "coverage": len(minute) >= 20,
        "reward": reward_risk >= 1.0,
    }
    evidence: list[str] = []
    score = 0
    if checks["trend"]:
        score += 20
        evidence.append("日线未单边强势上涨,适合高抛")
    if checks["support"]:
        score += 20
        evidence.append(f"当前价接近压力位 {resistance:.3f}")
    if checks["vwap"]:
        score += 15
        evidence.append(f"当前价高于 VWAP {vwap:.3f}")
    if checks["reversal"]:
        score += 20
        evidence.append("最近三根分钟K高点走低并出现滞涨")
    if checks["coverage"]:
        score += 10
        evidence.append("分钟数据覆盖满足盘中判断")
    if checks["reward"]:
        score += 15
        evidence.append(f"预期盈亏比 {reward_risk:.2f}")

    hard_blocks: list[str] = []
    if not trend_ok:
        hard_blocks.append("处于单边强势上涨,不宜高抛")
    if stop_risk > eff_stop_cap:
        hard_blocks.append(f"止损距离 {stop_risk:.2%} 超过上限 {eff_stop_cap:.2%}")
    if current >= resistance + 0.2 * atr:
        hard_blocks.append("已有效突破关键压力")
    limit_block = _limit_position_block(
        direction="short", current=current, prev_close=valid_closes[-1], ma20=ma20,
        closes=valid_closes, daily=daily, minute=minute, limit_ratio=limit_ratio or 0.0,
    )
    if limit_block:
        hard_blocks.append(limit_block)
    action = "sell_open" if score >= min_score and not hard_blocks else "observe"
    reason = "；".join(evidence) if action == "sell_open" else "；".join(hard_blocks or evidence or ["条件未满足"])
    return TSignalResult(
        valid=not hard_blocks,
        action=action,
        score=min(score, 100),
        reason=reason,
        evidence=evidence,
        hard_blocks=hard_blocks,
        current_price=round(current, 4),
        vwap=round(vwap, 4),
        support_price=round(resistance, 4),
        stop_loss_price=round(stop, 4),
        target_price=round(target, 4),
        data_quality=quality,
        metrics={
            "ma10": round(ma10, 4),
            "ma20": round(ma20, 4),
            "ma20_slope": round(ma20_slope, 6),
            "atr14": round(atr, 4),
            "vwap_deviation": round(vwap_deviation, 6),
            "resistance_distance": round(resistance_distance, 6),
            "stop_risk": round(stop_risk, 6),
            "reward_risk": round(reward_risk, 4),
            "eff_profit_pct": round(eff_profit, 6),
            "eff_stop_cap_pct": round(eff_stop_cap, 6),
            "reversal": reversal,
        },
        score_detail={key: bool(value) for key, value in checks.items()},
    )


def evaluate_t_exit_short(
    current_price: float,
    *,
    vwap: float,
    target_price: float,
    stop_loss_price: float,
) -> str:
    """倒T(先卖后买)离场:返回 buy_back / invalidated / observe。

    target_price 已在入场时取 min(vwap, 入场价×(1-eff_profit)),既保证至少回落到
    VWAP 才买回,又让 ATR 自适应目标真正驱动离场——故此处直接用 target_price,
    不再套 max(vwap, target)(那会把放宽的目标吞回 VWAP)。vwap 仅保留兼容签名。
    """
    if current_price >= stop_loss_price:
        return "invalidated"
    if current_price <= target_price:
        return "buy_back"
    return "observe"
