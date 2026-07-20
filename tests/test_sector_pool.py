"""板块池:种子播种、分组API、事件标注CRUD、周K聚合的单元测试。"""

import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.sector_pool import SECTOR_POOL_SEED, seed_sector_pool
from src.web.api import market_events
from src.web.api.market_events import (
    BoardEventMarkRequest,
    BoardEventMarkUpdateRequest,
    PoolBoardUpdateRequest,
    _aggregate_weekly,
    create_board_event_mark,
    delete_board_event_mark,
    delete_board_from_watchlist,
    get_board_pool,
    list_board_event_marks,
    update_board_event_mark,
    update_pool_board,
)
from src.web.database import Base
from src.web.models import WatchedBoard


FAKE_BOARDS = [
    {"code": "pt_bank", "name": "银行", "scope": "industry", "change_pct": 1.2, "turnover": 1e9},
    {"code": "pt_nonbank", "name": "非银金融", "scope": "industry", "change_pct": 0.5, "turnover": 2e9},
    {"code": "pt_metal", "name": "有色金属", "scope": "industry", "change_pct": -0.8, "turnover": 3e9},
    {"code": "pt_comm", "name": "通信", "scope": "industry", "change_pct": 2.4, "turnover": 4e9},
    {"code": "gn_ai", "name": "人工智能", "scope": "concept", "change_pct": 3.3, "turnover": 5e9},
    {"code": "gn_cpo", "name": "共封装光模块(CPO）", "scope": "concept", "change_pct": 4.1, "turnover": 6e9},
    {"code": "gn_gold", "name": "黄金概念", "scope": "concept", "change_pct": 0.1, "turnover": 7e9},
]


def _session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'sector_pool.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def test_seed_sector_pool_creates_and_is_idempotent(tmp_path):
    """种子播种按分类落库,重复播种不产生重复行。"""
    db = _session_factory(tmp_path)()
    report = seed_sector_pool(db, FAKE_BOARDS)
    assert report["created"] > 0
    count_after_first = db.query(WatchedBoard).count()

    bank = db.query(WatchedBoard).filter(WatchedBoard.board_code == "pt_bank").first()
    assert bank is not None
    assert bank.category == "finance"
    assert bank.tier == "pool"
    assert bank.scope == "industry"

    cpo = db.query(WatchedBoard).filter(WatchedBoard.board_code == "gn_cpo").first()
    assert cpo is not None
    assert cpo.category == "channel"
    assert cpo.scope == "concept"

    report2 = seed_sector_pool(db, FAKE_BOARDS)
    assert report2["created"] == 0
    assert db.query(WatchedBoard).count() == count_after_first


def test_seed_reports_unresolved(tmp_path):
    """名单里找不到的种子板块进入 unresolved 报告而不是报错。"""
    db = _session_factory(tmp_path)()
    report = seed_sector_pool(db, FAKE_BOARDS)
    assert len(report["unresolved"]) == len(SECTOR_POOL_SEED) - report["created"]


def test_board_pool_groups_by_category(tmp_path, monkeypatch):
    """板块池接口按分类分组返回,并合并当日涨跌幅。"""
    Session = _session_factory(tmp_path)
    db = Session()
    seed_sector_pool(db, FAKE_BOARDS)

    async def fake_fetch(proxy=None):
        return FAKE_BOARDS

    monkeypatch.setattr(market_events, "fetch_all_boards", fake_fetch)
    result = asyncio.run(get_board_pool(market="CN", auto_seed=False, db=db))
    keys = [c["key"] for c in result["categories"]]
    assert "finance" in keys
    assert "channel" in keys
    finance = next(c for c in result["categories"] if c["key"] == "finance")
    bank = next(b for b in finance["boards"] if b["board_code"] == "pt_bank")
    assert bank["change_pct"] == 1.2


def test_pool_pin_respects_limit_and_unpin(tmp_path):
    """pin 受最多8个限制,unpin 回到池内;取消关注默认降级不删除。"""
    db = _session_factory(tmp_path)()
    seed_sector_pool(db, FAKE_BOARDS)

    updated = update_pool_board("pt_bank", PoolBoardUpdateRequest(tier="pinned"), db=db)
    assert updated["tier"] == "pinned"

    for i in range(market_events.MAX_WATCHED_BOARDS - 1):
        db.add(
            WatchedBoard(
                market="CN",
                board_code=f"extra_{i}",
                board_name=f"额外{i}",
                tier="pinned",
                enabled=True,
            )
        )
    db.commit()

    with pytest.raises(HTTPException):
        update_pool_board("pt_metal", PoolBoardUpdateRequest(tier="pinned"), db=db)

    result = delete_board_from_watchlist("pt_bank", market="CN", hard=False, db=db)
    assert result.get("demoted") is True
    bank = db.query(WatchedBoard).filter(WatchedBoard.board_code == "pt_bank").first()
    assert bank is not None and bank.tier == "pool"


def test_event_mark_crud(tmp_path):
    """事件标注:创建、列表、更新、删除全链路。"""
    db = _session_factory(tmp_path)()
    created = create_board_event_mark(
        "pt_comm",
        BoardEventMarkRequest(date="2021-09-22", event_type="policy", title="双碳目标发布", summary="政策底", importance=2),
        db=db,
    )
    assert created["event_type"] == "policy"
    assert created["importance"] == 2

    rows = list_board_event_marks("pt_comm", market="CN", db=db)
    assert len(rows) == 1

    updated = update_board_event_mark(created["id"], BoardEventMarkUpdateRequest(title="双碳政策底"), db=db)
    assert updated["title"] == "双碳政策底"

    delete_board_event_mark(created["id"], db=db)
    assert list_board_event_marks("pt_comm", market="CN", db=db) == []


def test_event_mark_validation(tmp_path):
    """事件标注:非法日期与非法类型被拒绝。"""
    db = _session_factory(tmp_path)()
    with pytest.raises(HTTPException):
        create_board_event_mark(
            "pt_comm",
            BoardEventMarkRequest(date="2021/09/22", event_type="policy", title="x"),
            db=db,
        )
    with pytest.raises(HTTPException):
        create_board_event_mark(
            "pt_comm",
            BoardEventMarkRequest(date="2021-09-22", event_type="rumor", title="x"),
            db=db,
        )


def test_aggregate_weekly():
    """日K按 ISO 周聚合:高低取极值、收盘取周末、量额累加。"""
    rows = [
        {"date": "2024-01-01", "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 100, "turnover": 1000},
        {"date": "2024-01-02", "open": 10.5, "high": 12, "low": 10, "close": 11, "volume": 100, "turnover": 1000},
        {"date": "2024-01-05", "open": 11, "high": 11.5, "low": 8, "close": 9, "volume": 100, "turnover": 1000},
        {"date": "2024-01-08", "open": 9, "high": 9.5, "low": 8.5, "close": 9.2, "volume": 50, "turnover": 500},
    ]
    weekly = _aggregate_weekly(rows)
    assert len(weekly) == 2
    first = weekly[0]
    assert first["date"] == "2024-01-05"
    assert first["open"] == 10
    assert first["high"] == 12
    assert first["low"] == 8
    assert first["close"] == 9
    assert first["volume"] == 300
    second = weekly[1]
    assert second["date"] == "2024-01-08"
    assert second["close"] == 9.2
