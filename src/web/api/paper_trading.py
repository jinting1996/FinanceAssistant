"""模拟盘 API 端点。"""

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from zoneinfo import ZoneInfo

from src.config import Settings
from src.core.paper_trading_engine import (
    ALL_MARKETS,
    ENGINE,
    SKIP_STATS_KEY,
    compute_market_cash,
    market_allocations_or_default,
    normalize_allocations,
)
from src.core.strategy_pool import (
    get_paper_strategy_selection,
    list_strategy_pool,
    register_screener_strategy,
    save_paper_strategy_selection,
)
from src.core.trade_rules import get_trade_rules
from src.web.database import get_db
from src.web.models import (
    AppSettings,
    NotifyChannel,
    PaperTradingAccount,
    PaperTradingPosition,
    PaperTradingTrade,
    StockScreenerFormula,
    StockScreenerResult,
    StockScreenerRun,
    StrategySignalRun,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _format_dt(dt) -> str:
    if not dt:
        return ""
    tz_name = Settings().app_timezone or "UTC"
    try:
        tzinfo = ZoneInfo(tz_name)
    except Exception:
        tzinfo = timezone.utc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tzinfo).isoformat(timespec="seconds")


class ToggleBody(BaseModel):
    enabled: bool


class UpdateSettingsBody(BaseModel):
    excluded_markets: list[str] | None = None  # 兼容旧字段
    market_allocations: dict[str, float] | None = None  # {"CN":0.5,...}，合计 ≤ 1
    initial_capital: float | None = None  # 总资金（>0 时按差额增/减资）


class ScreenerStrategyBody(BaseModel):
    run_id: int | None = None
    formula_id: int | None = None
    max_results: int = Field(default=20, ge=1, le=100)
    min_change_pct: float | None = None
    trigger_scan: bool = True


class StrategySelectionBody(BaseModel):
    mode: str = "all"
    strategy_codes: list[str] = []
    top_n: int = 5


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _rule_float(rules: dict | None, path: str, default: float) -> float:
    node: Any = rules or {}
    try:
        for part in path.split("."):
            node = node[part]
        return float(node)
    except Exception:
        return float(default)


def _strategy_code_for_screener(run: StockScreenerRun) -> str:
    return f"screener:{run.formula_id or run.id}"


def _strategy_name_for_screener(run: StockScreenerRun) -> str:
    formula = run.formula
    if formula and formula.name:
        return f"选股策略: {formula.name}"
    return f"选股策略 #{run.formula_id or run.id}"


def _resolve_screener_run(db: Session, payload: ScreenerStrategyBody) -> StockScreenerRun:
    if payload.run_id:
        run = db.query(StockScreenerRun).filter(StockScreenerRun.id == payload.run_id).first()
        if not run:
            raise HTTPException(404, "选股运行记录不存在")
        return run

    if payload.formula_id:
        formula = (
            db.query(StockScreenerFormula)
            .filter(StockScreenerFormula.id == payload.formula_id)
            .first()
        )
        if not formula:
            raise HTTPException(404, "选股公式不存在")
        run = (
            db.query(StockScreenerRun)
            .filter(
                StockScreenerRun.formula_id == formula.id,
                StockScreenerRun.status == "success",
            )
            .order_by(StockScreenerRun.finished_at.desc(), StockScreenerRun.id.desc())
            .first()
        )
        if not run:
            raise HTTPException(400, "该公式还没有成功的选股结果，请先运行选股")
        return run

    raise HTTPException(400, "缺少 run_id 或 formula_id")


