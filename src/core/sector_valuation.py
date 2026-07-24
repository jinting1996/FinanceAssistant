"""板块估值分位:申万一级行业日频 PE/PB 采集与历史分位计算。

数据源:akshare `index_analysis_daily_sw`(申万一级行业日频估值),
延续项目 akshare 用法(延迟 import + _pick_column 模糊匹配,见 market_mood.py)。

板块码映射:腾讯行业板块码 `pt01<sw_code>`(如 pt01801080)去掉 `pt01` 前缀
即申万一级行业代码(801080),31 个行业一一对应;概念板块无申万估值。

关键约束:index_analysis_daily_sw 按年分段拉取(单年 ~2s,整段多年会超时)。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from src.web.models import SectorValuationDaily

logger = logging.getLogger(__name__)

TENCENT_INDUSTRY_PREFIX = "pt01"
# 分位窗口(交易日近似):3 年 ~730,5 年 ~1220
PERCENTILE_WINDOWS = {"3y": 730, "5y": 1220}


def tencent_code_to_sw(board_code: str, scope: str = "industry") -> str | None:
    """腾讯行业板块码 -> 申万一级行业代码;非行业板块或不匹配返回 None。"""
    if scope != "industry":
        return None
    code = (board_code or "").strip()
    if not code.startswith(TENCENT_INDUSTRY_PREFIX):
        return None
    sw = code[len(TENCENT_INDUSTRY_PREFIX):]
    return sw if sw.isdigit() and len(sw) == 6 else None


def _pick_column(columns: list[str], *keywords: str) -> str | None:
    for col in columns:
        if all(kw in str(col) for kw in keywords):
            return str(col)
    return None


def _num(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v == v else None  # NaN 过滤


def _fetch_sw_range(start_date: str, end_date: str) -> list[dict]:
    """拉取指定区间全部申万一级行业的日频估值(YYYYMMDD)。返回标准化行列表。

    注意:接口按交易日内部翻页,区间越长越慢(单年可达数分钟),长区间须分段/后台。
    """
    import akshare as ak

    df = ak.index_analysis_daily_sw(symbol="一级行业", start_date=start_date, end_date=end_date)
    if df is None or df.empty:
        return []
    cols = df.columns.tolist()
    code_col = _pick_column(cols, "指数代码") or _pick_column(cols, "代码")
    name_col = _pick_column(cols, "指数名称") or _pick_column(cols, "名称")
    date_col = _pick_column(cols, "发布日期") or _pick_column(cols, "日期")
    pe_col = _pick_column(cols, "市盈率")
    pb_col = _pick_column(cols, "市净率")
    div_col = _pick_column(cols, "股息率")
    turn_col = _pick_column(cols, "换手率")
    close_col = _pick_column(cols, "收盘指数") or _pick_column(cols, "收盘")
    if not code_col or not date_col:
        raise ValueError(f"申万估值字段不符合预期: {cols}")
    out: list[dict] = []
    for _, row in df.iterrows():
        code = str(row[code_col]).strip()
        date_raw = str(row[date_col]).strip()[:10]
        if not code or not date_raw:
            continue
        out.append(
            {
                "sw_code": code,
                "sw_name": str(row[name_col]).strip() if name_col else "",
                "date": date_raw,
                "pe": _num(row[pe_col]) if pe_col else None,
                "pb": _num(row[pb_col]) if pb_col else None,
                "dividend_yield": _num(row[div_col]) if div_col else None,
                "turnover_rate": _num(row[turn_col]) if turn_col else None,
                "close_index": _num(row[close_col]) if close_col else None,
            }
        )
    return out


def _upsert_rows(db: Session, rows: list[dict]) -> int:
    """按 (sw_code, date) 幂等写入。返回新增/更新行数。"""
    if not rows:
        return 0
    now = datetime.now()
    # 预取已有键,减少逐行查询
    existing = {
        (r.sw_code, r.date): r
        for r in db.query(SectorValuationDaily)
        .filter(SectorValuationDaily.date >= min(r["date"] for r in rows))
        .all()
    }
    changed = 0
    for r in rows:
        key = (r["sw_code"], r["date"])
        obj = existing.get(key)
        if obj is None:
            obj = SectorValuationDaily(sw_code=r["sw_code"], date=r["date"])
            db.add(obj)
            existing[key] = obj
        obj.sw_name = r["sw_name"] or obj.sw_name or ""
        obj.pe = r["pe"]
        obj.pb = r["pb"]
        obj.dividend_yield = r["dividend_yield"]
        obj.turnover_rate = r["turnover_rate"]
        obj.close_index = r["close_index"]
        obj.fetched_at = now
        changed += 1
    db.commit()
    return changed


def backfill_years(db: Session, *, start_year: int, end_year: int | None = None) -> dict:
    """逐年回填申万一级行业估值历史(每年一次接口调用,避免长区间超时)。"""
    end_year = end_year or datetime.now().year
    today = datetime.now().strftime("%Y%m%d")
    total = 0
    years_done = []
    for year in range(start_year, end_year + 1):
        end = min(f"{year}1231", today)
        try:
            rows = _fetch_sw_range(f"{year}0101", end)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[板块估值] %d 年回填失败: %s", year, exc)
            continue
        total += _upsert_rows(db, rows)
        years_done.append(year)
        logger.info("[板块估值] %d 年回填 %d 行", year, len(rows))
    return {"years": years_done, "rows": total}


def refresh_latest(db: Session, *, lookback_days: int = 15) -> dict:
    """增量刷新最近 lookback_days 的估值(收盘后日更,短窗口避免整年慢查询)。"""
    from datetime import timedelta

    end = datetime.now()
    start = end - timedelta(days=lookback_days)
    try:
        rows = _fetch_sw_range(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[板块估值] 增量刷新失败: %s", exc)
        return {"rows": 0, "error": str(exc)}
    n = _upsert_rows(db, rows)
    return {"rows": n}


def _percentile(series: list[float], value: float) -> float:
    """value 在 series 中的百分位(0-100),含并列。空序列返回 -1。"""
    if not series:
        return -1.0
    below = sum(1 for x in series if x < value)
    equal = sum(1 for x in series if x == value)
    rank = below + equal / 2.0
    return round(rank / len(series) * 100.0, 1)


def compute_valuation(db: Session, sw_code: str) -> dict | None:
    """计算某申万行业的当前估值 + PE/PB 历史分位(3年/5年)。无数据返回 None。"""
    rows = (
        db.query(SectorValuationDaily)
        .filter(SectorValuationDaily.sw_code == sw_code)
        .order_by(SectorValuationDaily.date.asc())
        .all()
    )
    if not rows:
        return None
    latest = rows[-1]
    pe_hist_all = [r.pe for r in rows if r.pe is not None and r.pe > 0]
    pb_hist_all = [r.pb for r in rows if r.pb is not None and r.pb > 0]

    def _pct_windows(hist: list[float], current: float | None) -> dict:
        result: dict[str, float | None] = {}
        for key, win in PERCENTILE_WINDOWS.items():
            if current is None or current <= 0:
                result[key] = None
                continue
            window = hist[-win:] if len(hist) > win else hist
            p = _percentile(window, current)
            result[key] = p if p >= 0 else None
        return result

    pe_pct = _pct_windows(pe_hist_all, latest.pe)
    pb_pct = _pct_windows(pb_hist_all, latest.pb)
    return {
        "sw_code": sw_code,
        "sw_name": latest.sw_name,
        "date": latest.date,
        "pe": latest.pe,
        "pb": latest.pb,
        "dividend_yield": latest.dividend_yield,
        "history_days": len(rows),
        "pe_percentile": pe_pct,
        "pb_percentile": pb_pct,
    }


def compute_valuation_map(db: Session, sw_codes: list[str]) -> dict[str, dict]:
    """批量计算多个申万行业的紧凑估值(pe + 3年PE分位 + 标签),单次查询。

    返回 {sw_code: {pe, pe_percentile_3y, label}};无历史的 code 不出现在结果里。
    """
    codes = [c for c in dict.fromkeys(sw_codes) if c]
    if not codes:
        return {}
    rows = (
        db.query(SectorValuationDaily)
        .filter(SectorValuationDaily.sw_code.in_(codes))
        .order_by(SectorValuationDaily.sw_code.asc(), SectorValuationDaily.date.asc())
        .all()
    )
    grouped: dict[str, list] = {}
    for r in rows:
        grouped.setdefault(r.sw_code, []).append(r)
    win = PERCENTILE_WINDOWS["3y"]
    out: dict[str, dict] = {}
    for code, recs in grouped.items():
        latest = recs[-1]
        if latest.pe is None or latest.pe <= 0:
            continue
        pe_hist = [x.pe for x in recs if x.pe is not None and x.pe > 0]
        window = pe_hist[-win:] if len(pe_hist) > win else pe_hist
        p = _percentile(window, latest.pe)
        pct = p if p >= 0 else None
        out[code] = {
            "pe": latest.pe,
            "pe_percentile_3y": pct,
            "label": valuation_label(pct),
            "history_days": len(recs),
        }
    return out


def valuation_label(pe_percentile: float | None) -> str:
    """PE 分位 -> 估值标签。"""
    if pe_percentile is None:
        return "unknown"
    if pe_percentile < 20:
        return "low"       # 低估
    if pe_percentile > 80:
        return "high"      # 高估
    return "fair"          # 合理
