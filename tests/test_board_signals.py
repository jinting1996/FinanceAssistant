from __future__ import annotations

from datetime import date, timedelta

from src.core.board_signals import build_board_signal


def _bars(values: list[float]) -> list[dict]:
    start = date(2026, 1, 1)
    return [
        {
            "date": (start + timedelta(days=idx)).strftime("%Y-%m-%d"),
            "open": value - 0.2,
            "high": value + 0.5,
            "low": value - 0.5,
            "close": value,
            "volume": 1000 + idx,
            "turnover": 1000000 + idx,
        }
        for idx, value in enumerate(values)
    ]


def test_board_signal_uptrend_is_bullish():
    signal = build_board_signal(_bars([float(x) for x in range(10, 80)]))

    assert signal["available"] is True
    assert signal["trend_score"] >= 58
    assert signal["rsi_state"] in {"strong", "overbought"}
    assert signal["macd_state"] in {"bullish_above_zero", "bullish_repair", "golden_cross"}


def test_board_signal_downtrend_is_cooling_or_weak():
    signal = build_board_signal(_bars([float(x) for x in range(90, 20, -1)]))

    assert signal["available"] is True
    assert signal["change_5d_pct"] < 0
    assert signal["rsi_state"] in {"weak", "oversold"}
    assert signal["rotation_state"] in {"cooling", "repair_watch", "neutral"}


def test_board_signal_sideways_is_neutralish():
    values = [50 + ((idx % 5) - 2) * 0.2 for idx in range(70)]
    signal = build_board_signal(_bars(values))

    assert signal["available"] is True
    assert abs(signal["change_5d_pct"]) < 2
    assert signal["rsi_state"] in {"neutral", "weak", "strong"}


def test_board_signal_insufficient_data():
    signal = build_board_signal(_bars([10, 10.2, 10.1]))

    assert signal["available"] is False
    assert signal["macd_state"] == "insufficient"
    assert signal["rsi_state"] == "insufficient"
    assert len(signal["series"]) == 3