def _serialize_account_dict(
    acc: PaperTradingAccount,
    *,
    initial: float,
    cash: float,
    total_equity: float,
    total_pnl: float,
    unrealized: float,
    total_trades: int,
    winning_trades: int,
    max_dd: float,
    peak: float,
    market: str | None = None,
    ratio: float | None = None,
) -> dict:
    win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0.0
    out = {
        "id": acc.id,
        "initial_capital": round(initial, 2),
        "current_capital": round(cash, 2),
        "total_equity": round(total_equity, 2),
        "total_pnl": round(total_pnl, 2),
        "unrealized_pnl": round(unrealized, 2),
        "total_trades": total_trades,
        "winning_trades": winning_trades,
        "win_rate": round(win_rate, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "peak_capital": round(peak, 2),
        "enabled": acc.enabled,
        "excluded_markets": acc.excluded_markets or [],
        "market_allocations": market_allocations_or_default(acc),
        "created_at": _format_dt(acc.created_at),
        "updated_at": _format_dt(acc.updated_at),
    }
    if market:
        out["market"] = market
        out["allocation_ratio"] = round(ratio or 0.0, 6)
    return out


def _build_equity_curve(
    db: Session, acc: PaperTradingAccount, market: str | None
) -> tuple[list[dict], float, float]:
    """构建收益曲线，返回 (curve, peak, max_drawdown_pct)。market=None 为全市场。"""
    ratio = market_allocations_or_default(acc).get(market, 0.0) if market else 1.0
    base = acc.initial_capital * ratio if market else acc.initial_capital

    tq = db.query(PaperTradingTrade).order_by(PaperTradingTrade.closed_at.asc())
    if market:
        tq = tq.filter(PaperTradingTrade.stock_market == market)
    trades = tq.all()

    pq = db.query(PaperTradingPosition).filter(PaperTradingPosition.status == "open")
    if market:
        pq = pq.filter(PaperTradingPosition.stock_market == market)
    open_positions = pq.all()

    by_date: dict[str, float] = {}
    for t in trades:
        if not t.closed_at:
            continue
        dt = t.closed_at
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        date_str = dt.strftime("%Y-%m-%d")
        by_date.setdefault(date_str, 0.0)
        by_date[date_str] += t.pnl

    curve: list[dict] = []
    running = base
    for date_str in sorted(by_date.keys()):
        running += by_date[date_str]
        curve.append({"date": date_str, "equity": round(running, 2)})

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    positions_value = sum(
        (p.current_price or p.entry_price) * p.quantity for p in open_positions
    )
    if market:
        realized = sum(t.pnl for t in trades)
        open_cost = sum(p.entry_price * p.quantity for p in open_positions)
        cash_now = compute_market_cash(acc.initial_capital, ratio, realized, open_cost)
    else:
        cash_now = acc.current_capital
    total_equity_now = cash_now + positions_value
    if curve and curve[-1]["date"] == today_str:
        curve[-1]["equity"] = round(total_equity_now, 2)
    else:
        curve.append({"date": today_str, "equity": round(total_equity_now, 2)})

    if len(curve) == 1:
        created = acc.created_at
        if created:
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            start_str = created.strftime("%Y-%m-%d")
            if start_str != today_str:
                curve.insert(0, {"date": start_str, "equity": round(base, 2)})
            else:
                curve.insert(0, {"date": today_str + " 00:00", "equity": round(base, 2)})

    peak = base
    max_dd = 0.0
    for pt in curve:
        eq = pt["equity"]
        if eq > peak:
            peak = eq
        if peak > 0:
            dd = (peak - eq) / peak * 100
            if dd > max_dd:
                max_dd = dd
    return curve, peak, max_dd


def _build_strategy_pnl_curve(
    db: Session, strategy_code: str, market: str | None
) -> list[dict]:
    """按策略构建累计已实现盈亏曲线(从 0 起,按平仓日累加)。

    单策略没有独立的资金分配口径,因此用累计盈亏(而非账户净值)表达,语义清晰。
    """
    tq = (
        db.query(PaperTradingTrade)
        .filter(PaperTradingTrade.strategy_code == strategy_code)
        .order_by(PaperTradingTrade.closed_at.asc())
    )
    if market:
        tq = tq.filter(PaperTradingTrade.stock_market == market)
    by_date: dict[str, float] = {}
    for t in tq.all():
        if not t.closed_at:
            continue
        dt = t.closed_at
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        date_str = dt.strftime("%Y-%m-%d")
        by_date[date_str] = by_date.get(date_str, 0.0) + (t.pnl or 0.0)

    curve: list[dict] = []
    running = 0.0
    for date_str in sorted(by_date.keys()):
        running += by_date[date_str]
        curve.append({"date": date_str, "equity": round(running, 2)})
    return curve


def _account_summary(db: Session, acc: PaperTradingAccount, market: str | None) -> dict:
    """账户汇总。market=None 为全账户（沿用引擎维护的回撤）；否则按该市场子池口径。"""
    if not market or market not in ALL_MARKETS:
        open_positions = (
            db.query(PaperTradingPosition)
            .filter(PaperTradingPosition.status == "open")
            .all()
        )
        unrealized = sum(p.unrealized_pnl or 0 for p in open_positions)
        positions_value = sum(
            (p.current_price or p.entry_price) * p.quantity for p in open_positions
        )
        return _serialize_account_dict(
            acc,
            initial=acc.initial_capital,
            cash=acc.current_capital,
            total_equity=acc.current_capital + positions_value,
            total_pnl=acc.total_pnl,
            unrealized=unrealized,
            total_trades=acc.total_trades,
            winning_trades=acc.winning_trades,
            max_dd=acc.max_drawdown_pct,
            peak=acc.peak_capital,
        )

    alloc = market_allocations_or_default(acc)
    ratio = alloc.get(market, 0.0)
    open_positions = (
        db.query(PaperTradingPosition)
        .filter(
            PaperTradingPosition.status == "open",
            PaperTradingPosition.stock_market == market,
        )
        .all()
    )
    trades = (
        db.query(PaperTradingTrade)
        .filter(PaperTradingTrade.stock_market == market)
        .all()
    )
    realized = sum(t.pnl for t in trades)
    open_cost = sum(p.entry_price * p.quantity for p in open_positions)
    cash = compute_market_cash(acc.initial_capital, ratio, realized, open_cost)
    positions_value = sum(
        (p.current_price or p.entry_price) * p.quantity for p in open_positions
    )
    winning = sum(1 for t in trades if t.pnl > 0)
    _, peak, max_dd = _build_equity_curve(db, acc, market)
    unrealized = sum(p.unrealized_pnl or 0 for p in open_positions)
    return _serialize_account_dict(
        acc,
        initial=acc.initial_capital * ratio,
        cash=cash,
        total_equity=cash + positions_value,
        total_pnl=realized,
        unrealized=unrealized,
        total_trades=len(trades),
        winning_trades=winning,
        max_dd=max_dd,
        peak=peak,
        market=market,
        ratio=ratio,
    )


def _strategy_performance(db: Session, market: str | None) -> list[dict]:
    """按策略聚合绩效（已平仓 + 持仓中），可按市场过滤。"""
    skip_stats = _load_skip_stats(db)
    skipped_by_strategy = skip_stats.get("by_strategy", {}) if isinstance(skip_stats, dict) else {}
    tq = db.query(PaperTradingTrade)
    if market:
        tq = tq.filter(PaperTradingTrade.stock_market == market)
    all_trades = tq.all()

    pq = db.query(PaperTradingPosition).filter(PaperTradingPosition.status == "open")
    if market:
        pq = pq.filter(PaperTradingPosition.stock_market == market)
    open_positions = pq.all()

    strategy_stats: dict[str, dict] = {}
    for t in all_trades:
        code = t.strategy_code or "unknown"
        s = strategy_stats.setdefault(code, {
            "strategy_code": code,
            "total_trades": 0, "winning_trades": 0,
            "total_pnl": 0.0, "total_pnl_pct_sum": 0.0,
            "holding_days_sum": 0,
            "open_positions": 0, "unrealized_pnl": 0.0,
            "exit_reason_counts": {},
        })
        s["total_trades"] += 1
        s["total_pnl"] += t.pnl
        s["total_pnl_pct_sum"] += t.pnl_pct
        s["holding_days_sum"] += t.holding_days or 0
        reason = t.exit_reason or "unknown"
        s["exit_reason_counts"][reason] = s["exit_reason_counts"].get(reason, 0) + 1
        if t.pnl > 0:
            s["winning_trades"] += 1

    for p in open_positions:
        code = p.strategy_code or "unknown"
        s = strategy_stats.setdefault(code, {
            "strategy_code": code,
            "total_trades": 0, "winning_trades": 0,
            "total_pnl": 0.0, "total_pnl_pct_sum": 0.0,
            "holding_days_sum": 0,
            "open_positions": 0, "unrealized_pnl": 0.0,
            "exit_reason_counts": {},
        })
        s["open_positions"] += 1
        s["unrealized_pnl"] += p.unrealized_pnl or 0

    strategy_perf = []
    for s in strategy_stats.values():
        n = s["total_trades"]
        strategy_perf.append({
            "strategy_code": s["strategy_code"],
            "total_trades": n,
            "winning_trades": s["winning_trades"],
            "win_rate": round(s["winning_trades"] / n * 100, 1) if n > 0 else 0,
            "total_pnl": round(s["total_pnl"], 2),
            "avg_pnl_pct": round(s["total_pnl_pct_sum"] / n, 2) if n > 0 else 0,
            "avg_holding_days": round(s["holding_days_sum"] / n, 1) if n > 0 else 0,
            "open_positions": s["open_positions"],
            "unrealized_pnl": round(s["unrealized_pnl"], 2),
            "skipped_count": int(skipped_by_strategy.get(s["strategy_code"], 0) or 0),
            "exit_reason_counts": dict(sorted(
                s["exit_reason_counts"].items(),
                key=lambda item: item[1],
                reverse=True,
            )),
        })
    strategy_perf.sort(key=lambda x: x["total_pnl"] + x["unrealized_pnl"], reverse=True)
    return strategy_perf


def _load_skip_stats(db: Session) -> dict:
    row = db.query(AppSettings).filter(AppSettings.key == SKIP_STATS_KEY).first()
    fallback = {"total": 0, "by_reason": {}, "by_strategy": {}, "samples": []}
    if not row or not row.value:
        return fallback
    try:
        import json

        data = json.loads(row.value)
        return data if isinstance(data, dict) else fallback
    except Exception:
        return fallback


def _position_response(p: PaperTradingPosition) -> dict:
    holding_days = 0
    if p.opened_at:
        from datetime import datetime, timezone as tz
        now = datetime.now(tz.utc)
        opened = p.opened_at
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=tz.utc)
        holding_days = max(0, (now - opened).days)
    return {
        "id": p.id,
        "stock_symbol": p.stock_symbol,
        "stock_market": p.stock_market,
        "stock_name": p.stock_name or "",
        "quantity": p.quantity,
        "entry_price": p.entry_price,
        "stop_loss": p.stop_loss,
        "target_price": p.target_price,
        "current_price": p.current_price,
        "unrealized_pnl": round(p.unrealized_pnl or 0, 2),
        "unrealized_pnl_pct": round(
            ((p.current_price - p.entry_price) / p.entry_price * 100)
            if p.current_price and p.entry_price > 0 else 0, 2
        ),
        "status": p.status,
        "signal_run_id": p.signal_run_id,
        "signal_snapshot_date": p.signal_snapshot_date or "",
        "signal_action": p.signal_action or "",
        "strategy_code": p.strategy_code or "",
        "holding_days": holding_days,
        "opened_at": _format_dt(p.opened_at),
        "closed_at": _format_dt(p.closed_at),
        "updated_at": _format_dt(p.updated_at),
    }


