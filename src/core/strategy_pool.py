"""Strategy pool helpers shared by strategy UI, screener, and paper trading."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.core.strategy_catalog import ensure_strategy_catalog, list_strategy_catalog
from src.web.database import SessionLocal
from src.web.models import (
    AppSettings,
    BacktestStrategyMetric,
    PaperTradingTrade,
    StockScreenerFormula,
    StrategyCatalog,
    StrategyOutcome,
)

PAPER_STRATEGY_SELECTION_KEY = "paper_trading_strategy_selection"

STRATEGY_TYPES = {"builtin", "screener_formula", "mcp", "agent"}
RISK_LEVELS = {"low", "medium", "high"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json_loads(raw: str | None, fallback):
    if not raw:
        return fallback
    try:
        data = json.loads(raw)
        return data if data is not None else fallback
    except Exception:
        return fallback


def _setting(db: Session, key: str) -> AppSettings | None:
    return db.query(AppSettings).filter(AppSettings.key == key).first()


def _save_setting(db: Session, key: str, value: str, description: str = "") -> None:
    row = _setting(db, key)
    if row:
        row.value = value
    else:
        db.add(AppSettings(key=key, value=value, description=description))


def _strategy_row(row: StrategyCatalog, ranking: dict | None = None) -> dict:
    return {
        "code": row.code,
        "name": row.name,
        "description": row.description or "",
        "version": row.version or "v1",
        "enabled": bool(row.enabled),
        "market_scope": row.market_scope or "ALL",
        "risk_level": row.risk_level or "medium",
        "params": row.params or {},
        "default_weight": float(row.default_weight or 1.0),
        "strategy_type": row.strategy_type or "builtin",
        "source_ref_type": row.source_ref_type or "",
        "source_ref_id": row.source_ref_id,
        "run_config": row.run_config or {},
        "auto_run_enabled": bool(row.auto_run_enabled),
        "ranking": ranking or {},
    }


def _normalize_strategy_update(payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "enabled" in payload:
        out["enabled"] = bool(payload.get("enabled"))
    if "name" in payload:
        name = str(payload.get("name") or "").strip()
        if name:
            out["name"] = name[:120]
    if "description" in payload:
        out["description"] = str(payload.get("description") or "").strip()[:1000]
    if "risk_level" in payload:
        risk = str(payload.get("risk_level") or "medium").strip()
        if risk not in RISK_LEVELS:
            raise ValueError("risk_level must be low/medium/high")
        out["risk_level"] = risk
    if "market_scope" in payload:
        market = str(payload.get("market_scope") or "ALL").strip().upper() or "ALL"
        if market not in {"ALL", "CN", "HK", "US"}:
            raise ValueError("market_scope must be ALL/CN/HK/US")
        out["market_scope"] = market
    if "default_weight" in payload:
        weight = float(payload.get("default_weight") or 1.0)
        out["default_weight"] = max(0.0, min(5.0, weight))
    if "run_config" in payload:
        cfg = payload.get("run_config")
        if not isinstance(cfg, dict):
            raise ValueError("run_config must be an object")
        out["run_config"] = cfg
    if "auto_run_enabled" in payload:
        out["auto_run_enabled"] = bool(payload.get("auto_run_enabled"))
    return out


def _paper_trade_stats(db: Session, since: datetime | None = None) -> dict[str, dict]:
    q = db.query(PaperTradingTrade)
    if since is not None:
        q = q.filter(PaperTradingTrade.closed_at >= since)
    rows = q.all()
    out: dict[str, dict] = {}
    for trade in rows:
        code = trade.strategy_code or "unknown"
        item = out.setdefault(
            code,
            {
                "paper_samples": 0,
                "paper_wins": 0,
                "paper_pnl": 0.0,
                "paper_return_sum": 0.0,
                "paper_returns": [],
                "paper_max_drawdown_pct": 0.0,
            },
        )
        pnl = float(trade.pnl or 0.0)
        ret = float(trade.pnl_pct or 0.0)
        item["paper_samples"] += 1
        item["paper_pnl"] += pnl
        item["paper_return_sum"] += ret
        item["paper_returns"].append(ret)
        if pnl > 0:
            item["paper_wins"] += 1

    for item in out.values():
        running = 0.0
        peak = 0.0
        max_dd = 0.0
        for ret in item.pop("paper_returns", []):
            running += ret
            if running > peak:
                peak = running
            max_dd = max(max_dd, peak - running)
        item["paper_max_drawdown_pct"] = round(max_dd, 2)
    return out


def _outcome_stats(db: Session) -> dict[str, dict]:
    rows = (
        db.query(
            StrategyOutcome.strategy_code,
            func.count(StrategyOutcome.id),
            func.sum(case_compatible_win()),
            func.avg(StrategyOutcome.outcome_return_pct),
        )
        .filter(StrategyOutcome.outcome_status.in_(("evaluated", "hit_target", "hit_stop")))
        .group_by(StrategyOutcome.strategy_code)
        .all()
    )
    out = {}
    for code, total, wins, avg_ret in rows:
        out[str(code)] = {
            "outcome_samples": int(total or 0),
            "outcome_wins": int(wins or 0),
            "outcome_avg_return_pct": float(avg_ret or 0.0),
        }
    return out


def _backtest_stats(db: Session) -> dict[str, dict]:
    rows = db.query(BacktestStrategyMetric).all()
    out: dict[str, dict] = {}
    for row in rows:
        code = row.strategy_code or "unknown"
        sample = int(row.sample_size or row.total_trades or 0)
        item = out.setdefault(
            code,
            {
                "backtest_samples": 0,
                "backtest_wins": 0,
                "backtest_pnl": 0.0,
                "backtest_return_weighted": 0.0,
                "backtest_max_drawdown_pct": 0.0,
                "backtest_recent_weighted": 0.0,
            },
        )
        item["backtest_samples"] += sample
        item["backtest_wins"] += int(row.winning_trades or 0)
        item["backtest_pnl"] += float(row.total_pnl or 0.0)
        item["backtest_return_weighted"] += float(row.avg_return_pct or 0.0) * sample
        item["backtest_recent_weighted"] += float(row.recent_30d_return_pct or 0.0) * sample
        item["backtest_max_drawdown_pct"] = max(
            float(item["backtest_max_drawdown_pct"]),
            float(row.max_drawdown_pct or 0.0),
        )
    for item in out.values():
        sample = int(item.get("backtest_samples", 0))
        item["backtest_avg_return_pct"] = (
            float(item.pop("backtest_return_weighted", 0.0)) / sample if sample else 0.0
        )
        item["backtest_recent_30d_return_pct"] = (
            float(item.pop("backtest_recent_weighted", 0.0)) / sample if sample else 0.0
        )
    return out


def case_compatible_win():
    from sqlalchemy import case

    return case((StrategyOutcome.outcome_return_pct > 0, 1), else_=0)


def calculate_strategy_ranking(db: Session) -> dict[str, dict]:
    paper_all = _paper_trade_stats(db)
    paper_recent = _paper_trade_stats(db, since=_now() - timedelta(days=30))
    outcomes = _outcome_stats(db)
    backtests = _backtest_stats(db)
    codes = {
        row.code
        for row in db.query(StrategyCatalog.code).all()
        if row.code
    } | set(paper_all.keys()) | set(outcomes.keys()) | set(backtests.keys())

    ranking: dict[str, dict] = {}
    for code in codes:
        p = paper_all.get(code, {})
        r = paper_recent.get(code, {})
        o = outcomes.get(code, {})
        b = backtests.get(code, {})
        paper_samples = int(p.get("paper_samples", 0))
        outcome_samples = int(o.get("outcome_samples", 0))
        backtest_samples = int(b.get("backtest_samples", 0))
        samples = paper_samples + outcome_samples + backtest_samples
        wins = (
            int(p.get("paper_wins", 0))
            + int(o.get("outcome_wins", 0))
            + int(b.get("backtest_wins", 0))
        )
        win_rate = (wins / samples * 100.0) if samples else 0.0
        paper_avg_ret = (
            float(p.get("paper_return_sum", 0.0)) / int(p.get("paper_samples", 1))
            if int(p.get("paper_samples", 0))
            else 0.0
        )
        weighted_ret = (
            paper_avg_ret * paper_samples
            + float(o.get("outcome_avg_return_pct", 0.0)) * outcome_samples
            + float(b.get("backtest_avg_return_pct", 0.0)) * backtest_samples
        )
        avg_return = weighted_ret / samples if samples else 0.0
        total_pnl = float(p.get("paper_pnl", 0.0)) + float(b.get("backtest_pnl", 0.0))
        max_dd = max(
            float(p.get("paper_max_drawdown_pct", 0.0)),
            float(b.get("backtest_max_drawdown_pct", 0.0)),
        )
        recent_avg = (
            float(r.get("paper_return_sum", 0.0)) / int(r.get("paper_samples", 1))
            if int(r.get("paper_samples", 0))
            else 0.0
        )
        if backtest_samples:
            recent_avg = (
                recent_avg * int(r.get("paper_samples", 0))
                + float(b.get("backtest_recent_30d_return_pct", 0.0)) * backtest_samples
            ) / (int(r.get("paper_samples", 0)) + backtest_samples)
        return_score = max(0.0, min(100.0, 50.0 + avg_return * 6.0))
        win_score = max(0.0, min(100.0, win_rate))
        dd_score = max(0.0, min(100.0, 100.0 - max_dd * 4.0))
        sample_score = max(0.0, min(100.0, samples / 20.0 * 100.0))
        recent_score = max(0.0, min(100.0, 50.0 + recent_avg * 8.0))
        score = (
            return_score * 0.35
            + win_score * 0.20
            + dd_score * 0.20
            + sample_score * 0.15
            + recent_score * 0.10
        )
        ranking[code] = {
            "strategy_code": code,
            "score": round(score, 2) if samples else 0.0,
            "sample_size": samples,
            "insufficient_samples": samples < 5,
            "status_label": "未验证" if samples == 0 else ("样本不足" if samples < 5 else "已验证"),
            "win_rate": round(win_rate, 2),
            "avg_return_pct": round(avg_return, 2),
            "total_pnl": round(total_pnl, 2),
            "max_drawdown_pct": round(max_dd, 2),
            "recent_30d_return_pct": round(recent_avg, 2),
        }
    return ranking


def list_strategy_pool(*, enabled_only: bool = False) -> dict:
    ensure_strategy_catalog()
    db = SessionLocal()
    try:
        ranking = calculate_strategy_ranking(db)
        q = db.query(StrategyCatalog)
        if enabled_only:
            q = q.filter(StrategyCatalog.enabled.is_(True))
        rows = q.order_by(StrategyCatalog.strategy_type.asc(), StrategyCatalog.code.asc()).all()
        return {"items": [_strategy_row(row, ranking.get(row.code)) for row in rows]}
    finally:
        db.close()


def update_strategy_pool_item(code: str, payload: dict[str, Any]) -> dict:
    ensure_strategy_catalog()
    db = SessionLocal()
    try:
        row = db.query(StrategyCatalog).filter(StrategyCatalog.code == code).first()
        if not row:
            raise KeyError("strategy not found")
        updates = _normalize_strategy_update(payload)
        for key, value in updates.items():
            setattr(row, key, value)
        db.commit()
        db.refresh(row)
        ranking = calculate_strategy_ranking(db)
        return _strategy_row(row, ranking.get(row.code))
    finally:
        db.close()


def register_screener_strategy(formula_id: int, *, run_config: dict | None = None) -> dict:
    ensure_strategy_catalog()
    db = SessionLocal()
    try:
        formula = db.query(StockScreenerFormula).filter(StockScreenerFormula.id == formula_id).first()
        if not formula:
            raise KeyError("formula not found")
        code = f"screener:{formula.id}"
        row = db.query(StrategyCatalog).filter(StrategyCatalog.code == code).first()
        if not row:
            row = StrategyCatalog(code=code, name=f"选股策略: {formula.name}")
            db.add(row)
        row.name = f"选股策略: {formula.name}"
        row.description = formula.description or "由选股公式生成的自定义策略"
        row.version = "screener-v1"
        row.enabled = True
        row.market_scope = "CN"
        row.risk_level = row.risk_level or "medium"
        row.params = {"horizon_days": 3}
        row.default_weight = float(row.default_weight or 1.0)
        row.strategy_type = "screener_formula"
        row.source_ref_type = "stock_screener_formula"
        row.source_ref_id = int(formula.id)
        merged = dict(row.run_config or {})
        merged.update(run_config or {})
        merged.setdefault("max_results", 20)
        merged.setdefault("position_pct", 0.05)
        row.run_config = merged
        db.commit()
        db.refresh(row)
        return _strategy_row(row, calculate_strategy_ranking(db).get(row.code))
    finally:
        db.close()


def get_strategy_ranking() -> dict:
    ensure_strategy_catalog()
    db = SessionLocal()
    try:
        ranking = calculate_strategy_ranking(db)
        pool = list_strategy_catalog(enabled_only=False)
        items = []
        for row in pool:
            stats = ranking.get(row["code"], {})
            items.append({**row, "ranking": stats})
        items.sort(key=lambda x: (x.get("ranking") or {}).get("score", 0), reverse=True)
        return {"items": items}
    finally:
        db.close()


def get_paper_strategy_selection(db: Session | None = None) -> dict:
    def _load(session: Session) -> dict:
        row = _setting(session, PAPER_STRATEGY_SELECTION_KEY)
        data = _json_loads(row.value if row else "", {})
        if not isinstance(data, dict):
            data = {}
        codes = data.get("strategy_codes")
        if not isinstance(codes, list):
            codes = []
        mode = str(data.get("mode") or "all").strip() or "all"
        top_n = int(data.get("top_n") or 5)
        return {
            "mode": mode if mode in {"all", "custom", "top_n"} else "all",
            "strategy_codes": [str(x) for x in codes if str(x).strip()],
            "top_n": max(1, min(top_n, 50)),
        }

    if db is not None:
        return _load(db)
    session = SessionLocal()
    try:
        return _load(session)
    finally:
        session.close()


def save_paper_strategy_selection(payload: dict[str, Any], db: Session | None = None) -> dict:
    def _save(session: Session) -> dict:
        mode = str(payload.get("mode") or "all").strip() or "all"
        if mode not in {"all", "custom", "top_n"}:
            mode = "all"
        codes = payload.get("strategy_codes") if isinstance(payload.get("strategy_codes"), list) else []
        data = {
            "mode": mode,
            "strategy_codes": [str(x) for x in codes if str(x).strip()],
            "top_n": max(1, min(int(payload.get("top_n") or 5), 50)),
        }
        _save_setting(
            session,
            PAPER_STRATEGY_SELECTION_KEY,
            json.dumps(data, ensure_ascii=False, sort_keys=True),
            "模拟盘启用策略池配置",
        )
        session.commit()
        return data

    if db is not None:
        return _save(db)
    session = SessionLocal()
    try:
        return _save(session)
    finally:
        session.close()


def resolve_enabled_strategy_codes_for_paper(db: Session) -> set[str] | None:
    """Return selected strategy codes. None means compatibility mode: all enabled strategies."""

    selection = get_paper_strategy_selection(db)
    mode = selection.get("mode")
    if mode == "all":
        return None
    if mode == "custom":
        return set(selection.get("strategy_codes") or [])
    ranking = calculate_strategy_ranking(db)
    top_n = int(selection.get("top_n") or 5)
    eligible = [
        (code, data)
        for code, data in ranking.items()
        if not data.get("insufficient_samples") and data.get("sample_size", 0) > 0
    ]
    eligible.sort(key=lambda x: x[1].get("score", 0), reverse=True)
    return {code for code, _ in eligible[:top_n]}
