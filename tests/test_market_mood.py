"""大盘情绪合成与缓存逻辑测试。"""

from src.core.market_mood import _pick_column, compute_sentiment


def test_sentiment_neutral_when_all_missing():
    """输入全部缺失时返回中性50分且置信度为0"""
    result = compute_sentiment(
        up_count=None,
        down_count=None,
        limit_up_count=None,
        limit_down_count=None,
        main_net_inflow_yi=None,
    )
    assert result["score"] == 50.0
    assert result["label"] == "中性"
    assert result["confidence"] == 0.0


def test_sentiment_bullish_market():
    """普涨+涨停远多于跌停+主力大幅净流入时评分应偏暖或亢奋"""
    result = compute_sentiment(
        up_count=4200,
        down_count=800,
        limit_up_count=80,
        limit_down_count=3,
        main_net_inflow_yi=250.0,
    )
    assert result["score"] >= 70
    assert result["label"] in ("偏暖", "亢奋")
    assert result["confidence"] == 1.0


def test_sentiment_bearish_market():
    """普跌+跌停多于涨停+主力大幅净流出时评分应偏冷或冰点"""
    result = compute_sentiment(
        up_count=600,
        down_count=4500,
        limit_up_count=5,
        limit_down_count=40,
        main_net_inflow_yi=-320.0,
    )
    assert result["score"] <= 30
    assert result["label"] in ("偏冷", "冰点")


def test_sentiment_flow_clamped():
    """主力净流入超过±300亿按满格计,不会溢出0-100区间"""
    hot = compute_sentiment(
        up_count=5000,
        down_count=100,
        limit_up_count=100,
        limit_down_count=0,
        main_net_inflow_yi=9999.0,
    )
    cold = compute_sentiment(
        up_count=100,
        down_count=5000,
        limit_up_count=0,
        limit_down_count=100,
        main_net_inflow_yi=-9999.0,
    )
    assert 0 <= cold["score"] < hot["score"] <= 100


def test_pick_column_fuzzy_match():
    """列名模糊匹配:需同时包含全部关键词"""
    cols = ["名称", "今日涨跌幅", "今日主力净流入-净额", "今日主力净流入-净占比"]
    assert _pick_column(cols, "主力净流入", "净额") == "今日主力净流入-净额"
    assert _pick_column(cols, "主力净流入", "净占比") == "今日主力净流入-净占比"
    assert _pick_column(cols, "不存在") is None
