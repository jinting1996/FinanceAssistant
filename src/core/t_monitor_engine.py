"""底仓 VWAP 做 T 的持仓扫描、状态机与通知。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from src.core.kline_service import fetch_klines_sync
from src.core.notifier import NotifierManager
from src.core.signals.base_position_vwap_t import (
    compute_base_position_vwap_t,
    evaluate_t_exit,
)
from src.core.strategy_catalog import get_strategy_profile_map
from src.models.market import MARKETS, MarketCode
from src.web.database import SessionLocal
from src.web.models import (
    Account,
    NotifyChannel,
    Position,
    Stock,
    TMonitorState,
    TSignalEvent,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _now() -> datetime:
    return datetime.now(SHANGHAI).replace(tzinfo=None)


def _row_time(row: Any) -> str:
    value = row.get("date") if isinstance(row, dict) else getattr(row, "date", "")
    return str(value or "")


class TMonitorEngine:
    position_ratio = 0.2
    max_cycles_per_day = 1
    signal_ttl_minutes = 10

    async def _notify(self, db: Session, event: TSignalEvent, stock: Stock) -> None:
        channels = (
            db.query(NotifyChannel)
            .filter(NotifyChannel.enabled.is_(True), NotifyChannel.is_default.is_(True))
            .all()
        )
        if not channels:
            event.notify_error = "no_default_channel"
            return
        notifier = NotifierManager()
        for channel in channels:
            notifier.add_channel(channel.type, channel.config or {})
        action_label = {"buy_t": "做T机会", "sell_t": "做T卖出提醒", "invalidated": "做T信号失效"}.get(event.action, "做T提醒")
        title = f"【{action_label}】{stock.name} {stock.symbol}"
        content = "\n".join(
            [
                "策略：底仓 VWAP 回归做T",
                f"信号：{event.action}",
                f"当前价：{event.current_price:.3f}" if event.current_price is not None else "当前价：--",
                f"VWAP：{event.vwap:.3f}" if event.vwap is not None else "VWAP：--",
                f"支撑位：{event.support_price:.3f}" if event.support_price is not None else "支撑位：--",
                f"止损位：{event.stop_loss_price:.3f}" if event.stop_loss_price is not None else "止损位：--",
                f"目标位：{event.target_price:.3f}" if event.target_price is not None else "目标位：--",
                f"建议数量：{event.recommended_quantity} 股",
                f"触发原因：{event.reason}",
                f"失效条件：{event.invalidation}",
            ]
        )
        result = await notifier.notify_with_result(title, content)
        event.notify_success = bool(result.get("success"))
        event.notify_error = str(result.get("error") or "")

    async def _create_event(
        self,
        db: Session,
        state: TMonitorState,
        position: Position,
        stock: Stock,
        action: str,
        reason: str,
    ) -> TSignalEvent:
        signal_id = f"T:{position.id}:{state.trade_date}:{action}:{state.cycle_count}"
        event = TSignalEvent(
            state_id=state.id,
            position_id=position.id,
            signal_id=signal_id,
            trade_date=state.trade_date,
            action=action,
            score=state.score,
            current_price=state.current_price,
            vwap=state.vwap,
            support_price=state.support_price,
            stop_loss_price=state.stop_loss_price,
            target_price=state.target_price,
            recommended_quantity=state.recommended_quantity,
            position_ratio=float((state.context or {}).get("position_ratio") or self.position_ratio),
            reason=reason,
            invalidation="跌破止损位或日线趋势破坏时策略失效",
            data_quality=str((state.context or {}).get("data_quality") or ""),
            payload=state.context or {},
        )
        db.add(event)
        db.flush()
        state.last_signal_id = signal_id
        state.last_signal_at = _now()
        await self._notify(db, event, stock)
        return event

    async def _scan_position(
        self,
        db: Session,
        position: Position,
        stock: Stock,
        account: Account,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        daily, minute = await asyncio.gather(
            asyncio.to_thread(fetch_klines_sync, stock.symbol, "CN", days=120, interval="1d", cache_ttl_sec=60),
            asyncio.to_thread(fetch_klines_sync, stock.symbol, "CN", days=320, interval="1min", cache_ttl_sec=30),
        )
        if not minute:
            return {"position_id": position.id, "status": "skipped", "reason": "腾讯分钟K为空"}
        trade_date = (_row_time(minute[-1])[:10] or _now().strftime("%Y-%m-%d"))
        state = (
            db.query(TMonitorState)
            .filter(TMonitorState.position_id == position.id, TMonitorState.trade_date == trade_date)
            .first()
        )
        if state is None:
            state = TMonitorState(position_id=position.id, trade_date=trade_date, state="idle")
            db.add(state)
            db.flush()

        current = float(getattr(minute[-1], "close", 0) if not isinstance(minute[-1], dict) else minute[-1].get("close", 0))
        if state.state == "waiting_exit":
            action = evaluate_t_exit(
                current,
                vwap=float(state.vwap or current),
                target_price=float(state.target_price or current),
                stop_loss_price=float(state.stop_loss_price or 0),
            )
            state.current_price = current
            if action in {"sell_t", "invalidated"}:
                state.state = "sell_t_notified" if action == "sell_t" else "invalidated"
                event = await self._create_event(db, state, position, stock, action, "价格回归 VWAP/目标位" if action == "sell_t" else "价格跌破止损位")
                return {"position_id": position.id, "status": action, "event_id": event.id}
            return {"position_id": position.id, "status": "waiting_exit"}

        if state.state == "buy_t_notified" and state.signal_expires_at and state.signal_expires_at < _now():
            state.state = "invalidated"
            event = await self._create_event(db, state, position, stock, "invalidated", "低吸信号超过确认有效期")
            return {"position_id": position.id, "status": "invalidated", "event_id": event.id}
        max_cycles = max(1, int(params.get("max_cycles_per_day", self.max_cycles_per_day)))
        if state.state != "idle" or state.cycle_count >= max_cycles:
            return {"position_id": position.id, "status": state.state}

        signal = compute_base_position_vwap_t(
            daily,
            minute,
            min_score=int(params.get("min_score", 70)),
            min_vwap_deviation_pct=float(params.get("min_vwap_deviation_pct", 0.003)),
            min_profit_pct=float(params.get("min_profit_pct", 0.008)),
            max_stop_pct=float(params.get("max_stop_pct", 0.015)),
        )
        position_ratio = min(max(float(params.get("position_ratio", self.position_ratio)), 0.0), 0.3)
        sellable = max(int(position.sellable_quantity if position.sellable_quantity is not None else position.quantity), 0)
        cash_quantity = int(float(account.available_funds or 0) / max(float(signal.current_price or 0), 0.01))
        recommended = min(int(sellable * position_ratio), cash_quantity)
        recommended = (recommended // 100) * 100
        context = signal.to_dict()
        context["position_ratio"] = position_ratio
        state.score = signal.score
        state.current_price = signal.current_price
        state.vwap = signal.vwap
        state.support_price = signal.support_price
        state.stop_loss_price = signal.stop_loss_price
        state.target_price = signal.target_price
        state.recommended_quantity = recommended
        state.context = context
        if signal.action != "buy_t":
            return {"position_id": position.id, "status": "observe", "score": signal.score}
        if recommended < 100:
            return {"position_id": position.id, "status": "skipped", "reason": "可卖底仓或可用资金不足100股"}
        state.entry_price = signal.current_price
        ttl_minutes = max(1, int(params.get("signal_ttl_minutes", self.signal_ttl_minutes)))
        state.signal_expires_at = _now() + timedelta(minutes=ttl_minutes)
        state.state = "buy_t_notified"
        event = await self._create_event(db, state, position, stock, "buy_t", signal.reason)
        return {"position_id": position.id, "status": "buy_t", "score": signal.score, "event_id": event.id}

    async def scan_once(
        self,
        *,
        position_id: int | None = None,
        bypass_market_hours: bool = False,
    ) -> dict[str, Any]:
        if not bypass_market_hours and not MARKETS[MarketCode.CN].is_trading_time():
            return {"scanned": 0, "triggered": 0, "skipped": "outside_trading_hours", "results": []}
        profile = get_strategy_profile_map().get("base_position_vwap_t") or {}
        if not profile.get("enabled", True):
            return {"scanned": 0, "triggered": 0, "skipped": "strategy_disabled", "results": []}
        params = profile.get("params") if isinstance(profile.get("params"), dict) else {}
        db = SessionLocal()
        try:
            query = (
                db.query(Position, Stock, Account)
                .join(Stock, Position.stock_id == Stock.id)
                .join(Account, Position.account_id == Account.id)
                .filter(Stock.market == "CN", Position.quantity > 0, Account.enabled.is_(True))
            )
            if position_id is not None:
                query = query.filter(Position.id == position_id)
            results = []
            for position, stock, account in query.all():
                try:
                    results.append(await self._scan_position(db, position, stock, account, params))
                    db.commit()
                except Exception as exc:
                    db.rollback()
                    results.append({"position_id": position.id, "status": "error", "reason": str(exc)})
            triggered = sum(item.get("status") in {"buy_t", "sell_t", "invalidated"} for item in results)
            return {"scanned": len(results), "triggered": triggered, "results": results}
        finally:
            db.close()


ENGINE = TMonitorEngine()
