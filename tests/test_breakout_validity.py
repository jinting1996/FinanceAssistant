"""突破有效性状态机测试:合成K线覆盖各状态与路径。"""

from src.core.signals.breakout_validity import BreakoutParams, compute_breakout_validity


def _bar(date: str, o: float, h: float, low: float, c: float, v: float = 1000.0) -> dict:
    return {"date": date, "open": o, "high": h, "low": low, "close": c, "volume": v}


def _date(i: int) -> str:
    # 生成递增的伪交易日(仅用于标识,算法不解析日期间隔)
    return f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}"


def _uptrend_base(n: int = 70, start: float = 10.0, step: float = 0.02) -> list[dict]:
    """缓慢爬升的底仓K线:满足均线多头,末端留出前高。"""
    rows = []
    price = start
    for i in range(n):
        o = price
        c = price + step
        rows.append(_bar(_date(i), o, c + 0.02, o - 0.03, c, 1000.0))
        price = c
    return rows


def _with_peak_and_pullback(rows: list[dict], peak: float, pull_days: int = 6) -> list[dict]:
    """在底仓尾部造一个前高(peak),随后横盘微升整理,使前高年龄≥5且均线保持多头。"""
    i = len(rows)
    rows = rows + [_bar(_date(i), peak - 0.1, peak, peak - 0.2, peak - 0.05, 1500.0)]
    base = peak - 0.35
    for j in range(pull_days):
        i += 1
        c = base + 0.015 * j  # 微升横盘,收盘始终低于 peak×1.005
        rows.append(_bar(_date(i), c - 0.02, c + 0.03, c - 0.05, c, 800.0))
    return rows


def _breakout_day(rows: list[dict], g0: float, *, vol: float, close_mult: float = 1.02) -> list[dict]:
    i = len(rows)
    c = g0 * close_mult
    return rows + [_bar(_date(i), rows[-1]["close"], c + 0.02, rows[-1]["close"] - 0.02, c, vol)]


def _make_event(vol: float = 2000.0) -> tuple[list[dict], float]:
    """构造标准事件:上升趋势+前高12.0+突破日。返回(rows, g0)。"""
    rows = _uptrend_base(70, start=10.0)
    peak = rows[-1]["close"] + 0.6  # 明显高于其后的整理区
    rows = _with_peak_and_pullback(rows, peak=peak)
    rows = _breakout_day(rows, peak, vol=vol)
    return rows, peak


def _hold_days(rows: list[dict], g0: float, days: int, *, vol: float = 900.0, mult: float = 1.025) -> list[dict]:
    for _ in range(days):
        i = len(rows)
        c = g0 * mult
        rows = rows + [_bar(_date(i), c - 0.02, c + 0.03, c - 0.05, c, vol)]
        mult += 0.002
    return rows


def test_observed_on_d0():
    """突破日当天(无后续K线)状态为 observed"""
    rows, g0 = _make_event(vol=2000.0)
    r = compute_breakout_validity(rows)
    assert r.state == "observed"
    assert abs(r.g0 - round(g0, 4)) < 0.01
    assert r.event_age == 0


def test_pending_before_confirm_window():
    """突破后1~2天未过最小确认时间,状态为 pending"""
    rows, g0 = _make_event(vol=2000.0)
    rows = _hold_days(rows, g0, 2)
    r = compute_breakout_validity(rows)
    assert r.state == "pending"
    assert "最小确认时间" in r.reason


def test_valid_active_path_a():
    """放量突破(路径A)+站稳3日 → valid_active,止损=max(G_SUPPORT,L0)"""
    rows, g0 = _make_event(vol=2000.0)  # V0=2000 ≥ V_BASE×1.3
    rows = _hold_days(rows, g0, 3)
    r = compute_breakout_validity(rows)
    assert r.state == "valid_active"
    assert r.paths["A"] is True
    assert r.stop_loss == round(max(r.g_support, r.l0), 4)
    assert r.target_price == round(r.g0 * 1.15, 4)