def _trade_response(t: PaperTradingTrade) -> dict:
    return {
        "id": t.id,
        "stock_symbol": t.stock_symbol,
        "stock_market": t.stock_market,
        "stock_name": t.stock_name or "",
        "quantity": t.quantity,
        "entry_price": t.entry_price,
        "exit_price": t.exit_price,
        "pnl": round(t.pnl, 2),
        "pnl_pct": round(t.pnl_pct, 2),
        "exit_reason": t.exit_reason,
        "signal_run_id": t.signal_run_id,
        "signal_snapshot_date": t.signal_snapshot_date or "",
        "strategy_code": t.strategy_code or "",
        "holding_days": t.holding_days or 0,
        "opened_at": _format_dt(t.opened_at),
        "closed_at": _format_dt(t.closed_at),
    }


@router.get("/account")
def get_account(market: str | None = None, db: Session = Depends(get_db)):
    acc = db.query(PaperTradingAccount).first()
    if not acc:
        acc = PaperTradingAccount(
            initial_capital=1000000.0,
            current_capital=1000000.0,
            peak_capital=1000000.0,
        )
        db.add(acc)
        db.commit()
        db.refresh(acc)
    return _account_summary(db, acc, market if market in ALL_MARKETS else None)


@router.get("/positions")
def list_positions(status: str = "open", market: str | None = None, db: Session = Depends(get_db)):
    query = db.query(PaperTradingPosition)
    if status != "all":
        query = query.filter(PaperTradingPosition.status == status)
    if market in ALL_MARKETS:
        query = query.filter(PaperTradingPosition.stock_market == market)
    rows = query.order_by(PaperTradingPosition.opened_at.desc()).all()
    return [_position_response(p) for p in rows]


