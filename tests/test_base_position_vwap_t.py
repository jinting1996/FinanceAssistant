from datetime import datetime, timedelta

from src.collectors.kline_collector import KlineData
from src.core.signals.base_position_vwap_t import (
    compute_base_position_vwap_t,
    compute_intraday_vwap,
    evaluate_t_exit,
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
