"""每日复盘导出与成交流水单测。"""

from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.web.api import review, trades
from src.web.api.trades import TradeRecordCreate
from src.web.database import Base
from src.web.models import Account, Position, Stock, StockKlineCache, TradeRecord


def _session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'review.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _add_kline(db, symbol: str, date_str: str, close: float, volume: float):
    db.add(
        StockKlineCache(
            market="CN",
            symbol=symbol,
            date=date_str,
            open=close,
            high=close,
            low=close,
            close=close,
            volume=volume,
            source="test",
        )
    )


# ---------------------------------------------------------------------------
# 成交流水 CRUD
# ---------------------------------------------------------------------------

def test_trade_create_and_list_by_date(tmp_path):
    """录入成交后可按日期过滤查询，金额缺省时按价格×数量自动计算。"""
    Session = _session_factory(tmp_path)
    db = Session()
    try:
        created = trades.create_trade(
            TradeRecordCreate(
                symbol="600519",
                name="贵州茅台",
                direction="buy",
                price=1500.5,
                quantity=100,
                traded_at=datetime(2026, 7, 16, 10, 30),
            ),
            db=db,
        )
        assert created["amount"] == pytest.approx(150050.0)

        result = trades.list_trades(date="2026-07-16", db=db)
        assert len(result) == 1
        assert result[0]["symbol"] == "600519"
        assert result[0]["direction"] == "buy"

        # 其他日期查不到
        assert trades.list_trades(date="2026-07-15", db=db) == []
    finally:
        db.close()


def test_trade_direction_validation():
    """成交方向仅允许 buy/sell，非法值抛出校验错误。"""
    with pytest.raises(ValidationError):
        TradeRecordCreate(symbol="600519", direction="hold", price=10, quantity=100)


def test_trade_delete(tmp_path):
    """删除成交记录后列表为空。"""
    Session = _session_factory(tmp_path)
    db = Session()
    try:
        created = trades.create_trade(
            TradeRecordCreate(symbol="000001", direction="sell", price=12.3, quantity=200),
            db=db,
        )
        assert trades.delete_trade(created["id"], db=db) == {"success": True}
        assert db.query(TradeRecord).count() == 0
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 复盘数据采集
# ---------------------------------------------------------------------------

def _fake_summary(position_id: int) -> dict:
    return {
        "accounts": [
            {
                "id": 1,
                "name": "默认账户",
                "available_funds": 10000.0,
                "total_market_value": 10700.0,
                "total_cost": 10000.0,
                "total_pnl": 700.0,
                "total_pnl_pct": 7.0,
                "total_daily_pnl": 200.0,
                "total_assets": 20700.0,
                "positions": [
                    {
                        "id": position_id,
                        "stock_id": 1,
                        "symbol": "600519",
                        "name": "贵州茅台",
                        "market": "CN",
                        "cost_price": 100.0,
                        "quantity": 100,
                        "current_price": 107.0,
                        "change_pct": 1.9,
                        "market_value_cny": 10700.0,
                        "pnl": 700.0,
                        "pnl_pct": 7.0,
                        "exchange_rate": None,
                    }
                ],
            }
        ],
        "total": {
            "total_market_value": 10700.0,
            "total_cost": 10000.0,
            "total_pnl": 700.0,
            "total_pnl_pct": 7.0,
            "total_daily_pnl": 200.0,
            "available_funds": 10000.0,
            "total_assets": 20700.0,
        },
    }


def _seed_position(db) -> int:
    account = Account(name="默认账户", available_funds=10000.0)
    stock = Stock(symbol="600519", name="贵州茅台", market="CN")
    db.add_all([account, stock])
    db.flush()
    pos = Position(
        account_id=account.id, stock_id=stock.id, cost_price=100.0, quantity=100
    )
    db.add(pos)
    db.commit()
    return pos.id


def test_collect_review_data_past_date(tmp_path, monkeypatch):
    """历史日期采集：本周盈亏按周初前收盘回算，成交按日期过滤，跳过实时行情。"""
    Session = _session_factory(tmp_path)
    db = Session()
    try:
        position_id = _seed_position(db)
        # 2026-06-18 为周四，所在周周一为 2026-06-15
        _add_kline(db, "600519", "2026-06-12", close=100.0, volume=30000)  # 周初基准
        _add_kline(db, "600519", "2026-06-17", close=105.0, volume=50000)  # 昨日
        db.add(
            TradeRecord(
                symbol="600519",
                market="CN",
                name="贵州茅台",
                direction="buy",
                price=104.5,
                quantity=100,
                amount=10450.0,
                traded_at=datetime(2026, 6, 18, 14, 30),
            )
        )
        db.commit()

        monkeypatch.setattr(
            review, "get_portfolio_summary",
            lambda account_id=None, include_quotes=True, db=None: _fake_summary(position_id),
        )

        data = review.collect_review_data(db, datetime(2026, 6, 18))

        assert data["is_today"] is False
        # 本周盈亏 = (107 - 100) * 100 = 700
        assert data["total"]["weekly_pnl"] == pytest.approx(700.0)
        assert data["total"]["position_ratio"] == pytest.approx(51.69, abs=0.01)
        # 成交按日期过滤
        assert len(data["trades"]) == 1
        assert data["trades"][0]["time"] == "14:30"
        # 非当日不抓实时行情，但昨日量来自K线缓存
        assert data["quotes_detail"][0]["open"] is None
        assert data["quotes_detail"][0]["prev_volume"] == pytest.approx(50000)
        # 持有天数按录入日估算
        assert data["positions"][0]["holding_days"] is not None
    finally:
        db.close()


