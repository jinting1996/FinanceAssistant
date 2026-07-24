"""板块估值分位:码映射、分位计算、批量、标签、API 的单元测试(不打网络)。"""

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core import sector_valuation as sv
from src.web.api.market_events import get_board_valuation
from src.web.database import Base
from src.web.models import SectorValuationDaily, WatchedBoard


def _session():
    e = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(e)
    return sessionmaker(bind=e)()


def _seed_history(db, sw_code="801080", n=300, pe_series=None):
    """写入 n 天历史;pe_series 指定则用之,否则线性 10..(10+n)。"""
    from datetime import date, timedelta

    start = date(2023, 1, 1)
    for i in range(n):
        pe = pe_series[i] if pe_series else 10.0 + i
        db.add(
            SectorValuationDaily(
                sw_code=sw_code,
                sw_name="电子",
                date=(start + timedelta(days=i)).isoformat(),
                pe=pe,
                pb=pe / 10.0,
                dividend_yield=1.5,
                close_index=1000.0 + i,
            )
        )
    db.commit()


def test_tencent_code_to_sw():
    """腾讯行业码去 pt01 前缀=申万码;概念/非法返回 None。"""
    assert sv.tencent_code_to_sw("pt01801080", "industry") == "801080"
    assert sv.tencent_code_to_sw("pt01801780", "industry") == "801780"
    assert sv.tencent_code_to_sw("gn_ai", "concept") is None
    assert sv.tencent_code_to_sw("pt01801080", "concept") is None  # 概念 scope 不映射
    assert sv.tencent_code_to_sw("BK0480", "industry") is None      # 非腾讯格式


def test_percentile_basic():
    """百分位:最小值≈0,最大值≈接近100,中位≈50。"""
    series = [float(x) for x in range(100)]  # 0..99
    assert sv._percentile(series, 0) < 5
    assert sv._percentile(series, 99) > 95
    assert 45 <= sv._percentile(series, 50) <= 55
    assert sv._percentile([], 5) == -1.0


def test_compute_valuation_and_label():
    """当前 PE 为历史最高 -> 分位接近100 -> 高估;最低 -> 低估。"""
    db = _session()
    # 递增序列,最后一天 PE 最高
    _seed_history(db, n=300)
    val = sv.compute_valuation(db, "801080")
    assert val is not None
    assert val["history_days"] == 300
    assert val["pe_percentile"]["3y"] > 95
    assert sv.valuation_label(val["pe_percentile"]["3y"]) == "high"

    # 追加一天极低 PE,应变低估
    from datetime import date

    db.add(SectorValuationDaily(sw_code="801080", sw_name="电子", date=date(2024, 1, 1).isoformat(), pe=1.0, pb=0.1))
    db.commit()
    val2 = sv.compute_valuation(db, "801080")
    assert val2["pe_percentile"]["3y"] < 5
    assert sv.valuation_label(val2["pe_percentile"]["3y"]) == "low"


def test_valuation_label_thresholds():
    """分位阈值:<20 低估, 20-80 合理, >80 高估, None 未知。"""
    assert sv.valuation_label(10) == "low"
    assert sv.valuation_label(50) == "fair"
    assert sv.valuation_label(90) == "high"
    assert sv.valuation_label(None) == "unknown"


def test_compute_valuation_map_batch():
    """批量估值:多行业一次算,无数据的 code 不出现。"""
    db = _session()
    _seed_history(db, sw_code="801080", n=100)
    _seed_history(db, sw_code="801780", n=100)
    vm = sv.compute_valuation_map(db, ["801080", "801780", "801150"])
    assert set(vm.keys()) == {"801080", "801780"}  # 801150 无数据
    assert vm["801080"]["label"] in ("low", "fair", "high")
    assert vm["801080"]["history_days"] == 100


def test_compute_valuation_empty():
    """无历史返回 None。"""
    db = _session()
    assert sv.compute_valuation(db, "801080") is None


def test_valuation_api_industry_and_concept():
    """估值API:行业板块有值;概念板块 available=False。"""
    db = _session()
    _seed_history(db, sw_code="801080", n=300)
    db.add(WatchedBoard(market="CN", board_code="pt01801080", board_name="电子", scope="industry", category="theme"))
    db.add(WatchedBoard(market="CN", board_code="gn_ai", board_name="人工智能", scope="concept", category="theme"))
    db.commit()

    res_ind = get_board_valuation("pt01801080", market="CN", db=db)
    assert res_ind["available"] is True
    assert res_ind["sw_code"] == "801080"
    assert res_ind["label"] in ("low", "fair", "high")

    res_con = get_board_valuation("gn_ai", market="CN", db=db)
    assert res_con["available"] is False


def test_valuation_api_industry_without_history():
    """行业板块但估值库为空 -> available=False,提示回填。"""
    db = _session()
    db.add(WatchedBoard(market="CN", board_code="pt01801080", board_name="电子", scope="industry", category="theme"))
    db.commit()
    res = get_board_valuation("pt01801080", market="CN", db=db)
    assert res["available"] is False
