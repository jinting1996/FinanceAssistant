from types import SimpleNamespace

import pytest

from src.core.screener.formula import FormulaError, evaluate_formula, parse_formula


def _bars(values):
    rows = []
    for i, close in enumerate(values, start=1):
        rows.append(
            SimpleNamespace(
                date=f"2026-01-{i:02d}",
                open=float(close) - 0.1,
                high=float(close) + 0.2,
                low=float(close) - 0.2,
                close=float(close),
                volume=1000 + i * 10,
            )
        )
    return rows


def test_tdx_assignment_and_output_label():
    rows = _bars(range(1, 80))
    assert evaluate_formula("X:=MA(C,5); XG: X > MA(C,20);", rows)["matched"] is True


def test_supported_indicators_on_uptrend():
    rows = _bars(range(1, 80))
    result = evaluate_formula("C > MA(C,20) AND RSI(C,6) > 70 AND MACD(C,12,26,9) > 0", rows)
    assert result["matched"] is True
    assert result["indicators"]["ma20"] is not None
    assert result["indicators"]["rsi6"] is not None


def test_cross_ref_hhv_count_every():
    rows = _bars([10] * 30 + [10.5, 11, 12, 13, 14])
    formula = "C > REF(HHV(H,20),1) AND COUNT(C > MA(C,5), 3) >= 1 AND EVERY(C > O, 2)"
    assert evaluate_formula(formula, rows)["matched"] is True


def test_unknown_function_is_blocked():
    with pytest.raises(FormulaError):
        parse_formula("__import__(1)")

