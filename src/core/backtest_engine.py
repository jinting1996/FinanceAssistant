"""Backtest engine for strategy-pool historical validation."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from math import isfinite
from typing import Any

from sqlalchemy.orm import Session

from src.collectors.kline_collector import KlineData
from src.core.providers import ProviderRequest, get_kline_orchestrator
from src.core.screener.formula import FormulaEvaluator, parse_formula
from src.core.screener.providers import ScreenerStock, get_screener_provider, normalize_universe_config
from src.core.strategy_catalog import ensure_strategy_catalog
from src.core.trade_rules import get_trade_rules
from src.web.database import SessionLocal
from src.web.models import (
    BacktestDailyEquity,
    BacktestRun,
    BacktestStrategyMetric,
    BacktestTrade,
    StockScreenerFormula,
    StrategyCatalog,
    StrategySignalRun,
)

logger = logging.getLogger(__name__)


@dataclass
class BacktestSignal:
    strategy_code: str
    strategy_name: str
    strategy_type: str
    source_ref_id: int | None
    signal_run_id: int | None
    stock_symbol: str
    stock_market: str
    stock_name: str
    signal_date: str
    entry_low: float | None
    entry_high: float | None
    stop_loss: float | None
    target_price: float | None
    holding_days: int
    rank_score: float = 0.0
    meta: dict[str, Any] | None = None


@dataclass
class ExecutedTrade:
    strategy_code: str
    stock_key: str
    entry_date: str
    exit_date: str
    quantity: int
    entry_price: float
    exit_price: float
    pnl: float
    pnl_pct: float
    fees: float
    entry_cost: float
    proceeds: float
    exit_reason: str
    holding_days: int
    row: BacktestTrade


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_day(value: str, fallback: datetime | None = None) -> datetime:
    try:
        return datetime.strptime(str(value or "")[:10], "%Y-%m-%d")
    except Exception:
        return fallback or datetime.now()


def _dt(day: str) -> datetime | None:
    try:
        return datetime.strptime(day[:10], "%Y-%m-%d")
    except Exception:
        return None


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
        return out if isfinite(out) else default
    except Exception:
        return default


def _optional_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
        return out if isfinite(out) else None
    except Exception:
        return None


def _config_float(config: dict, key: str, default: float) -> float:
    value = _safe_float(config.get(key), default)
    return max(0.0, value)


def _config_int(config: dict, key: str, default: int) -> int:
    try:
        return max(1, int(config.get(key) or default))
    except Exception:
        return default


def _rule_float(rules: dict, path: str, default: float) -> float:
    cur: Any = rules
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return _safe_float(cur, default)


def _sort_klines(klines: list[KlineData]) -> list[KlineData]:
    return sorted([k for k in klines if getattr(k, "date", "")], key=lambda k: k.date)


def fetch_klines_for_backtest(symbol: str, market: str, *, days: int) -> list[KlineData]:
    """Fetch daily K lines for backtests.

    Tests monkeypatch this function so the engine can be exercised without
    hitting external market data providers.
    """

    resp = get_kline_orchestrator().fetch_sync(
        ProviderRequest(
            symbols=(symbol,),
            market=(market or "CN").upper(),
            extra=(("days", int(days)), ("interval", "1d")),
        ),
        cache_ttl_sec=60,
    )
    return _sort_klines(resp.data if resp.success and resp.data else [])


def _load_klines(symbol: str, market: str, start_date: str, end_date: str, *, warmup_days: int = 180) -> list[KlineData]:
    start = _parse_day(start_date)
    end = _parse_day(end_date)
    days = max(30, (end - start).days + warmup_days + 10)
    klines = fetch_klines_for_backtest(symbol, market, days=days)
    min_day = (start - timedelta(days=warmup_days)).strftime("%Y-%m-%d")
    return _sort_klines([k for k in klines if min_day <= str(k.date)[:10] <= end_date])


def _bar_index(klines: list[KlineData]) -> dict[str, int]:
    return {str(k.date)[:10]: idx for idx, k in enumerate(klines)}


def _next_bar_after(klines: list[KlineData], signal_date: str) -> tuple[int, KlineData] | None:
    for idx, bar in enumerate(klines):
        if str(bar.date)[:10] > signal_date:
            return idx, bar
    return None


def _run_config(strategy: StrategyCatalog) -> dict:
    return strategy.run_config if isinstance(strategy.run_config, dict) else {}


def _strategy_market_ok(strategy: StrategyCatalog, market: str) -> bool:
    scope = str(strategy.market_scope or "ALL").upper()
    return scope in {"", "ALL"} or scope == market.upper()


def _signal_from_row(row: StrategySignalRun, strategy: StrategyCatalog) -> BacktestSignal:
    cfg = _run_config(strategy)
    return BacktestSignal(
        strategy_code=row.strategy_code,
        strategy_name=row.strategy_name or strategy.name,
        strategy_type=strategy.strategy_type or "builtin",
        source_ref_id=strategy.source_ref_id,
        signal_run_id=row.id,
        stock_symbol=row.stock_symbol,
        stock_market=row.stock_market or "CN",
        stock_name=row.stock_name or "",
        signal_date=row.snapshot_date,
        entry_low=_optional_float(row.entry_low),
        entry_high=_optional_float(row.entry_high),
        stop_loss=_optional_float(row.stop_loss),
        target_price=_optional_float(row.target_price),
        holding_days=max(1, int(row.holding_days or cfg.get("max_holding_days") or cfg.get("holding_days") or 3)),
        rank_score=float(row.rank_score or row.score or 0.0),
        meta={"source": "strategy_signal_run", "action": row.action, "status": row.status},
    )


def _historical_signals(db: Session, strategies: dict[str, StrategyCatalog], run: BacktestRun) -> list[BacktestSignal]:
    codes = [code for code, s in strategies.items() if (s.strategy_type or "builtin") in {"builtin", "agent", "mcp"}]
    if not codes:
        return []
    q = (
        db.query(StrategySignalRun)
        .filter(
            StrategySignalRun.strategy_code.in_(codes),
            StrategySignalRun.snapshot_date >= run.start_date,
            StrategySignalRun.snapshot_date <= run.end_date,
            StrategySignalRun.status == "active",
            StrategySignalRun.action.in_(("buy", "add")),
        )
        .order_by(StrategySignalRun.snapshot_date.asc(), StrategySignalRun.rank_score.desc())
    )
    market = str(run.market or "CN").upper()
    if market != "ALL":
        q = q.filter(StrategySignalRun.stock_market == market)
    out: list[BacktestSignal] = []
    for row in q.all():
        strategy = strategies.get(row.strategy_code)
        if strategy:
            out.append(_signal_from_row(row, strategy))
    return out


def _screener_formula_signals(db: Session, strategies: dict[str, StrategyCatalog], run: BacktestRun, rules: dict) -> tuple[list[BacktestSignal], list[BacktestSignal]]:
    out: list[BacktestSignal] = []
    skipped: list[BacktestSignal] = []
    market = str(run.market or "CN").upper()
    for strategy in strategies.values():
        if (strategy.strategy_type or "") != "screener_formula":
            continue
        if market != "CN":
            skipped.append(
                BacktestSignal(
                    strategy_code=strategy.code,
                    strategy_name=strategy.name,
                    strategy_type=strategy.strategy_type or "screener_formula",
                    source_ref_id=strategy.source_ref_id,
                    signal_run_id=None,
                    stock_symbol="",
                    stock_market=market,
                    stock_name="",
                    signal_date=run.start_date,
                    entry_low=None,
                    entry_high=None,
                    stop_loss=None,
                    target_price=None,
                    holding_days=0,
                    meta={"skip_reason": "non_cn_formula"},
                )
            )
            continue
        formula = (
            db.query(StockScreenerFormula)
            .filter(StockScreenerFormula.id == strategy.source_ref_id)
            .first()
        )
        if not formula:
            skipped.append(
                BacktestSignal(
                    strategy_code=strategy.code,
                    strategy_name=strategy.name,
                    strategy_type=strategy.strategy_type or "screener_formula",
                    source_ref_id=strategy.source_ref_id,
                    signal_run_id=None,
                    stock_symbol="",
                    stock_market=market,
                    stock_name="",
                    signal_date=run.start_date,
                    entry_low=None,
                    entry_high=None,
                    stop_loss=None,
                    target_price=None,
                    holding_days=0,
                    meta={"skip_reason": "formula_missing"},
                )
            )
            continue
        cfg = normalize_universe_config(formula.universe_config)
        provider = get_screener_provider(cfg.get("provider"))
        limit = min(int((strategy.run_config or {}).get("max_results") or cfg["max_symbols"]), cfg["max_symbols"])
        try:
            universe = provider.resolve_universe(db, cfg, limit=limit)
        except Exception as e:
            skipped.append(
                BacktestSignal(
                    strategy_code=strategy.code,
                    strategy_name=strategy.name,
                    strategy_type=strategy.strategy_type or "screener_formula",
                    source_ref_id=strategy.source_ref_id,
                    signal_run_id=None,
                    stock_symbol="",
                    stock_market="CN",
                    stock_name="",
                    signal_date=run.start_date,
                    entry_low=None,
                    entry_high=None,
                    stop_loss=None,
                    target_price=None,
                    holding_days=0,
                    meta={"skip_reason": "universe_failed", "error": str(e)},
                )
            )
            continue
        try:
            program = parse_formula(formula.formula)
        except Exception as e:
            skipped.append(
                BacktestSignal(
                    strategy_code=strategy.code,
                    strategy_name=strategy.name,
                    strategy_type=strategy.strategy_type or "screener_formula",
                    source_ref_id=strategy.source_ref_id,
                    signal_run_id=None,
                    stock_symbol="",
                    stock_market="CN",
                    stock_name="",
                    signal_date=run.start_date,
                    entry_low=None,
                    entry_high=None,
                    stop_loss=None,
                    target_price=None,
                    holding_days=0,
                    meta={"skip_reason": "formula_parse_failed", "error": str(e)},
                )
            )
            continue
        band = _rule_float(rules, "risk.entry_band_pct", 0.01)
        stop_pct = _rule_float(rules, "risk.buy_fallback_stop_price_pct", 0.95)
        target_pct = _rule_float(rules, "risk.buy_fallback_target_price_pct", 1.06)
        holding_days = _config_int(strategy.run_config or {}, "max_holding_days", int((strategy.params or {}).get("horizon_days") or 3))
        for stock in universe:
            if not isinstance(stock, ScreenerStock):
                continue
            klines = _load_klines(stock.symbol, stock.market or "CN", run.start_date, run.end_date, warmup_days=int(cfg["days"]))
            if len(klines) < 2:
                continue
            try:
                result = FormulaEvaluator(klines).run(program)
            except Exception as e:
                logger.debug("formula backtest failed for %s %s: %s", strategy.code, stock.symbol, e)
                continue
            series = result.get("series") if isinstance(result, dict) else []
            for idx, matched in enumerate(series or []):
                if not matched or idx >= len(klines):
                    continue
                bar = klines[idx]
                day = str(bar.date)[:10]
                if not (run.start_date <= day <= run.end_date):
                    continue
                price = float(bar.close or bar.open or 0)
                if price <= 0:
                    continue
                out.append(
                    BacktestSignal(
                        strategy_code=strategy.code,
                        strategy_name=strategy.name,
                        strategy_type=strategy.strategy_type or "screener_formula",
                        source_ref_id=strategy.source_ref_id,
                        signal_run_id=None,
                        stock_symbol=stock.symbol,
                        stock_market="CN",
                        stock_name=stock.name,
                        signal_date=day,
                        entry_low=price * (1 - band),
                        entry_high=price * (1 + band),
                        stop_loss=price * stop_pct,
                        target_price=price * target_pct,
                        holding_days=holding_days,
                        rank_score=0.0,
                        meta={
                            "source": "screener_formula",
                            "formula_id": formula.id,
                            "formula_name": formula.name,
                            "board_code": stock.board_code,
                        },
                    )
                )
    return out, skipped


def _skip_trade(run: BacktestRun, signal: BacktestSignal, reason: str, detail: str = "") -> BacktestTrade:
    return BacktestTrade(
        run_id=run.id,
        strategy_code=signal.strategy_code,
        strategy_name=signal.strategy_name,
        strategy_type=signal.strategy_type,
        source_ref_id=signal.source_ref_id,
        signal_run_id=signal.signal_run_id,
        stock_symbol=signal.stock_symbol or "",
        stock_market=signal.stock_market or str(run.market or "CN").upper(),
        stock_name=signal.stock_name,
        entry_date=signal.signal_date,
        exit_date=signal.signal_date,
        skipped=True,
        skip_reason=reason,
        meta={**(signal.meta or {}), "detail": detail},
    )


def _execute_signal(
    run: BacktestRun,
    signal: BacktestSignal,
    strategy: StrategyCatalog,
    klines: list[KlineData],
    available_cash: float,
    rules: dict,
) -> tuple[ExecutedTrade | None, BacktestTrade | None, float]:
    next_bar = _next_bar_after(klines, signal.signal_date)
    if not next_bar:
        return None, _skip_trade(run, signal, "no_next_bar", "信号后没有下一交易日K线"), available_cash
    entry_idx, entry_bar = next_bar
    open_price = float(entry_bar.open or 0)
    if open_price <= 0:
        return None, _skip_trade(run, signal, "invalid_entry_price", "下一交易日开盘价无效"), available_cash
    if signal.entry_low is not None and open_price < float(signal.entry_low):
        return None, _skip_trade(run, signal, "below_entry_range", f"open={open_price:.4f} < entry_low={signal.entry_low:.4f}"), available_cash
    if signal.entry_high is not None and open_price > float(signal.entry_high):
        return None, _skip_trade(run, signal, "above_entry_range", f"open={open_price:.4f} > entry_high={signal.entry_high:.4f}"), available_cash

    config = _run_config(strategy)
    position_pct = min(1.0, _config_float(config, "position_pct", 0.05))
    fee_pct = _config_float(config, "fee_pct", 0.0)
    tax_pct = _config_float(config, "tax_pct", 0.0)
    slippage_pct = _config_float(config, "slippage_pct", 0.0)
    max_holding_days = _config_int(config, "max_holding_days", signal.holding_days or 3)

    entry_price = open_price * (1 + slippage_pct)
    budget = max(0.0, available_cash * position_pct)
    quantity = int(budget // entry_price) if entry_price > 0 else 0
    if str(signal.stock_market or run.market).upper() == "CN":
        quantity = (quantity // 100) * 100
    if quantity <= 0:
        return None, _skip_trade(run, signal, "insufficient_lot_size", "资金不足以买入最小交易单位"), available_cash
    buy_fee = entry_price * quantity * fee_pct
    entry_cost = entry_price * quantity + buy_fee
    if entry_cost > available_cash:
        if str(signal.stock_market or run.market).upper() == "CN":
            quantity = int((available_cash / (entry_price * (1 + fee_pct))) // 100) * 100
        else:
            quantity = int(available_cash / (entry_price * (1 + fee_pct)))
        if quantity <= 0:
            return None, _skip_trade(run, signal, "insufficient_cash", "可用资金不足"), available_cash
        buy_fee = entry_price * quantity * fee_pct
        entry_cost = entry_price * quantity + buy_fee

    stop_loss = signal.stop_loss
    if stop_loss is None:
        stop_loss = entry_price * (1 - _rule_float(rules, "risk.paper_fallback_stop_loss_pct", 0.08))
    target_price = signal.target_price
    if target_price is None:
        target_price = entry_price * (1 + _rule_float(rules, "risk.paper_fallback_target_profit_pct", 0.15))

    exit_idx = min(len(klines) - 1, entry_idx + max_holding_days)
    exit_bar = klines[exit_idx]
    exit_reason = "max_holding_days"
    for idx in range(entry_idx, min(len(klines), entry_idx + max_holding_days + 1)):
        bar = klines[idx]
        if str(bar.date)[:10] > run.end_date:
            break
        if stop_loss is not None and float(bar.low or 0) <= float(stop_loss):
            exit_idx = idx
            exit_bar = bar
            exit_reason = "stop_loss"
            break
        if target_price is not None and float(bar.high or 0) >= float(target_price):
            exit_idx = idx
            exit_bar = bar
            exit_reason = "target_price"
            break
    if str(exit_bar.date)[:10] > run.end_date:
        for idx in range(entry_idx, len(klines)):
            if str(klines[idx].date)[:10] <= run.end_date:
                exit_idx = idx
                exit_bar = klines[idx]
        exit_reason = "end_date"
    elif str(exit_bar.date)[:10] == run.end_date and exit_reason == "max_holding_days" and exit_idx == len(klines) - 1:
        exit_reason = "end_date"

    if exit_reason == "stop_loss" and stop_loss is not None:
        exit_price = float(stop_loss) * (1 - slippage_pct)
    elif exit_reason == "target_price" and target_price is not None:
        exit_price = float(target_price) * (1 - slippage_pct)
    else:
        exit_price = float(exit_bar.close or exit_bar.open or 0) * (1 - slippage_pct)
    sell_fee = exit_price * quantity * (fee_pct + tax_pct)
    fees = buy_fee + sell_fee
    proceeds = exit_price * quantity - sell_fee
    pnl = proceeds - entry_cost
    pnl_pct = (pnl / entry_cost * 100.0) if entry_cost else 0.0
    entry_date = str(entry_bar.date)[:10]
    exit_date = str(exit_bar.date)[:10]
    holding_days = max(0, (_parse_day(exit_date) - _parse_day(entry_date)).days)
    row = BacktestTrade(
        run_id=run.id,
        strategy_code=signal.strategy_code,
        strategy_name=signal.strategy_name,
        strategy_type=signal.strategy_type,
        source_ref_id=signal.source_ref_id,
        signal_run_id=signal.signal_run_id,
        stock_symbol=signal.stock_symbol,
        stock_market=signal.stock_market or str(run.market or "CN").upper(),
        stock_name=signal.stock_name,
        quantity=quantity,
        entry_date=entry_date,
        exit_date=exit_date,
        entry_price=round(entry_price, 4),
        exit_price=round(exit_price, 4),
        stop_loss=stop_loss,
        target_price=target_price,
        pnl=round(pnl, 4),
        pnl_pct=round(pnl_pct, 4),
        fees=round(fees, 4),
        holding_days=holding_days,
        exit_reason=exit_reason,
        skipped=False,
        meta={**(signal.meta or {}), "position_pct": position_pct, "fee_pct": fee_pct, "tax_pct": tax_pct, "slippage_pct": slippage_pct},
        opened_at=_dt(entry_date),
        closed_at=_dt(exit_date),
    )
    executed = ExecutedTrade(
        strategy_code=signal.strategy_code,
        stock_key=f"{signal.stock_market}:{signal.stock_symbol}",
        entry_date=entry_date,
        exit_date=exit_date,
        quantity=quantity,
        entry_price=entry_price,
        exit_price=exit_price,
        pnl=pnl,
        pnl_pct=pnl_pct,
        fees=fees,
        entry_cost=entry_cost,
        proceeds=proceeds,
        exit_reason=exit_reason,
        holding_days=holding_days,
        row=row,
    )
    return executed, None, entry_cost


def _build_equity_rows(run: BacktestRun, trades: list[ExecutedTrade], kline_map: dict[tuple[str, str], list[KlineData]]) -> tuple[list[BacktestDailyEquity], dict]:
    start = _parse_day(run.start_date)
    end = _parse_day(run.end_date)
    days = [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(max(0, (end - start).days) + 1)]
    cash = float(run.initial_capital or 0.0)
    peak = cash
    rows: list[BacktestDailyEquity] = []
    buy_by_day: dict[str, list[ExecutedTrade]] = defaultdict(list)
    sell_by_day: dict[str, list[ExecutedTrade]] = defaultdict(list)
    for trade in trades:
        buy_by_day[trade.entry_date].append(trade)
        sell_by_day[trade.exit_date].append(trade)
    close_lookup: dict[tuple[str, str], dict[str, float]] = {}
    for key, klines in kline_map.items():
        close_lookup[key] = {str(k.date)[:10]: float(k.close or k.open or 0) for k in klines}

    active: list[ExecutedTrade] = []
    for day in days:
        for trade in buy_by_day.get(day, []):
            cash -= trade.entry_cost
            active.append(trade)
        positions_value = 0.0
        next_active: list[ExecutedTrade] = []
        for trade in active:
            if trade.exit_date <= day:
                cash += trade.proceeds
            else:
                market_symbol = tuple(trade.stock_key.split(":", 1))
                price = close_lookup.get((str(market_symbol[0]), str(market_symbol[1])), {}).get(day, trade.entry_price)
                positions_value += price * trade.quantity
                next_active.append(trade)
        active = next_active
        equity = cash + positions_value
        peak = max(peak, equity)
        drawdown = ((peak - equity) / peak * 100.0) if peak else 0.0
        rows.append(
            BacktestDailyEquity(
                run_id=run.id,
                date=day,
                cash=round(cash, 4),
                positions_value=round(positions_value, 4),
                equity=round(equity, 4),
                drawdown_pct=round(drawdown, 4),
            )
        )
    final_equity = rows[-1].equity if rows else cash
    max_dd = max((row.drawdown_pct for row in rows), default=0.0)
    return rows, {
        "final_equity": round(final_equity, 2),
        "total_pnl": round(final_equity - float(run.initial_capital or 0.0), 2),
        "total_return_pct": round(((final_equity / float(run.initial_capital or 1.0)) - 1.0) * 100.0, 2),
        "max_drawdown_pct": round(max_dd, 2),
    }


def _strategy_metrics(run: BacktestRun, strategies: dict[str, StrategyCatalog], trade_rows: list[BacktestTrade]) -> list[BacktestStrategyMetric]:
    by_code: dict[str, list[BacktestTrade]] = defaultdict(list)
    skipped_by_code: dict[str, list[BacktestTrade]] = defaultdict(list)
    for row in trade_rows:
        if row.skipped:
            skipped_by_code[row.strategy_code].append(row)
        else:
            by_code[row.strategy_code].append(row)
    out: list[BacktestStrategyMetric] = []
    for code in sorted(set(strategies.keys()) | set(by_code.keys()) | set(skipped_by_code.keys())):
        strategy = strategies.get(code)
        rows = by_code.get(code, [])
        sample = len(rows)
        wins = sum(1 for r in rows if float(r.pnl or 0) > 0)
        total_pnl = sum(float(r.pnl or 0) for r in rows)
        returns = [float(r.pnl_pct or 0) for r in rows]
        recent_cutoff = _parse_day(run.end_date) - timedelta(days=30)
        recent_rows = [r for r in rows if (_dt(r.exit_date) or _parse_day(run.end_date)) >= recent_cutoff]
        recent = (sum(float(r.pnl_pct or 0) for r in recent_rows) / len(recent_rows)) if recent_rows else 0.0
        running = 0.0
        peak = 0.0
        max_dd = 0.0
        for ret in returns:
            running += ret
            peak = max(peak, running)
            max_dd = max(max_dd, peak - running)
        exit_counts = Counter(r.exit_reason or "unknown" for r in rows)
        skip_counts = Counter(r.skip_reason or "unknown" for r in skipped_by_code.get(code, []))
        out.append(
            BacktestStrategyMetric(
                run_id=run.id,
                strategy_code=code,
                strategy_name=(strategy.name if strategy else code),
                strategy_type=(strategy.strategy_type if strategy else ""),
                stock_market=run.market or "CN",
                total_trades=sample,
                winning_trades=wins,
                win_rate=round((wins / sample * 100.0) if sample else 0.0, 4),
                total_pnl=round(total_pnl, 4),
                total_return_pct=round(total_pnl / float(run.initial_capital or 1.0) * 100.0, 4),
                avg_return_pct=round((sum(returns) / sample) if sample else 0.0, 4),
                max_drawdown_pct=round(max_dd, 4),
                recent_30d_return_pct=round(recent, 4),
                sample_size=sample,
                exit_reason_counts=dict(exit_counts),
                skip_reason_counts=dict(skip_counts),
            )
        )
    return out


def run_backtest(run_id: str) -> dict:
    ensure_strategy_catalog()
    db = SessionLocal()
    try:
        run = db.query(BacktestRun).filter(BacktestRun.id == run_id).first()
        if not run:
            raise KeyError("backtest run not found")
        run.status = "running"
        run.message = "回测运行中"
        run.started_at = _now()
        run.updated_at = _now()
        db.query(BacktestTrade).filter(BacktestTrade.run_id == run.id).delete()
        db.query(BacktestDailyEquity).filter(BacktestDailyEquity.run_id == run.id).delete()
        db.query(BacktestStrategyMetric).filter(BacktestStrategyMetric.run_id == run.id).delete()
        db.commit()

        codes = [str(x) for x in (run.strategy_codes or []) if str(x).strip()]
        explicit_codes = bool(codes)
        q = db.query(StrategyCatalog).filter(StrategyCatalog.code.in_(codes)) if explicit_codes else db.query(StrategyCatalog).filter(StrategyCatalog.enabled.is_(True))
        selected = {
            row.code: row
            for row in q.all()
            if row.code and (explicit_codes or row.enabled) and _strategy_market_ok(row, str(run.market or "CN"))
        }
        if not selected:
            run.status = "completed"
            run.message = "没有可回测的启用策略"
            run.summary = {"total_trades": 0, "skipped_count": 0, "sample_size": 0}
            run.finished_at = _now()
            db.commit()
            return run.summary or {}

        rules = get_trade_rules(db)
        signals = _historical_signals(db, selected, run)
        formula_signals, formula_skips = _screener_formula_signals(db, selected, run, rules)
        signals.extend(formula_signals)
        signals.sort(key=lambda s: (s.signal_date, -s.rank_score, s.strategy_code, s.stock_symbol))

        trade_rows: list[BacktestTrade] = []
        executed: list[ExecutedTrade] = []
        kline_map: dict[tuple[str, str], list[KlineData]] = {}
        available_cash = float(run.initial_capital or 0.0)
        active_until: dict[tuple[str, str, str], str] = {}
        pending_releases: list[ExecutedTrade] = []

        for sig in formula_skips:
            trade_rows.append(_skip_trade(run, sig, str((sig.meta or {}).get("skip_reason") or "skipped"), str((sig.meta or {}).get("error") or "")))

        for signal in signals:
            still_pending: list[ExecutedTrade] = []
            for held in pending_releases:
                if held.exit_date <= signal.signal_date:
                    available_cash += held.proceeds
                else:
                    still_pending.append(held)
            pending_releases = still_pending
            strategy = selected.get(signal.strategy_code)
            if not strategy:
                trade_rows.append(_skip_trade(run, signal, "strategy_not_selected"))
                continue
            pos_key = (signal.strategy_code, signal.stock_market, signal.stock_symbol)
            if active_until.get(pos_key, "") > signal.signal_date:
                trade_rows.append(_skip_trade(run, signal, "already_holding"))
                continue
            kkey = (signal.stock_market or str(run.market or "CN").upper(), signal.stock_symbol)
            if kkey not in kline_map:
                kline_map[kkey] = _load_klines(signal.stock_symbol, signal.stock_market or str(run.market or "CN").upper(), run.start_date, run.end_date)
            klines = kline_map[kkey]
            if not klines:
                trade_rows.append(_skip_trade(run, signal, "no_kline_data"))
                continue
            trade, skipped, entry_cost = _execute_signal(run, signal, strategy, klines, available_cash, rules)
            if skipped is not None:
                trade_rows.append(skipped)
                continue
            if trade is None:
                continue
            available_cash -= entry_cost
            active_until[pos_key] = trade.exit_date
            pending_releases.append(trade)
            executed.append(trade)
            trade_rows.append(trade.row)

        equity_rows, equity_summary = _build_equity_rows(run, executed, kline_map)
        metrics = _strategy_metrics(run, selected, trade_rows)
        for row in trade_rows:
            db.add(row)
        for row in equity_rows:
            db.add(row)
        for row in metrics:
            db.add(row)
        completed = [r for r in trade_rows if not r.skipped]
        skipped_count = len([r for r in trade_rows if r.skipped])
        wins = len([r for r in completed if float(r.pnl or 0) > 0])
        summary = {
            **equity_summary,
            "total_trades": len(completed),
            "skipped_count": skipped_count,
            "win_rate": round((wins / len(completed) * 100.0) if completed else 0.0, 2),
            "sample_size": len(completed),
            "strategy_count": len(selected),
        }
        run.status = "completed"
        run.message = "回测完成，策略排名已更新"
        run.summary = summary
        run.finished_at = _now()
        run.updated_at = _now()
        db.commit()
        return summary
    except Exception as e:
        logger.exception("backtest failed: %s", e)
        db.rollback()
        row = db.query(BacktestRun).filter(BacktestRun.id == run_id).first()
        if row:
            row.status = "failed"
            row.message = "回测失败"
            row.error = str(e)
            row.finished_at = _now()
            row.updated_at = _now()
            db.commit()
        raise
    finally:
        db.close()
