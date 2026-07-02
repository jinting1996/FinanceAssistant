"""突破结构与量价识别的单测。"""

from __future__ import annotations

from dataclasses import dataclass

from src.core.breakout_context import (
    analyze_breakout,
    build_limit_line,
    find_pivot_highs,
    format_daily_klines,
    is_limit_move,
)


@dataclass
class Bar:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float


def _bar(day: int, close: float, high: float | None = None, vol: float = 1000.0) -> Bar:
    h = high if high is not None else close
    return Bar(f"2026-01-{day:02d}", close, h, close - 0.1, close, vol)


def test_涨停判定按板块比例():
    """主板 10% 涨停、创业板 20%，容忍零点几个百分点。"""
    assert is_limit_move(9.95, 0.10) == 1
    assert is_limit_move(5.0, 0.10) == 0
    assert is_limit_move(-9.98, 0.10) == -1
    assert is_limit_move(19.9, 0.20) == 1


def test_摆动高点需要两侧确认最近若干根不算():
    """pivot 需右侧 k 根确认，最近 k 根不可能成为前高，避免把上涨途中新高当前高。"""
    highs = [10, 11, 12, 15, 12, 11, 10, 11, 13, 16]  # 索引3是高点15，末尾16未确认
    pivots = find_pivot_highs(highs, k=2)
    assert 3 in pivots
    assert 9 not in pivots  # 最后一根即便更高也不算


def test_突破后前高锚点固定不随新高上移():
    """构造：箱体高点15 → 突破 → 一路创新高。前高应锚在15而非最新高点。"""
    bars = []
    # 前期箱体，高点 15（第4天），两侧有确认
    for i, c in enumerate([12, 13, 15, 13, 12, 11, 12, 13], start=1):
        bars.append(_bar(i, c, high=c))
    # 突破日起一路走高到 20
    for i, c in enumerate([16, 17, 18, 19, 20], start=9):
        bars.append(_bar(i, c, high=c))
    # 补足 min_bars
    while len(bars) < 20:
        bars.append(_bar(len(bars) + 1, 20, high=20))

    info = analyze_breakout(bars, pivot_k=2)
    assert info.has_data
    assert info.broke is True
    assert info.confirmed is True
    assert info.prev_high == 15  # 锚点是突破前的箱体高点
    assert info.post_breakout_high is not None and info.post_breakout_high >= 20
    assert "不改变前高" in info.summary


def test_未突破时前高为上方最近阻力():
    """现价仍在前高之下时，前高=上方最近的已确认摆动高点。"""
    bars = []
    for i, c in enumerate([12, 13, 18, 13, 12, 11, 12, 13, 12, 13], start=1):
        bars.append(_bar(i, c, high=c))
    while len(bars) < 20:
        bars.append(_bar(len(bars) + 1, 13, high=13))
    info = analyze_breakout(bars, pivot_k=2)
    assert info.has_data
    assert info.broke is False
    assert info.prev_high == 18
    assert info.gap_to_prev_high_pct is not None and info.gap_to_prev_high_pct < 0


def test_日K标注放量涨停():
    """放量涨停那天应带 涨停 + 放量 标记。"""
    bars = [_bar(i, 10.0, high=10.0, vol=1000) for i in range(1, 7)]
    # 第7天涨停(+10%)且放量(相对前5日均量放大)
    bars.append(_bar(7, 11.0, high=11.0, vol=5000))
    lines = format_daily_klines(bars, symbol="600000", name="测试")
    last = lines[-1]
    assert "涨停" in last
    assert "放量" in last


def test_涨跌停线():
    """创业板 20%，主板 10%。"""
    assert "20%" in build_limit_line(0.0, 10.0, symbol="300001", name="创业")
    assert "10%" in build_limit_line(0.0, 10.0, symbol="600000", name="主板")
    assert build_limit_line(None, None) == ""