@router.get("/trades")
def list_trades(limit: int = 50, offset: int = 0, market: str | None = None, db: Session = Depends(get_db)):
    base = db.query(PaperTradingTrade)
    if market in ALL_MARKETS:
        base = base.filter(PaperTradingTrade.stock_market == market)
    total = base.count()
    rows = (
        base.order_by(PaperTradingTrade.closed_at.desc())
        .offset(max(0, offset))
        .limit(max(1, min(limit, 200)))
        .all()
    )
    return {
        "total": total,
        "items": [_trade_response(t) for t in rows],
    }


@router.get("/metrics")
def get_metrics(
    market: str | None = None,
    strategy_code: str | None = None,
    db: Session = Depends(get_db),
):
    acc = db.query(PaperTradingAccount).first()
    if not acc:
        return {"account": None, "equity_curve": [], "open_positions": 0, "strategy_performance": [], "skip_stats": _load_skip_stats(db)}

    mkt = market if market in ALL_MARKETS else None

    pq = db.query(PaperTradingPosition).filter(PaperTradingPosition.status == "open")
    if mkt:
        pq = pq.filter(PaperTradingPosition.stock_market == mkt)
    open_count = pq.count()

    # 指定策略时返回该策略累计盈亏曲线;否则返回账户净值曲线
    if strategy_code:
        equity_curve = _build_strategy_pnl_curve(db, strategy_code, mkt)
    else:
        equity_curve, _peak, _max_dd = _build_equity_curve(db, acc, mkt)

    return {
        "account": _account_summary(db, acc, mkt),
        "equity_curve": equity_curve,
        "equity_curve_mode": "strategy" if strategy_code else "account",
        "open_positions": open_count,
        "strategy_performance": _strategy_performance(db, mkt),
        "skip_stats": _load_skip_stats(db),
    }