def test_valid_active_path_b_late_volume():
    """D0缩量突破,后置正向放量日确认(路径B)→ valid_active"""
    rows, g0 = _make_event(vol=900.0)  # D0 未放量
    rows = _hold_days(rows, g0, 2, vol=900.0)
    # 第3天正向放量推进:放量+上涨+收盘强
    i = len(rows)
    c = g0 * 1.045
    rows = rows + [_bar(_date(i), g0 * 1.03, c + 0.02, g0 * 1.025, c, 2200.0)]
    r = compute_breakout_validity(rows)
    assert r.state == "valid_active"
    assert r.paths["B"] is True


def test_failed_close_below_support():
    """收盘跌破 G_SUPPORT(3a)→ failed 且永久失效"""
    rows, g0 = _make_event(vol=2000.0)
    i = len(rows)
    c = g0 * 0.96  # < 0.97×G0
    rows = rows + [_bar(_date(i), g0, g0 * 1.01, c - 0.05, c, 1000.0)]
    r = compute_breakout_validity(rows)
    assert r.state == "failed"
    assert "3a" in r.fail_reason


def test_failed_big_volume_breakdown():
    """放量收回 G0 下方(3d 放量破位)→ failed,即使跌幅未及支撑区"""
    rows, g0 = _make_event(vol=2000.0)
    i = len(rows)
    c = g0 * 0.985  # 在 [G_SUPPORT, G0) 区间,但放量
    rows = rows + [_bar(_date(i), g0 * 1.01, g0 * 1.015, c - 0.02, c, 2500.0)]
    r = compute_breakout_validity(rows)
    assert r.state == "failed"
    assert "3d" in r.fail_reason


def test_failed_three_consecutive_below():
    """连续3日收盘<G0(3c)→ failed(缩量阴跌也算持续拒绝)"""
    rows, g0 = _make_event(vol=2000.0)
    for k in range(3):
        i = len(rows)
        c = g0 * (0.995 - 0.002 * k)  # 略低于G0但高于支撑区
        rows = rows + [_bar(_date(i), c + 0.01, c + 0.02, c - 0.02, c, 700.0)]
    r = compute_breakout_validity(rows)
    assert r.state == "failed"
    assert "3c" in r.fail_reason


def test_invalidated_after_valid():
    """先达成Valid再破位 → invalidated(不改写曾经有效)"""
    rows, g0 = _make_event(vol=2000.0)
    rows = _hold_days(rows, g0, 4)  # 已达成 valid(路径A)
    i = len(rows)
    c = g0 * 0.96
    rows = rows + [_bar(_date(i), g0, g0 * 1.01, c - 0.05, c, 1000.0)]
    r = compute_breakout_validity(rows)
    assert r.state == "invalidated"
    assert r.ever_valid is True


def test_extended_when_overextended():
    """C/G0>1.15 → extended,不作为买入候选"""
    rows, g0 = _make_event(vol=2000.0)
    rows = _hold_days(rows, g0, 3, mult=1.16)  # 收盘越过 1.15×G0
    r = compute_breakout_validity(rows)
    assert r.state == "extended"
    assert r.extension > 1.15


def test_not_in_pool_without_breakout():
    """无突破事件 → not_in_pool"""
    rows = _uptrend_base(70)
    r = compute_breakout_validity(rows)
    assert r.state == "not_in_pool"


def test_anchor_replay_keeps_event():
    """锚点冻结:传入 anchor_d0_date 后即使事件超出观察窗口也能复评"""
    rows, g0 = _make_event(vol=2000.0)
    d0_date = rows[-1]["date"]
    rows = _hold_days(rows, g0, 8)  # 年龄8 > 观察期5
    r = compute_breakout_validity(rows, anchor_d0_date=d0_date)
    assert r.d0_date == d0_date
    assert r.state in ("valid_active", "extended")


def test_insufficient_data():
    """数据不足10根 → insufficient"""
    r = compute_breakout_validity([_bar(_date(i), 10, 10.1, 9.9, 10, 100) for i in range(5)])
    assert r.state == "insufficient"
