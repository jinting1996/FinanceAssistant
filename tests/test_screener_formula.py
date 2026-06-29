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


def test_sma_matches_tdx_recursion():
    """通达信 SMA(X,N,M):Y=(X*M+Y_前*(N-M))/N 递归加权"""
    rows = _bars([10, 12, 14, 16])
    # 手算 SMA(C,3,1): 10 → (12+10*2)/3=10.667 → (14+10.667*2)/3=11.778 → (16+11.778*2)/3=13.185
    res = evaluate_formula("SMA(C,3,1) > 13 AND SMA(C,3,1) < 13.5", rows)
    assert res["matched"] is True


def test_sum_std_avedev_run():
    """SUM/STD/AVEDEV 可参与表达式且产生有限值"""
    rows = _bars(range(1, 40))
    assert evaluate_formula("SUM(C,5) > 0 AND STD(C,20) > 0 AND AVEDEV(C,14) > 0", rows)["matched"] is True


def test_exist_and_barslast():
    """EXIST(N日内曾成立) 与 BARSLAST(距上次成立周期数)"""
    rows = _bars([10] * 30 + [9, 9, 12])  # 最后一根放量上涨
    assert evaluate_formula("EXIST(C > REF(C,1), 3)", rows)["matched"] is True
    # 最近一次 C>O 是最后一根(每根 open=close-0.1,恒成立),BARSLAST=0
    assert evaluate_formula("BARSLAST(C > O) = 0", rows)["matched"] is True


def test_kdj_golden_cross_and_boll_compose():
    """积木式组合:KDJ 金叉 / 布林上轨,均可正确解析与求值"""
    rows = _bars([10] * 20 + [10.5, 11, 12, 13, 14, 15])
    kdj = "RSV := (C-LLV(L,9))/(HHV(H,9)-LLV(L,9))*100; CROSS(SMA(RSV,3,1), SMA(SMA(RSV,3,1),3,1))"
    # 仅验证可解析+求值(不强求命中)
    assert isinstance(evaluate_formula(kdj, rows)["matched"], bool)
    boll = "C > MA(C,20) + 2 * STD(C,20)"
    assert evaluate_formula(boll, rows)["matched"] is True


def test_hhvbars_distance_to_recent_high():
    """HHVBARS(H,N):最高值到当前的周期数;最高点在最后一根则为 0"""
    rows = _bars([10, 11, 15, 12, 13])  # 最高 15 在倒数第 3 根
    assert evaluate_formula("HHVBARS(H,5) = 2", rows)["matched"] is True
    rows2 = _bars([10, 11, 12, 13, 20])  # 最高在最后一根
    assert evaluate_formula("HHVBARS(H,5) = 0", rows2)["matched"] is True


def test_codelike_board_filter():
    """CODELIKE 按股票代码前缀过滤,可去除创业板/科创板"""
    rows = _bars(range(1, 40))
    # 主板 600519:非双创 → NOT(CODELIKE('300')) 成立
    main = "NOT(CODELIKE('300')) AND NOT(CODELIKE('688'))"
    assert evaluate_formula(main, rows, symbol="600519")["matched"] is True
    # 创业板 300750:CODELIKE('300') 命中 → 被排除
    assert evaluate_formula(main, rows, symbol="300750")["matched"] is False
    # 带交易所前缀也能识别
    assert evaluate_formula("CODELIKE('688')", rows, symbol="SH688981")["matched"] is True


def test_breakout_pool_formula_runs():
    """突破前高观察池公式(引擎兼容版)可解析并求值为布尔"""
    rows = _bars([10] * 65 + [10.2, 10.4, 10.6, 11.5])  # 末尾突破前高
    formula = (
        "N:=60; M:=5; K:=5; BR:=1.005; DEV:=1.25;\n"
        "MA5:=MA(C,5); MA10:=MA(C,10); MA20:=MA(C,20); MA60:=MA(C,60);\n"
        "UPT:=MA5>=MA10 AND MA10>=MA20 AND MA20>=MA60 AND MA60>=REF(MA60,10) AND C>MA20 AND C/MA20<DEV;\n"
        "G:=REF(HHV(H,N),1);\n"
        "GB:=REF(HHVBARS(H,N),1)+1; DIST:=GB>=M;\n"
        "TP:=C>=G*BR AND REF(C,1)<G*BR AND DIST AND UPT;\n"
        "XG:COUNT(TP,K+1)>0 AND NOT(CODELIKE('300')) AND NOT(CODELIKE('688'));"
    )
    assert isinstance(evaluate_formula(formula, rows, symbol="600519")["matched"], bool)


def test_unknown_function_is_blocked():
    with pytest.raises(FormulaError):
        parse_formula("__import__(1)")