@router.post("/account/toggle")
def toggle_account(body: ToggleBody, db: Session = Depends(get_db)):
    acc = db.query(PaperTradingAccount).first()
    if not acc:
        raise HTTPException(404, "模拟盘账户不存在")
    acc.enabled = body.enabled
    db.commit()
    db.refresh(acc)
    return _account_summary(db, acc, None)


@router.post("/account/reset")
def reset_account():
    result = ENGINE.reset_account()
    if not result.get("ok"):
        raise HTTPException(500, "重置失败")
    return {"ok": True}


@router.post("/positions/{position_id}/close")
async def close_position(position_id: int):
    result = await ENGINE.close_position_manual_async(position_id)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "平仓失败"))
    return {"ok": True}


@router.post("/account/settings")
def update_settings(body: UpdateSettingsBody, db: Session = Depends(get_db)):
    acc = db.query(PaperTradingAccount).first()
    if not acc:
        raise HTTPException(404, "模拟盘账户不存在")

    if body.market_allocations is not None:
        alloc = normalize_allocations(body.market_allocations)
        total = sum(alloc.values())
        if total > 1.0 + 1e-9:
            raise HTTPException(400, f"投资比例合计不能超过 100%（当前 {round(total * 100)}%）")
        acc.market_allocations = alloc
        # 同步派生 excluded_markets（比例 0 即排除），兼容旧读取
        acc.excluded_markets = [m for m in ALL_MARKETS if alloc.get(m, 0.0) <= 0]
    elif body.excluded_markets is not None:
        valid = {"CN", "HK", "US"}
        acc.excluded_markets = [m for m in body.excluded_markets if m in valid]

    if body.initial_capital is not None and body.initial_capital > 0:
        delta = body.initial_capital - (acc.initial_capital or 0.0)
        acc.initial_capital = body.initial_capital
        acc.current_capital = (acc.current_capital or 0.0) + delta
        if acc.peak_capital is None or acc.current_capital > acc.peak_capital:
            acc.peak_capital = acc.current_capital

    db.commit()
    db.refresh(acc)
    return _account_summary(db, acc, None)


