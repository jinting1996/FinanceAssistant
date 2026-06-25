from datetime import datetime, timedelta

from src.collectors.kline_collector import KlineData
from src.core.signals.base_position_vwap_t import (
    compute_base_position_vwap_t,
    compute_base_position_vwap_t_short,
    compute_intraday_vwap,
    evaluate_t_exit,
    evaluate_t_exit_short,
)


def _daily_rows() -> list[dict]:
    rows = []
    for index in range(40):
        close = 9.50 + index * 0.012
        rows.append(
            {
                "date": f"2026-05-{index + 1:02d}",
                "open": close - 0.02,
                "high": close + 0.10,
                "low": close - 0.08,
                "close": close,
                "volume": 100_000,
            }
        )
    rows[-1]["low"] = 9.78
    return rows


def _minute_rows(with_amount: bool = False) -> list[KlineData]:
    start = datetime(2026, 6, 22, 9, 30)
    rows = []
    for index in range(30):
        close = 10.02 - index * 0.009
        if index >= 27:
            close = [9.775, 9.778, 9.785][index - 27]
        volume = 1_000.0
        rows.append(
            KlineData(
                date=(start + timedelta(minutes=index)).strftime("%Y-%m-%d %H:%M"),
                open=close + 0.003,
                high=close + 0.012,
                low=close - 0.008 + (index - 27) * 0.004 if index >= 27 else close - 0.008,
                close=close,
                volume=volume,
                amount=close * volume if with_amount else None,
                source="tencent",
            )
        )
    return rows


def test_compute_intraday_vwap_uses_amount_and_estimated_fallback():
    value, quality = compute_intraday_vwap(_minute_rows(with_amount=True))
    assert value is not None
    assert quality == "amount"

    estimated, estimated_quality = compute_intraday_vwap(_minute_rows(with_amount=False))
    assert estimated is not None
    assert estimated_quality == "estimated"


def test_only_latest_trading_day_minutes_used():
    """混入昨日分钟K不应污染 VWAP/信号(应只看最新交易日)。"""
    from src.core.signals.base_position_vwap_t import _latest_day_minutes

    yesterday = [
        KlineData(
            date=f"2026-06-21 14:{minute:02d}",
            open=20.0, high=20.1, low=19.9, close=20.0, volume=1000.0, amount=None, source="t",
        )
        for minute in range(40, 60)
    ]
    today = _minute_rows()
    mixed = yesterday + today

    assert len(_latest_day_minutes(mixed)) == len(today)
    res_mixed = compute_base_position_vwap_t(_daily_rows(), mixed)
    res_today = compute_base_position_vwap_t(_daily_rows(), today)
    assert res_mixed.vwap == res_today.vwap
    assert res_mixed.score == res_today.score


def test_compute_base_position_vwap_t_produces_executable_levels():
    result = compute_base_position_vwap_t(_daily_rows(), _minute_rows())

    assert result.action == "buy_t"
    assert result.score >= 70
    assert result.stop_loss_price < result.current_price < result.target_price
    assert result.vwap > result.current_price
    assert result.data_quality == "estimated"


def test_compute_base_position_vwap_t_blocks_broken_trend():
    daily = _daily_rows()
    for index, row in enumerate(daily[-20:]):
        row["close"] = 11.0 - index * 0.1
        row["low"] = row["close"] - 0.1
        row["high"] = row["close"] + 0.1

    result = compute_base_position_vwap_t(daily, _minute_rows())

    assert result.action == "observe"
    assert result.hard_blocks


def test_evaluate_t_exit_state():
    params = {"vwap": 10.0, "target_price": 10.1, "stop_loss_price": 9.5}
    assert evaluate_t_exit(10.0, **params) == "sell_t"
    assert evaluate_t_exit(9.4, **params) == "invalidated"
    assert evaluate_t_exit(9.8, **params) == "observe"


def _flat_daily_rows() -> list[dict]:
    """横盘震荡日K(MA20 斜率约 0),用于倒T(高抛)信号。"""
    rows = []
    for index in range(40):
        close = 10.04 if index % 2 else 9.96
        rows.append(
            {
                "date": f"2026-05-{index + 1:02d}",
                "open": close - 0.02,
                "high": close + 0.10,
                "low": close - 0.10,
                "close": close,
                "volume": 100_000,
            }
        )
    return rows


def _minute_rows_short() -> list[KlineData]:
    """分钟价格冲高到压力位、高于 VWAP,末端高点走低滞涨。"""
    start = datetime(2026, 6, 22, 9, 30)
    rows = []
    for index in range(30):
        close = 9.92 + index * 0.0038
        if index >= 27:
            close = [10.035, 10.032, 10.028][index - 27]
        volume = 1_000.0
        rows.append(
            KlineData(
                date=(start + timedelta(minutes=index)).strftime("%Y-%m-%d %H:%M"),
                open=close - 0.003,
                high=close + 0.005,
                low=close - 0.010,
                close=close,
                volume=volume,
                amount=None,
                source="tencent",
            )
        )
    return rows


def test_compute_base_position_vwap_t_short_produces_executable_levels():
    result = compute_base_position_vwap_t_short(_flat_daily_rows(), _minute_rows_short())

    assert result.action == "sell_open"
    assert result.score >= 70
    # 倒T:止损在上方、目标(买回)在下方,当前价高于 VWAP
    assert result.target_price < result.current_price < result.stop_loss_price
    assert result.vwap < result.current_price


def test_atr_adaptive_widens_targets_on_high_amplitude():
    """高振幅日K下,止盈/止损上限按 ATR 自动放大,超过固定地板。"""
    rows = []
    for index in range(40):
        close = 10.0
        rows.append(
            {
                "date": f"2026-05-{index + 1:02d}",
                "open": close,
                "high": close * 1.04,
                "low": close * 0.96,
                "close": close,
                "volume": 100_000,
            }
        )
    result = compute_base_position_vwap_t(rows, _minute_rows())
    assert result.metrics["eff_profit_pct"] > 0.008  # 超过 0.8% 地板
    assert result.metrics["eff_stop_cap_pct"] > 0.015  # 超过 1.5% 地板


def test_evaluate_t_exit_short_state():
    params = {"vwap": 10.0, "target_price": 9.9, "stop_loss_price": 10.5}
    assert evaluate_t_exit_short(9.9, **params) == "buy_back"
    assert evaluate_t_exit_short(10.6, **params) == "invalidated"
    assert evaluate_t_exit_short(10.2, **params) == "observe"
