"""上升趋势突破有效性策略(文档 v3.0,60根口径)——纯日线 OHLCV 状态机计算。

第0层:定位突破事件(60日前高、前高年龄≥5、收盘突破+0.5%缓冲、上升趋势背景);
第1层:锚点冻结后逐日回放——失效检查(3a~3e)优先、价格接受、四条确认路径(A/B/C/D)、
       八态归结(not_in_pool/observed/pending/valid_active/extended/failed/expired/invalidated)。
交易映射(模拟盘):valid_active=买入;止损=max(G_SUPPORT,L0);止盈=G0×1.15;
failed/invalidated=强制平仓(由扫描器写 sell 信号)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

EPS = 0.0001


def _value(row: Any, key: str, default: Any = None) -> Any:
    return row.get(key, default) if isinstance(row, dict) else getattr(row, key, default)


def _float(value: Any, default: float | None = None) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if result == result else default


@dataclass
class BreakoutParams:
    lookback: int = 60          # N 前高回看窗口
    min_high_age: int = 5       # M 前高最小年龄
    max_observe: int = 5        # K 最大观察期(D0+K)
    breakout_buffer: float = 1.005   # BR 突破缓冲
    max_deviation: float = 1.25      # DEV C/MA20 上限
    confirm_days: int = 3       # CW 最小确认时间
    support_ratio: float = 0.97      # FB G_SUPPORT=G0×FB
    vol_up_ratio: float = 1.3        # RUP 放量阈值
    vol_down_ratio: float = 0.8      # 缩量阈值
    vbase_window: int = 20      # V_BASE 窗口
    ext_g0: float = 1.15        # 过度延伸 C/G0
    ext_ma20: float = 1.25      # 过度延伸 C/MA20
    accept_ratio: float = 0.60  # 站上 G0 天数占比阈值


@dataclass
class BreakoutValidityResult:
    state: str                  # not_in_pool/observed/pending/valid_active/extended/failed/expired/invalidated/insufficient
    reason: str = ""
    d0_date: str = ""
    d0_index: int = -1
    g0: float | None = None
    g_support: float | None = None
    l0: float | None = None
    c0: float | None = None
    v0: float | None = None
    v_base: float | None = None
    event_age: int = 0
    close: float | None = None
    extension: float | None = None       # C/G0
    price_acceptance: bool = False
    paths: dict[str, bool] = field(default_factory=dict)
    ever_valid: bool = False
    fail_reason: str = ""
    fail_date: str = ""
    stop_loss: float | None = None       # max(G_SUPPORT, L0)
    target_price: float | None = None    # G0 × ext_g0
    evidence: list[str] = field(default_factory=list)
    limited: list[str] = field(default_factory=list)   # 数据不足未检验的条件

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict

        return asdict(self)


def _ma(closes: list[float], end: int, n: int) -> float | None:
    """closes[end] 为最后一根,计算截至 end(含)的 n 日均线。"""
    if end + 1 < n:
        return None
    window = closes[end - n + 1 : end + 1]
    return sum(window) / n


def _cpos(o: float, h: float, low: float, c: float) -> float:
    return (c - low) / (h - low + EPS)


def _upsh(o: float, h: float, low: float, c: float) -> float:
    return (h - max(o, c)) / (h - low + EPS)


def _is_limit_down(prev_close: float, close: float) -> bool:
    """主板跌停:C ≤ ROUND(前收×0.90,2)+0.01(容差口径,双创/ST已被选股范围排除)。"""
    return close <= round(prev_close * 0.90, 2) + 0.01


def _trend_ok(closes: list[float], t: int, params: BreakoutParams, limited: list[str]) -> bool:
    ma5 = _ma(closes, t, 5)
    ma10 = _ma(closes, t, 10)
    ma20 = _ma(closes, t, 20)
    ma60 = _ma(closes, t, 60)
    ma60_ref = _ma(closes, t - 10, 60) if t >= 10 else None
    c = closes[t]
    if ma5 is None or ma10 is None or ma20 is None:
        return False
    base = ma5 >= ma10 >= ma20 and c > ma20 and c / ma20 < params.max_deviation
    if ma60 is None or ma60_ref is None:
        # §2.1.1 降级:MA60 数据不足时退化口径,并标注未检验
        if "MA60趋势背景未检验" not in limited:
            limited.append("MA60趋势背景未检验")
        return base
    return base and ma20 >= ma60 and ma60 >= ma60_ref


def _find_breakout(
    rows: list[Any],
    highs: list[float],
    closes: list[float],
    params: BreakoutParams,
    limited: list[str],
    anchor_index: int | None = None,
) -> tuple[int, float] | None:
    """定位突破日 D0,返回 (index, G0)。anchor_index 指定时只验证该日(锚点冻结场景)。"""
    last = len(rows) - 1
    if anchor_index is not None:
        candidates = [anchor_index]
    else:
        # 只在观察窗口内找新事件:D0 距今 ≤ max_observe
        candidates = list(range(last, max(last - params.max_observe, 0) - 1, -1))
    for t in candidates:
        if t < 2:
            continue
        window = min(params.lookback, t)
        if window < 2:
            continue
        seg = highs[t - window : t]
        g = max(seg)
        # 前高年龄:取最近一次触及最高价的日子,距 t ≥ min_high_age
        high_idx_in_seg = max(i for i, h in enumerate(seg) if h == g)
        high_age = t - (t - window + high_idx_in_seg)
        if high_age < params.min_high_age:
            continue
        if not (closes[t] >= g * params.breakout_buffer and closes[t - 1] < g * params.breakout_buffer):
            continue
        if not _trend_ok(closes, t, params, limited):
            continue
        return t, g
    return None


def compute_breakout_validity(
    daily_klines: list[Any],
    *,
    params: BreakoutParams | None = None,
    anchor_d0_date: str | None = None,
) -> BreakoutValidityResult:
    """计算突破有效性状态。

    anchor_d0_date:持仓监控场景传入原事件的 D0 日期,锚点冻结在该事件上重放;
    不传则在观察窗口内定位最近一次合格突破(选股场景)。
    """
    p = params or BreakoutParams()
    rows = [r for r in (daily_klines or [])]
    if len(rows) < 10:
        return BreakoutValidityResult(state="insufficient", reason="K线数据不足10根")

    dates = [str(_value(r, "date") or "")[:10] for r in rows]
    opens = [_float(_value(r, "open")) for r in rows]
    highs = [_float(_value(r, "high")) for r in rows]
    lows = [_float(_value(r, "low")) for r in rows]
    closes = [_float(_value(r, "close")) for r in rows]
    volumes = [_float(_value(r, "volume")) for r in rows]
    if any(v is None for v in closes) or any(v is None for v in highs) or any(v is None for v in lows):
        return BreakoutValidityResult(state="insufficient", reason="K线关键字段不完整")

    limited: list[str] = []
    anchor_index: int | None = None
    if anchor_d0_date:
        target = anchor_d0_date[:10]
        for i, d in enumerate(dates):
            if d == target:
                anchor_index = i
                break
        if anchor_index is None:
            return BreakoutValidityResult(state="insufficient", reason=f"锚点日 {target} 不在数据区间内")

    found = _find_breakout(rows, highs, closes, p, limited, anchor_index=anchor_index)
    if found is None:
        return BreakoutValidityResult(
            state="not_in_pool",
            reason="观察窗口内无合格突破事件" if anchor_index is None else "锚点日不构成合格突破",
            limited=limited,
        )
    d0, g0 = found

    # ---- 锚点冻结 ----
    g_support = g0 * p.support_ratio
    l0 = float(lows[d0])
    c0 = float(closes[d0])
    o0 = float(opens[d0]) if opens[d0] is not None else c0
    h0 = float(highs[d0])
    v0 = volumes[d0]
    vb_window = min(p.vbase_window, d0)
    v_seg = [v for v in volumes[d0 - vb_window : d0] if v is not None and v > 0]
    v_base = (sum(v_seg) / len(v_seg)) if len(v_seg) >= 5 else None
    if v_base is None:
        limited.append("量能基准不足,量能类条件不可判")

    last = len(rows) - 1
    age = last - d0
    close = float(closes[last])
    extension = close / g0 if g0 > 0 else None
    stop_loss = round(max(g_support, l0), 4)
    target_price = round(g0 * p.ext_g0, 4)
    evidence: list[str] = [f"D0={dates[d0]} G0={g0:.2f} C0={c0:.2f} 事件年龄={age}天"]

    def _result(state: str, reason: str, **kw: Any) -> BreakoutValidityResult:
        return BreakoutValidityResult(
            state=state,
            reason=reason,
            d0_date=dates[d0],
            d0_index=d0,
            g0=round(g0, 4),
            g_support=round(g_support, 4),
            l0=l0,
            c0=c0,
            v0=v0,
            v_base=round(v_base, 2) if v_base else None,
            event_age=age,
            close=close,
            extension=round(extension, 4) if extension else None,
            stop_loss=stop_loss,
            target_price=target_price,
            evidence=evidence,
            limited=limited,
            **kw,
        )

    # D0 是否正向放量日(路径A前提)
    d0_positive = bool(
        v_base is not None
        and v0 is not None
        and v0 >= v_base * p.vol_up_ratio
        and _cpos(o0, h0, l0, c0) >= 0.6
        and _upsh(o0, h0, l0, c0) < 0.4
    )

    # ---- 逐日回放 D1 → last ----
    above_count = 0          # 收盘≥G0 天数(D1起)
    consecutive_below = 0
    min_close = c0
    pos_bigvol_seen = False  # 路径B:正向放量日
    neg_bigvol_seen = False  # 路径D否决:负面放量日
    pullback_seen = False    # 路径C:缩量回踩支撑区
    all_above = True         # 路径D:每日收盘≥G0
    decline_vol_ok = True    # 路径D:回落日量能递减或≤V_BASE
    last_decline_vol: float | None = None
    ever_valid = False
    fail_reason = ""
    fail_date = ""

    for d in range(d0 + 1, last + 1):
        c = float(closes[d])
        h = float(highs[d])
        low = float(lows[d])
        o = float(opens[d]) if opens[d] is not None else c
        v = volumes[d]
        prev_c = float(closes[d - 1])
        min_close = min(min_close, c)
        if c >= g0:
            above_count += 1
            consecutive_below = 0
        else:
            consecutive_below += 1
        if c < g0:
            all_above = False

        big_vol = v is not None and v_base is not None and v >= v_base * p.vol_up_ratio
        small_vol = v is not None and v_base is not None and v <= v_base * p.vol_down_ratio
        upsh = _upsh(o, h, low, c)
        cpos = _cpos(o, h, low, c)

        if big_vol and c > prev_c and c >= g0 and cpos >= 0.6 and upsh < 0.4:
            pos_bigvol_seen = True
        if big_vol and (c < g0 or upsh >= 0.4 or c < prev_c):
            neg_bigvol_seen = True
        if low <= g0 * 1.02 and c >= g_support and small_vol:
            pullback_seen = True
        if c < prev_c and v is not None and v_base is not None:
            if not (v <= v_base or (last_decline_vol is not None and v <= last_decline_vol)):
                decline_vol_ok = False
            last_decline_vol = v

        # 失效检查(优先):3a~3e
        if not fail_reason:
            if c < g_support:
                fail_reason, fail_date = f"3a 收盘{c:.2f}<G_SUPPORT{g_support:.2f}", dates[d]
            elif c < l0:
                fail_reason, fail_date = f"3b 收盘{c:.2f}<L0 {l0:.2f}", dates[d]
            elif consecutive_below >= 3:
                fail_reason, fail_date = "3c 连续3日收盘<G0", dates[d]
            elif c < g0 and big_vol:
                fail_reason, fail_date = "3d 放量破位", dates[d]
            elif c < g0 and _is_limit_down(prev_c, c):
                fail_reason, fail_date = "3e 跌停破位", dates[d]
        if fail_reason:
            break

        # 达成 Valid 检查(用于 Invalidated 区分):价格接受 + 任一路径 + 年龄≥CW
        day_age = d - d0
        denom = max(day_age, 1)
        acceptance_d = c >= g0 and (above_count / denom) >= p.accept_ratio and min_close >= g_support
        if acceptance_d and day_age >= p.confirm_days:
            path_a = d0_positive
            path_b = (not d0_positive) and pos_bigvol_seen
            path_c = pullback_seen and c >= g0
            path_d = all_above and not neg_bigvol_seen and decline_vol_ok
            if path_a or path_b or path_c or path_d:
                ever_valid = True

    if fail_reason:
        evidence.append(f"失效: {fail_reason} @{fail_date}")
        state = "invalidated" if ever_valid else "failed"
        return _result(state, fail_reason, ever_valid=ever_valid, fail_reason=fail_reason, fail_date=fail_date)

    # ---- 当前状态归结 ----
    denom = max(age, 1)
    acceptance = close >= g0 and (above_count / denom) >= p.accept_ratio and min_close >= g_support
    paths = {
        "A": d0_positive and acceptance,
        "B": (not d0_positive) and pos_bigvol_seen and acceptance,
        "C": pullback_seen and close >= g0 and acceptance,
        "D": all_above and not neg_bigvol_seen and decline_vol_ok and age >= p.confirm_days and acceptance,
    }
    if v_base is None:
        paths = {k: False for k in paths}

    if age == 0:
        return _result("observed", "突破日当天,无后续数据", paths=paths, price_acceptance=acceptance)
    if age < p.confirm_days:
        return _result("pending", f"未过最小确认时间(年龄{age}<{p.confirm_days})", paths=paths, price_acceptance=acceptance)

    hit = [k for k, v in paths.items() if v]
    if acceptance and hit:
        evidence.append(f"命中路径: {'/'.join(hit)};站上占比 {above_count}/{denom}={above_count / denom:.0%}")
        ma20 = _ma([float(x) for x in closes], last, 20)
        if (extension is not None and extension > p.ext_g0) or (ma20 and close / ma20 >= p.ext_ma20):
            return _result(
                "extended",
                f"突破有效但过度延伸 C/G0={extension:.3f}",
                paths=paths, price_acceptance=True, ever_valid=True,
            )
        return _result("valid_active", f"突破有效(路径{'/'.join(hit)})", paths=paths, price_acceptance=True, ever_valid=True)

    if ever_valid:
        # 曾经有效但当前接受度退坡(未触失效):保守归 pending
        return _result("pending", "曾达成Valid,当前价格接受退坡", paths=paths, price_acceptance=acceptance, ever_valid=True)
    if age > p.max_observe:
        return _result("expired", f"超过最大观察期({age}>{p.max_observe})且从未确认", paths=paths, price_acceptance=acceptance)
    missing = "价格接受不成立" if not acceptance else "四条路径均未命中"
    return _result("pending", f"等待确认:{missing}", paths=paths, price_acceptance=acceptance)