@router.get("/strategy-selection")
def get_strategy_selection(db: Session = Depends(get_db)):
    return {
        "selection": get_paper_strategy_selection(db),
        "strategy_pool": list_strategy_pool(enabled_only=True).get("items", []),
    }


@router.post("/strategy-selection")
def update_strategy_selection(body: StrategySelectionBody, db: Session = Depends(get_db)):
    selection = save_paper_strategy_selection(body.model_dump(), db)
    return {
        "selection": selection,
        "strategy_pool": list_strategy_pool(enabled_only=True).get("items", []),
    }


@router.post("/scan")
async def manual_scan():
    """手动触发一次模拟盘扫描（建仓 + 平仓检查）。"""
    result = await ENGINE.scan_once()
    return result


@router.post("/screener-strategy")
async def create_signals_from_screener_strategy(
    payload: ScreenerStrategyBody,
    db: Session = Depends(get_db),
):
    """把一次选股运行结果转成模拟盘可执行的自定义策略信号。"""
    run = _resolve_screener_run(db, payload)
    if run.status != "success":
        raise HTTPException(400, "只能使用成功完成的选股结果生成模拟盘信号")

    query = (
        db.query(StockScreenerResult)
        .filter(StockScreenerResult.run_id == run.id, StockScreenerResult.matched == True)
    )
    if payload.min_change_pct is not None:
        query = query.filter(StockScreenerResult.change_pct >= float(payload.min_change_pct))
    results = (
        query.order_by(
            StockScreenerResult.change_pct.desc(),
            StockScreenerResult.id.asc(),
        )
        .limit(payload.max_results)
        .all()
    )
    if not results:
        raise HTTPException(400, "本次选股没有可用于模拟盘的命中结果")

    rules = get_trade_rules(db)
    entry_band_pct = _rule_float(rules, "risk.entry_band_pct", 0.01)
    stop_loss_pct = _rule_float(rules, "risk.paper_fallback_stop_loss_pct", 0.08)
    target_profit_pct = _rule_float(rules, "risk.paper_fallback_target_profit_pct", 0.15)
    snapshot = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if run.formula_id:
        strategy_item = register_screener_strategy(
            int(run.formula_id),
            run_config={"max_results": payload.max_results},
        )
        strategy_code = strategy_item["code"]
        strategy_name = strategy_item["name"]
    else:
        strategy_code = _strategy_code_for_screener(run)
        strategy_name = _strategy_name_for_screener(run)
    created = 0
    updated = 0
    skipped = 0

    for idx, item in enumerate(results):
        price = _safe_float(item.last_close)
        if price is None or price <= 0:
            skipped += 1
            continue
        change = _safe_float(item.change_pct) or 0.0
        score = max(45.0, min(95.0, 72.0 + change))
        rank_score = score + max(0.0, (len(results) - idx) * 0.01)
        indicators = item.indicators if isinstance(item.indicators, dict) else {}
        evidence = [
            f"选股公式命中: {strategy_name}",
            f"最近收盘价 {price:.2f}",
        ]
        if item.board_name:
            evidence.append(f"来源板块: {item.board_name}")

        row = (
            db.query(StrategySignalRun)
            .filter(
                StrategySignalRun.snapshot_date == snapshot,
                StrategySignalRun.stock_symbol == item.symbol,
                StrategySignalRun.stock_market == item.market,
                StrategySignalRun.strategy_code == strategy_code,
                StrategySignalRun.source_pool == "screener",
            )
            .first()
        )
        if row:
            updated += 1
        else:
            row = StrategySignalRun(
                snapshot_date=snapshot,
                stock_symbol=item.symbol,
                stock_market=item.market,
                strategy_code=strategy_code,
                source_candidate_id=None,
            )
            db.add(row)
            created += 1

        row.stock_name = item.name or item.symbol
        row.strategy_name = strategy_name
        row.strategy_version = "screener-v1"
        row.risk_level = "medium"
        row.source_pool = "screener"
        row.score = score
        row.rank_score = rank_score
        row.confidence = round(score / 100.0, 3)
        row.status = "active"
        row.action = "buy"
        row.action_label = "自定义策略建仓"
        row.signal = "选股公式命中"
        row.reason = item.reason or "Formula matched on the latest trading day"
        row.evidence = evidence
        row.holding_days = 3
        row.entry_low = round(price * (1 - entry_band_pct), 4)
        row.entry_high = round(price * (1 + entry_band_pct), 4)
        row.stop_loss = round(price * (1 - stop_loss_pct), 4)
        row.target_price = round(price * (1 + target_profit_pct), 4)
        row.invalidation = "选股条件失效或触发模拟盘止损/止盈"
        row.plan_quality = 100
        row.source_agent = "screener"
        row.source_suggestion_id = None
        row.trace_id = f"screener-run:{run.id}"
        row.is_holding_snapshot = False
        row.context_quality_score = None
        row.payload = {
            "source": "screener_strategy",
            "screener_run_id": run.id,
            "screener_formula_id": run.formula_id,
            "formula_snapshot": run.formula_snapshot,
            "board_code": item.board_code or "",
            "board_name": item.board_name or "",
            "indicators": indicators,
            "change_pct": item.change_pct,
        }
        row.updated_at = datetime.now(timezone.utc)

    db.commit()
    scan_result = None
    if payload.trigger_scan and (created or updated):
        scan_result = await ENGINE.scan_once()

    return {
        "ok": True,
        "run_id": run.id,
        "strategy_code": strategy_code,
        "strategy_name": strategy_name,
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "scan": scan_result,
    }