def test_collect_review_data_today_quotes(tmp_path, monkeypatch):
    """当日采集：实时行情提供 OHLC 与成交量，量比 = 今日量 / 昨日量。"""
    Session = _session_factory(tmp_path)
    db = Session()
    try:
        position_id = _seed_position(db)
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        _add_kline(db, "600519", yesterday, close=105.0, volume=50000)
        db.commit()

        monkeypatch.setattr(
            review, "get_portfolio_summary",
            lambda account_id=None, include_quotes=True, db=None: _fake_summary(position_id),
        )
        monkeypatch.setattr(
            review, "_fetch_quotes_for_stocks",
            lambda stocks: {
                "600519": {
                    "open_price": 106.0,
                    "high_price": 108.0,
                    "low_price": 104.0,
                    "volume": 100000.0,
                }
            },
        )

        data = review.collect_review_data(db, datetime.now())

        assert data["is_today"] is True
        quote = data["quotes_detail"][0]
        assert quote["open"] == pytest.approx(106.0)
        assert quote["volume_ratio"] == pytest.approx(2.0)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Markdown 渲染
# ---------------------------------------------------------------------------

def _sample_data() -> dict:
    return {
        "date": "2026-07-16",
        "weekday": "周四",
        "generated_at": "2026-07-16 15:45",
        "is_today": True,
        "total": {
            "total_market_value": 10700.0,
            "total_pnl": 700.0,
            "total_pnl_pct": 7.0,
            "total_daily_pnl": 200.0,
            "available_funds": 10000.0,
            "total_assets": 20700.0,
            "position_ratio": 51.69,
            "daily_pnl_pct": 0.98,
            "weekly_pnl": 700.0,
            "weekly_pnl_pct": 3.5,
            "weekly_incomplete": False,
        },
        "accounts": [],
        "positions": [
            {
                "id": 1,
                "symbol": "600519",
                "name": "贵州茅台",
                "market": "CN",
                "cost_price": 100.0,
                "current_price": 107.0,
                "quantity": 100,
                "position_ratio": 51.69,
                "holding_days": 12,
                "pnl": 700.0,
                "pnl_pct": 7.0,
                "change_pct": 1.9,
            }
        ],
        "trades": [
            {
                "time": "14:30",
                "symbol": "600519",
                "name": "贵州茅台",
                "direction": "buy",
                "price": 104.5,
                "quantity": 100,
                "amount": 10450.0,
                "note": "回踩加仓",
            }
        ],
        "quotes_detail": [
            {
                "symbol": "600519",
                "name": "贵州茅台",
                "market": "CN",
                "open": 106.0,
                "high": 108.0,
                "low": 104.0,
                "current": 107.0,
                "change_pct": 1.9,
                "volume": 100000.0,
                "prev_volume": 50000.0,
                "volume_ratio": 2.0,
            }
        ],
    }


def test_render_markdown_contains_sections():
    """默认渲染包含四大板块与关键数据。"""
    md = review.render_review_markdown(_sample_data(), hide_amounts=False)
    assert "## 一、账户概况" in md
    assert "## 二、持仓快照" in md
    assert "## 三、今日成交" in md
    assert "## 四、个股当日行情" in md
    assert "总资产：¥20,700.00" in md
    assert "600519" in md
    assert "12天" in md
    assert "买入" in md
    assert "回踩加仓" in md
    assert "10.00万" in md  # 今日成交量 100000 -> 10.00万


def test_render_markdown_hide_amounts():
    """hide_amounts 模式不输出任何绝对金额，仅保留比例。"""
    md = review.render_review_markdown(_sample_data(), hide_amounts=True)
    assert "¥" not in md
    assert "总资产" not in md
    assert "+7.00%" in md  # 浮盈百分比保留
    assert "51.69%" in md  # 仓位比例保留
    # 成交表不含金额列
    assert "10,450.00" not in md


def test_render_markdown_empty_states():
    """无持仓、无成交时渲染兜底文案。"""
    data = _sample_data()
    data["positions"] = []
    data["trades"] = []
    data["quotes_detail"] = []
    md = review.render_review_markdown(data)
    assert "当前无持仓。" in md
    assert "今日无成交记录。" in md
    assert "无持仓个股。" in md
