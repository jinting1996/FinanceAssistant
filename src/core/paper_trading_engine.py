"""模拟盘引擎：自动按策略信号建仓/平仓，跟踪虚拟账户收益。"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.core.providers import ProviderRequest, get_quote_orchestrator
from src.core.strategy_pool import resolve_enabled_strategy_codes_for_paper
from src.core.trade_rules import get_trade_rules
from src.models.market import MarketCode, MARKETS
from src.web.database import SessionLocal
from src.web.models import (
    PaperTradingAccount,
    PaperTradingPosition,
    PaperTradingTrade,
    AppSettings,
    StrategyCatalog,
    StrategySignalRun,
)

logger = logging.getLogger(__name__)

FIXED_QUANTITY = 100
SKIP_STATS_KEY = "paper_trading_last_skip_stats"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_market(market: str) -> MarketCode:
    try:
        return MarketCode(market)
    except Exception:
        return MarketCode.CN


def _is_trading_time(market: str) -> bool:
    mc = _to_market(market)
    market_def = MARKETS.get(mc)
    if not market_def:
        return False
    return market_def.is_trading_time()


def _safe_float(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
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


def _aware(dt: datetime | None) -> datetime | None:
    if not dt:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _strategy_config_map(db: Session, codes: set[str]) -> dict[str, dict]:
    if not codes:
        return {}
    rows = db.query(StrategyCatalog).filter(StrategyCatalog.code.in_(list(codes))).all()
    return {row.code: (row.run_config or {}) for row in rows if row.code}


def _config_float(config: dict | None, key: str, default: float) -> float:
    try:
        return float((config or {}).get(key, default))
    except Exception:
        return float(default)


def _risk_fraction(config: dict | None, key: str) -> float | None:
    """读取策略级风控分数(run_config.risk.<key>),如 stop_loss_pct=0.08 表示 8%。

    返回 None 表示未配置 → 调用方退回全局 trade_rules 兜底。与回测引擎保持一致。
    """
    risk = (config or {}).get("risk")
    if not isinstance(risk, dict):
        return None
    val = _safe_float(risk.get(key))
    return val if (val is not None and val > 0) else None


def _position_quantity(*, market: str, price: float, available_cash: float, config: dict | None) -> int:
    pct = max(0.001, min(_config_float(config, "position_pct", 0.05), 1.0))
    budget = min(available_cash, available_cash * pct)
    if budget <= 0 or price <= 0:
        return 0
    raw = int(budget // price)
    if market == "CN":
        return (raw // 100) * 100
    return max(0, raw)


def _skip_event(skip_events: list[dict], sig: StrategySignalRun, reason: str, detail: str = "") -> None:
    skip_events.append(
        {
            "strategy_code": sig.strategy_code or "",
            "stock_symbol": sig.stock_symbol or "",
            "stock_market": sig.stock_market or "",
            "stock_name": sig.stock_name or sig.stock_symbol or "",
            "reason": reason,
            "detail": detail,
        }
    )


def _persist_skip_stats(db: Session, skip_events: list[dict]) -> dict:
    by_reason: dict[str, int] = {}
    by_strategy: dict[str, int] = {}
    for evt in skip_events:
        reason = str(evt.get("reason") or "unknown")
        code = str(evt.get("strategy_code") or "unknown")
        by_reason[reason] = by_reason.get(reason, 0) + 1
        by_strategy[code] = by_strategy.get(code, 0) + 1
    payload = {
        "total": len(skip_events),
        "by_reason": by_reason,
        "by_strategy": by_strategy,
        "samples": skip_events[:80],
        "updated_at": _utc_now().isoformat(timespec="seconds"),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    row = db.query(AppSettings).filter(AppSettings.key == SKIP_STATS_KEY).first()
    if row:
        row.value = raw
    else:
        db.add(AppSettings(key=SKIP_STATS_KEY, value=raw, description="模拟盘最近一次跳过原因统计"))
    return payload


# ---------------------------------------------------------------------------
# 分市场资金配置（投资比例 → 子池现金）
# ---------------------------------------------------------------------------

ALL_MARKETS: tuple[str, ...] = ("CN", "HK", "US")
# 信号最大有效天数:snapshot_date 早于该窗口的 active 信号不再触发建仓/进入盘前计划
SIGNAL_MAX_AGE_DAYS = 5
DEFAULT_ALLOCATIONS: dict[str, float] = {"CN": 0.5, "HK": 0.3, "US": 0.2}


def normalize_allocations(raw: dict | None) -> dict[str, float]:
    """补齐三市场、clamp 到 [0,1]，返回 {market: ratio}。"""
    raw = raw or {}
    out: dict[str, float] = {}
    for m in ALL_MARKETS:
        try:
            v = float(raw.get(m, 0.0) or 0.0)
        except Exception:
            v = 0.0
        out[m] = min(1.0, max(0.0, v))
    return out


def market_allocations_or_default(account: Any) -> dict[str, float]:
    """账户未配置比例时回退默认配置，否则归一化已配置的比例。"""
    raw = getattr(account, "market_allocations", None) or {}
    if not raw:
        return dict(DEFAULT_ALLOCATIONS)
    return normalize_allocations(raw)


def allocations_from_excluded(excluded: list[str] | None) -> dict[str, float]:
    """迁移用：被排除市场比例置 0，其余市场按默认权重归一化到合计 1.0。"""
    excluded_set = {str(m).upper() for m in (excluded or [])}
    weights = {m: DEFAULT_ALLOCATIONS[m] for m in ALL_MARKETS if m not in excluded_set}
    total = sum(weights.values())
    if total <= 0:
        # 全部被排除：兜底投 A 股
        return {"CN": 1.0, "HK": 0.0, "US": 0.0}
    return {m: round(weights.get(m, 0.0) / total, 6) for m in ALL_MARKETS}


def compute_market_cash(
    initial_capital: float, ratio: float, realized_pnl: float, open_cost: float
) -> float:
    """某市场可用现金 = 总资金×比例 + 该市场已实现盈亏 − 该市场持仓成本（纯函数，可单测）。"""
    return initial_capital * ratio + realized_pnl - open_cost


def market_realized_open(db: Session, market: str) -> tuple[float, float]:
    """返回 (该市场已实现盈亏合计, 该市场未平仓持仓成本合计)。"""
    realized = (
        db.query(func.coalesce(func.sum(PaperTradingTrade.pnl), 0.0))
        .filter(PaperTradingTrade.stock_market == market)
        .scalar()
    ) or 0.0
    open_cost = (
        db.query(
            func.coalesce(
                func.sum(PaperTradingPosition.entry_price * PaperTradingPosition.quantity),
                0.0,
            )
        )
        .filter(
            PaperTradingPosition.status == "open",
            PaperTradingPosition.stock_market == market,
        )
        .scalar()
    ) or 0.0
    return float(realized), float(open_cost)


def market_available_cash(
    db: Session, account: PaperTradingAccount, market: str, alloc: dict | None = None
) -> float:
    """某市场当前可用现金（用于建仓门槛与展示）。"""
    alloc = alloc or market_allocations_or_default(account)
    ratio = alloc.get(market, 0.0)
    realized, open_cost = market_realized_open(db, market)
    return compute_market_cash(account.initial_capital, ratio, realized, open_cost)


def _serialize_position(pos: PaperTradingPosition) -> dict:
    """将 ORM Position 提取为 plain dict，避免 detached 问题。"""
    return {
        "id": pos.id,
        "stock_symbol": pos.stock_symbol,
        "stock_market": pos.stock_market,
        "stock_name": pos.stock_name or "",
        "quantity": pos.quantity,
        "entry_price": pos.entry_price,
        "stop_loss": pos.stop_loss,
        "target_price": pos.target_price,
        "current_price": pos.current_price,
        "unrealized_pnl": pos.unrealized_pnl,
        "status": pos.status,
        "strategy_code": pos.strategy_code or "",
    }


def _serialize_trade(trade: PaperTradingTrade) -> dict:
    """将 ORM Trade 提取为 plain dict。"""
    return {
        "id": trade.id,
        "stock_symbol": trade.stock_symbol,
        "stock_market": trade.stock_market,
        "stock_name": trade.stock_name or "",
        "quantity": trade.quantity,
        "entry_price": trade.entry_price,
        "exit_price": trade.exit_price,
        "pnl": trade.pnl,
        "pnl_pct": trade.pnl_pct,
        "exit_reason": trade.exit_reason,
        "holding_days": trade.holding_days,
        "strategy_code": trade.strategy_code or "",
    }


def _serialize_signal(sig: StrategySignalRun) -> dict:
    """将 ORM Signal 提取为 plain dict。"""
    return {
        "id": sig.id,
        "stock_symbol": sig.stock_symbol,
        "stock_market": sig.stock_market,
        "stock_name": sig.stock_name or "",
        "strategy_code": sig.strategy_code or "",
        "rank_score": sig.rank_score,
        "entry_low": sig.entry_low,
        "entry_high": sig.entry_high,
        "action": sig.action,
    }


class PaperTradingEngine:
    """模拟盘扫描引擎。"""

    def _get_or_create_account(self, db: Session) -> PaperTradingAccount:
        account = db.query(PaperTradingAccount).first()
        if not account:
            account = PaperTradingAccount(
                initial_capital=1000000.0,
                current_capital=1000000.0,
                peak_capital=1000000.0,
            )
            db.add(account)
            db.commit()
            db.refresh(account)
        return account

    def _fetch_quotes_map(self, symbols_markets: list[tuple[str, str]]) -> dict[tuple[str, str], dict]:
        """批量获取报价，返回 {(market, symbol): quote_dict}

        通过 QuoteOrchestrator 调度,支持多 provider 主备故障转移。
        """
        grouped: dict[MarketCode, list[str]] = {}
        for symbol, market in symbols_markets:
            mc = _to_market(market)
            grouped.setdefault(mc, []).append(symbol)

        orch = get_quote_orchestrator()
        out: dict[tuple[str, str], dict] = {}
        for market, symbols in grouped.items():
            if not symbols:
                continue
            req = ProviderRequest(symbols=tuple(symbols), market=market.value)
            resp = orch.fetch_sync(req)
            if not resp.success:
                logger.error(f"[模拟盘] 拉行情失败 {market.value}: {resp.error}")
                continue
            by_symbol = {str(r.get("symbol")): r for r in (resp.data or [])}
            for sym in symbols:
                q = by_symbol.get(sym)
                if q:
                    out[(market.value, sym)] = q
        return out

    def _check_entries(
        self, db: Session, account: PaperTradingAccount,
    ) -> tuple[int, set[tuple[str, str]], list[tuple[PaperTradingPosition, StrategySignalRun | None]], list[dict]]:
        """检查可入场的策略信号，自动建仓。返回 (建仓数, 新建仓股票key集合, 建仓事件列表, 跳过事件)。"""
        skip_events: list[dict] = []
        # 查询最新活跃买入信号(超过 SIGNAL_MAX_AGE_DAYS 的陈旧信号不再触发建仓,
        # 避免生成方未及时置 inactive 时按过时的入场区间开仓)
        fresh_after = (_utc_now() - timedelta(days=SIGNAL_MAX_AGE_DAYS)).strftime("%Y-%m-%d")
        query = (
            db.query(StrategySignalRun)
            .filter(
                StrategySignalRun.status == "active",
                StrategySignalRun.action.in_(["buy", "add"]),
                StrategySignalRun.entry_low.isnot(None),
                StrategySignalRun.entry_high.isnot(None),
                StrategySignalRun.snapshot_date >= fresh_after,
            )
        )
        selected_codes = resolve_enabled_strategy_codes_for_paper(db)
        # 先在 SQL 层按已选策略过滤,避免未选策略的高分信号挤占 limit 名额
        if selected_codes is not None:
            if not selected_codes:
                return 0, set(), [], skip_events
            query = query.filter(StrategySignalRun.strategy_code.in_(selected_codes))
        # 按投资比例排除不投入（比例为 0）的市场
        alloc = market_allocations_or_default(account)
        excluded = [m for m in ALL_MARKETS if alloc.get(m, 0.0) <= 0]
        if excluded:
            query = query.filter(StrategySignalRun.stock_market.notin_(excluded))
        signals = query.order_by(StrategySignalRun.rank_score.desc()).limit(50).all()
        entry_events: list[tuple[PaperTradingPosition, StrategySignalRun | None]] = []
        new_keys: set[tuple[str, str]] = set()
        if not signals:
            return 0, new_keys, entry_events, skip_events

        # 已有 open position 的股票
        open_keys = set()
        open_positions = (
            db.query(PaperTradingPosition)
            .filter(PaperTradingPosition.status == "open")
            .all()
        )
        for p in open_positions:
            open_keys.add((p.stock_symbol, p.stock_market))

        # 收集需要报价的信号（去重：同股票只取 rank_score 最高的一条）
        candidates = []
        seen = set()
        for sig in signals:
            if selected_codes is not None and str(sig.strategy_code or "") not in selected_codes:
                _skip_event(skip_events, sig, "strategy_not_selected", "该策略未被模拟盘策略选择启用")
                continue
            key = (sig.stock_symbol, sig.stock_market)
            if key in open_keys:
                _skip_event(skip_events, sig, "existing_position", "已有模拟盘持仓")
                continue
            if key in seen:
                _skip_event(skip_events, sig, "duplicate_signal", "同股票已有更高排名信号")
                continue
            seen.add(key)
            candidates.append(sig)

        if not candidates:
            return 0, new_keys, entry_events, skip_events

        # 批量获取报价
        syms = [(s.stock_symbol, s.stock_market) for s in candidates]
        quotes = self._fetch_quotes_map(syms)

        # 预算各市场可用现金（建仓时按市场子池逐笔扣减）
        market_cash = {m: market_available_cash(db, account, m, alloc) for m in ALL_MARKETS}
        rules = get_trade_rules(db)
        stop_loss_pct = _rule_float(rules, "risk.paper_fallback_stop_loss_pct", 0.08)
        target_profit_pct = _rule_float(rules, "risk.paper_fallback_target_profit_pct", 0.15)
        strategy_configs = _strategy_config_map(
            db,
            {str(sig.strategy_code or "") for sig in candidates if sig.strategy_code},
        )

        opened = 0
        for sig in candidates:
            config = strategy_configs.get(sig.strategy_code or "", {})
            ttl_hours = _config_float(config, "signal_ttl_hours", 48.0)
            sig_time = _aware(sig.updated_at or sig.created_at)
            if ttl_hours > 0 and sig_time is not None:
                age_hours = (_utc_now() - sig_time).total_seconds() / 3600.0
                if age_hours > ttl_hours:
                    _skip_event(skip_events, sig, "signal_expired", f"信号已过期 {age_hours:.1f}h")
                    continue
            key = (sig.stock_market, sig.stock_symbol)
            quote = quotes.get(key)
            if not quote:
                _skip_event(skip_events, sig, "no_quote", "行情不可用")
                continue
            current_price = _safe_float(quote.get("current_price"))
            if current_price is None or current_price <= 0:
                _skip_event(skip_events, sig, "invalid_price", "当前价无效")
                continue
            if sig.entry_low and current_price < sig.entry_low:
                _skip_event(skip_events, sig, "below_entry_range", f"{current_price:.2f} < {sig.entry_low:.2f}")
                continue
            if sig.entry_high and current_price > sig.entry_high:
                _skip_event(skip_events, sig, "above_entry_range", f"{current_price:.2f} > {sig.entry_high:.2f}")
                continue

            # 用当前市价入场
            slippage_pct = max(0.0, _config_float(config, "slippage_pct", 0.0))
            fee_pct = max(0.0, _config_float(config, "fee_pct", 0.0))
            entry_price = round(current_price * (1 + slippage_pct), 4)
            mkt = sig.stock_market
            if alloc.get(mkt, 0.0) <= 0:
                _skip_event(skip_events, sig, "market_disabled", "该市场投资比例为 0")
                continue  # 该市场比例为 0，不投入
            quantity = _position_quantity(
                market=mkt,
                price=entry_price,
                available_cash=market_cash.get(mkt, 0.0),
                config=config,
            )
            if quantity <= 0:
                _skip_event(skip_events, sig, "quantity_too_small", "按仓位计算不足最小交易单位")
                continue
            cost = entry_price * quantity * (1 + fee_pct)
            if cost > market_cash.get(mkt, 0.0):
                _skip_event(skip_events, sig, "insufficient_cash", "该市场子池额度不足")
                continue  # 该市场子池额度不足

            # 基于入场价计算止损/止盈
            # 优先用信号的止损/止盈比例，否则用默认 -8%/+15%
            stop_loss = sig.stop_loss
            target_price = sig.target_price
            if stop_loss and sig.entry_low and sig.entry_low > 0:
                # 保留信号的止损比例，映射到实际入场价
                orig_mid = (sig.entry_low + (sig.entry_high or sig.entry_low)) / 2
                if orig_mid > 0:
                    stop_ratio = (stop_loss - orig_mid) / orig_mid
                    target_ratio = ((target_price - orig_mid) / orig_mid) if target_price else 0.15
                    stop_loss = round(entry_price * (1 + stop_ratio), 4)
                    target_price = round(entry_price * (1 + target_ratio), 4) if target_price else None
            # 兜底优先级:信号显式价位 > 策略级风控(run_config.risk) > 全局 -8%/+15%
            sl_frac = _risk_fraction(config, "stop_loss_pct")
            tp_frac = _risk_fraction(config, "target_profit_pct")
            if not stop_loss or stop_loss <= 0 or stop_loss >= entry_price:
                pct = sl_frac if sl_frac is not None else stop_loss_pct
                stop_loss = round(entry_price * (1 - pct), 4)
            if not target_price or target_price <= 0 or target_price <= entry_price:
                pct = tp_frac if tp_frac is not None else target_profit_pct
                target_price = round(entry_price * (1 + pct), 4)

            pos = PaperTradingPosition(
                stock_symbol=sig.stock_symbol,
                stock_market=sig.stock_market,
                stock_name=sig.stock_name or "",
                quantity=quantity,
                entry_price=entry_price,
                stop_loss=stop_loss,
                target_price=target_price,
                current_price=current_price,
                unrealized_pnl=0.0,
                status="open",
                signal_run_id=sig.id,
                signal_snapshot_date=sig.snapshot_date or "",
                signal_action=sig.action or "",
                strategy_code=sig.strategy_code or "",
            )
            db.add(pos)
            account.current_capital -= cost
            market_cash[mkt] = market_cash.get(mkt, 0.0) - cost
            open_keys.add((sig.stock_symbol, sig.stock_market))
            new_keys.add((sig.stock_symbol, sig.stock_market))
            entry_events.append((pos, sig))
            opened += 1
            logger.info(
                "[模拟盘] 建仓: %s %s @ %.2f, 止损=%.2f, 止盈=%s, 策略=%s",
                sig.stock_name or sig.stock_symbol,
                sig.stock_market,
                entry_price,
                sig.stop_loss or 0,
                sig.target_price or "无",
                sig.strategy_code,
            )

        if opened > 0:
            db.commit()
        return opened, new_keys, entry_events, skip_events

    def _close_position(
        self,
        db: Session,
        account: PaperTradingAccount,
        pos: PaperTradingPosition,
        exit_price: float,
        exit_reason: str,
    ) -> PaperTradingTrade:
        """平仓单个持仓，返回交易记录。"""
        now = _utc_now()
        config = _strategy_config_map(db, {pos.strategy_code or ""}).get(pos.strategy_code or "", {})
        fee_pct = max(0.0, _config_float(config, "fee_pct", 0.0))
        tax_pct = max(0.0, _config_float(config, "tax_pct", 0.0))
        slippage_pct = max(0.0, _config_float(config, "slippage_pct", 0.0))
        exit_price = round(exit_price * (1 - slippage_pct), 4)
        fees = (pos.entry_price * pos.quantity * fee_pct) + (exit_price * pos.quantity * (fee_pct + tax_pct))
        pnl = (exit_price - pos.entry_price) * pos.quantity - fees
        pnl_pct = ((exit_price - pos.entry_price) / pos.entry_price * 100) if pos.entry_price > 0 else 0.0

        holding_days = 0
        if pos.opened_at:
            opened = pos.opened_at
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=timezone.utc)
            holding_days = max(0, (now - opened).days)

        trade = PaperTradingTrade(
            stock_symbol=pos.stock_symbol,
            stock_market=pos.stock_market,
            stock_name=pos.stock_name or "",
            quantity=pos.quantity,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            pnl=pnl,
            pnl_pct=round(pnl_pct, 2),
            exit_reason=exit_reason,
            signal_run_id=pos.signal_run_id,
            signal_snapshot_date=pos.signal_snapshot_date or "",
            strategy_code=pos.strategy_code or "",
            holding_days=holding_days,
            opened_at=pos.opened_at,
            closed_at=now,
        )
        db.add(trade)

        pos.status = "closed"
        pos.closed_at = now
        pos.current_price = exit_price
        pos.unrealized_pnl = pnl

        # 回收资金
        sell_cost = exit_price * pos.quantity * (fee_pct + tax_pct)
        account.current_capital += exit_price * pos.quantity - sell_cost
        account.total_pnl += pnl
        account.total_trades += 1
        if pnl > 0:
            account.winning_trades += 1

        logger.info(
            "[模拟盘] 平仓: %s %s @ %.2f, 盈亏=%.2f (%.2f%%), 原因=%s",
            pos.stock_name or pos.stock_symbol,
            pos.stock_market,
            exit_price,
            pnl,
            pnl_pct,
            exit_reason,
        )
        return trade

    def _check_exits(
        self, db: Session, account: PaperTradingAccount, skip_keys: set[tuple[str, str]] | None = None,
    ) -> tuple[int, list[tuple[PaperTradingPosition, PaperTradingTrade]]]:
        """检查持仓止损/止盈/信号反转，自动平仓。skip_keys 中的股票跳过（本轮新建仓）。"""
        exit_events: list[tuple[PaperTradingPosition, PaperTradingTrade]] = []
        positions = (
            db.query(PaperTradingPosition)
            .filter(PaperTradingPosition.status == "open")
            .all()
        )
        if not positions:
            return 0, exit_events

        # 批量获取报价
        syms = [(p.stock_symbol, p.stock_market) for p in positions]
        quotes = self._fetch_quotes_map(syms)

        closed = 0
        for pos in positions:
            # 跳过本轮刚建仓的持仓
            if skip_keys and (pos.stock_symbol, pos.stock_market) in skip_keys:
                continue
            key = (pos.stock_market, pos.stock_symbol)
            quote = quotes.get(key)
            current_price = _safe_float(quote.get("current_price")) if quote else None

            if current_price is None or current_price <= 0:
                continue

            # 更新现价和浮动盈亏
            pos.current_price = current_price
            pos.unrealized_pnl = (current_price - pos.entry_price) * pos.quantity

            # 检查止损
            if pos.stop_loss and current_price <= pos.stop_loss:
                trade = self._close_position(db, account, pos, current_price, "stop_loss")
                exit_events.append((pos, trade))
                closed += 1
                continue

            # 检查止盈
            if pos.target_price and current_price >= pos.target_price:
                trade = self._close_position(db, account, pos, current_price, "target_price")
                exit_events.append((pos, trade))
                closed += 1
                continue

            # 检查信号反转
            if pos.signal_run_id:
                latest = (
                    db.query(StrategySignalRun)
                    .filter(
                        StrategySignalRun.stock_symbol == pos.stock_symbol,
                        StrategySignalRun.stock_market == pos.stock_market,
                        StrategySignalRun.status == "active",
                    )
                    .order_by(StrategySignalRun.created_at.desc())
                    .first()
                )
                if latest and latest.action in ("sell", "reduce"):
                    trade = self._close_position(db, account, pos, current_price, "signal_reversal")
                    exit_events.append((pos, trade))
                    closed += 1
                    continue

        self._update_account_metrics(db, account)
        db.commit()
        return closed, exit_events

    def _update_account_metrics(self, db: Session, account: PaperTradingAccount) -> None:
        """更新账户峰值和最大回撤。"""
        # 计算包含浮动盈亏的总资产
        open_positions = (
            db.query(PaperTradingPosition)
            .filter(PaperTradingPosition.status == "open")
            .all()
        )
        unrealized_total = sum(p.unrealized_pnl or 0 for p in open_positions)
        total_equity = account.current_capital + sum(
            (p.current_price or p.entry_price) * p.quantity for p in open_positions
        )

        if total_equity > account.peak_capital:
            account.peak_capital = total_equity

        if account.peak_capital > 0:
            drawdown = (account.peak_capital - total_equity) / account.peak_capital * 100
            if drawdown > account.max_drawdown_pct:
                account.max_drawdown_pct = round(drawdown, 2)

    def _scan_sync(self) -> dict:
        """同步扫描（在线程中执行）。"""
        db = SessionLocal()
        try:
            account = self._get_or_create_account(db)
            if not account.enabled:
                return {"status": "disabled"}

            opened, new_keys, entry_events, skip_events = self._check_entries(db, account)
            closed, exit_events = self._check_exits(db, account, skip_keys=new_keys)
            skip_stats = _persist_skip_stats(db, skip_events)
            db.commit()

            # 在 db.close() 前将 ORM 对象序列化为 dict，避免 detached 问题
            serialized_entries = [
                {"pos_data": _serialize_position(pos), "sig_data": _serialize_signal(sig) if sig else None}
                for pos, sig in entry_events
            ]
            serialized_exits = [
                {"pos_data": _serialize_position(pos), "trade_data": _serialize_trade(trade)}
                for pos, trade in exit_events
            ]

            return {
                "status": "ok",
                "opened": opened,
                "closed": closed,
                "skipped": len(skip_events),
                "skip_stats": skip_stats,
                "entry_events": serialized_entries,
                "exit_events": serialized_exits,
            }
        except Exception as e:
            logger.exception(f"[模拟盘] 扫描异常: {e}")
            return {"status": "error", "error": str(e)}
        finally:
            db.close()

    async def scan_once(self) -> dict:
        """异步扫描入口。"""
        result = await asyncio.to_thread(self._scan_sync)
        # 发送通知（异步，失败不影响交易）
        await self._send_notifications(result)
        return result

    def close_position_manual(self, position_id: int) -> dict:
        """手动平仓。"""
        db = SessionLocal()
        try:
            account = self._get_or_create_account(db)
            pos = (
                db.query(PaperTradingPosition)
                .filter(
                    PaperTradingPosition.id == position_id,
                    PaperTradingPosition.status == "open",
                )
                .first()
            )
            if not pos:
                return {"ok": False, "error": "持仓不存在或已平仓"}

            # 获取最新报价(走 orchestrator,支持故障转移)
            mc = _to_market(pos.stock_market)
            orch = get_quote_orchestrator()
            resp = orch.fetch_sync(
                ProviderRequest(symbols=(pos.stock_symbol,), market=mc.value)
            )
            rows = resp.data if resp.success and resp.data else []

            exit_price = pos.current_price or pos.entry_price
            if rows:
                p = _safe_float(rows[0].get("current_price"))
                if p and p > 0:
                    exit_price = p

            trade = self._close_position(db, account, pos, exit_price, "manual")
            self._update_account_metrics(db, account)
            db.commit()
            # 序列化后返回，避免 db.close() 后 ORM 对象 detached
            return {
                "ok": True,
                "pos_data": _serialize_position(pos),
                "trade_data": _serialize_trade(trade),
            }
        finally:
            db.close()

    async def close_position_manual_async(self, position_id: int) -> dict:
        """异步手动平仓，含通知。"""
        result = await asyncio.to_thread(self.close_position_manual, position_id)
        if result.get("ok"):
            try:
                from src.core.paper_trading_notifier import notify_exit
                pos_data = result.pop("pos_data", None)
                trade_data = result.pop("trade_data", None)
                if pos_data and trade_data:
                    await notify_exit(pos_data, trade_data)
            except Exception:
                logger.exception("[模拟盘] 手动平仓通知失败")
        return result

    async def _send_notifications(self, result: dict) -> None:
        """从扫描结果中取出序列化事件，发送通知。"""
        try:
            from src.core.paper_trading_notifier import notify_entry, notify_exit

            for evt in result.pop("entry_events", []):
                await notify_entry(evt["pos_data"], evt.get("sig_data"))
            for evt in result.pop("exit_events", []):
                await notify_exit(evt["pos_data"], evt["trade_data"])
        except Exception:
            logger.exception("[模拟盘] 通知发送失败")

    def reset_account(self) -> dict:
        """重置模拟盘（清空所有数据）。"""
        db = SessionLocal()
        try:
            db.query(PaperTradingPosition).delete()
            db.query(PaperTradingTrade).delete()
            account = db.query(PaperTradingAccount).first()
            if account:
                account.current_capital = account.initial_capital
                account.total_pnl = 0.0
                account.total_trades = 0
                account.winning_trades = 0
                account.max_drawdown_pct = 0.0
                account.peak_capital = account.initial_capital
                account.enabled = True
            db.commit()
            return {"ok": True}
        finally:
            db.close()


ENGINE = PaperTradingEngine()