# ---------------------------------------------------------------------------
# 跟单通知设置
# ---------------------------------------------------------------------------

_NOTIFY_KEYS = [
    "pt_notify_enabled",
    "pt_notify_channel_ids",
    "pt_notify_realtime",
    "pt_notify_premarket",
    "pt_notify_summary",
]

_NOTIFY_DEFAULTS = {
    "pt_notify_enabled": "false",
    "pt_notify_channel_ids": "",
    "pt_notify_realtime": "true",
    "pt_notify_premarket": "true",
    "pt_notify_summary": "true",
}


@router.get("/notify-settings")
def get_notify_settings(db: Session = Depends(get_db)):
    """返回当前通知配置 + 可用渠道列表。"""
    rows = db.query(AppSettings).filter(AppSettings.key.in_(_NOTIFY_KEYS)).all()
    settings = dict(_NOTIFY_DEFAULTS)
    for r in rows:
        settings[r.key] = r.value or _NOTIFY_DEFAULTS.get(r.key, "")

    channels = db.query(NotifyChannel).filter(NotifyChannel.enabled.is_(True)).all()
    channel_list = [
        {"id": ch.id, "name": ch.name, "type": ch.type, "is_default": ch.is_default}
        for ch in channels
    ]

    return {"settings": settings, "channels": channel_list}


class NotifySettingsBody(BaseModel):
    pt_notify_enabled: str | None = None
    pt_notify_channel_ids: str | None = None
    pt_notify_realtime: str | None = None
    pt_notify_premarket: str | None = None
    pt_notify_summary: str | None = None


@router.post("/notify-settings")
def update_notify_settings(body: NotifySettingsBody, db: Session = Depends(get_db)):
    """更新通知配置。"""
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    for key, value in updates.items():
        row = db.query(AppSettings).filter(AppSettings.key == key).first()
        if row:
            row.value = value
        else:
            db.add(AppSettings(key=key, value=value, description=f"模拟盘通知配置: {key}"))
    db.commit()
    return get_notify_settings(db)


@router.post("/notify-test")
async def test_notify():
    """发送测试通知。"""
    from src.core.paper_trading_notifier import send_test_notification
    result = await send_test_notification()
    if not result.get("success"):
        raise HTTPException(400, result.get("error", "发送失败"))
    return {"ok": True}


@router.post("/premarket-plan")
async def trigger_premarket_plan():
    """手动触发盘前计划通知。"""
    from src.core.paper_trading_notifier import send_premarket_plan
    await send_premarket_plan()
    return {"ok": True}


@router.post("/daily-summary")
async def trigger_daily_summary():
    """手动触发日终摘要通知。"""
    from src.core.paper_trading_notifier import send_daily_summary
    await send_daily_summary()
    return {"ok": True}
