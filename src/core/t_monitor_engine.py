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
    compute_base_position_vwap_t_short,
    evaluate_t_exit,
    evaluate_t_exit_short,
)

# 倒T(先卖后买)相关的动作与状态
_SHORT_ACTIONS = {"sell_open", "buy_back"}
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
    max_cycles_per_day = 5  # 当日最多做T轮数;0 表示不限
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
        action_label = {
            "buy_t": "做T机会(低吸)",
            "sell_t": "做T卖出提醒(止盈)",
            "sell_open": "做T机会(高抛/倒T)",
            "buy_back": "做T买回提醒(倒T)",
            "invalidated": "做T信号失效",
        }.get(event.action, "做T提醒")
        is_short = event.action in _SHORT_ACTIONS
        level_label = "压力位" if is_short else "支撑位"
        title = f"【{action_label}】{stock.name} {stock.symbol}"
        content = "\n".join(
            [
                "策略：底仓 VWAP 回归做T",
                f"方向：{'倒T(先卖后买)' if is_short else '正T(先买后卖)'}",
                f"信号：{event.action}",
                f"当前价：{event.current_price:.3f}" if event.current_price is not None else "当前价：--",
                f"VWAP：{event.vwap:.3f}" if event.vwap is not None else "VWAP：--",
                f"{level_label}：{event.support_price:.3f}" if event.support_price is not None else f"{level_label}：--",
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
            invalidation=(
                "突破止损位或日线转强时策略失效"
                if action in _SHORT_ACTIONS
                else "跌破止损位或日线趋势破坏时策略失效"
            ),
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
            asyncio.to_thread(fetch_klines_sync, stock.symbol, "CN", days=320, interval="1min", cache_ttl_sec=8),
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
        thresholds = dict(
            min_score=int(params.get("min_score", 70)),
            min_vwap_deviation_pct=float(params.get("min_vwap_deviation_pct", 0.003)),
            min_profit_pct=float(params.get("min_profit_pct", 0.008)),
            max_stop_pct=float(params.get("max_stop_pct", 0.015)),
        )

        # --- 离场态:实时重算"离场质量分"(平仓本质是反向入场),价格阈值或评分任一满足即触发 ---
        if state.state == "waiting_exit":
            # 正T 卖出 = 高抛质量(反向 short 入场分)
            price_hit = evaluate_t_exit(
                current,
                vwap=float(state.vwap or current),
                target_price=float(state.target_price or current),
                stop_loss_price=float(state.stop_loss_price or 0),
            )
            sell_sig = compute_base_position_vwap_t_short(daily, minute, **thresholds)
            state.current_price = current
            state.score = sell_sig.score
            state.context = {**(state.context or {}), "exit_score": sell_sig.score, "exit_reason": sell_sig.reason}
            if price_hit == "invalidated":
                state.state = "invalidated"
                event = await self._create_event(db, state, position, stock, "invalidated", "价格跌破止损位")
                return {"position_id": position.id, "status": "invalidated", "event_id": event.id}
            if price_hit == "sell_t" or sell_sig.action == "sell_open":
                state.state = "sell_t_notified"
                reason = "价格回归 VWAP/目标位" if price_hit == "sell_t" else f"高抛质量达标(分{sell_sig.score}):{sell_sig.reason}"
                event = await self._create_event(db, state, position, stock, "sell_t", reason)
                return {"position_id": position.id, "status": "sell_t", "event_id": event.id}
            return {"position_id": position.id, "status": "waiting_exit", "score": sell_sig.score}

        if state.state == "waiting_buyback":
            # 倒T 买回 = 低吸质量(反向 long 入场分)
            price_hit = evaluate_t_exit_short(
                current,
                vwap=float(state.vwap or current),
                target_price=float(state.target_price or current),
                stop_loss_price=float(state.stop_loss_price or current),
            )
            buy_sig = compute_base_position_vwap_t(daily, minute, **thresholds)
            state.current_price = current
            state.score = buy_sig.score
            state.context = {**(state.context or {}), "buyback_score": buy_sig.score, "buyback_reason": buy_sig.reason}
            if price_hit == "invalidated":
                state.state = "invalidated"
                event = await self._create_event(db, state, position, stock, "invalidated", "价格突破止损位")
                return {"position_id": position.id, "status": "invalidated", "event_id": event.id}
            if price_hit == "buy_back" or buy_sig.action == "buy_t":
                state.state = "buy_back_notified"
                reason = "价格回落 VWAP/目标位" if price_hit == "buy_back" else f"低吸质量达标(分{buy_sig.score}):{buy_sig.reason}"
                event = await self._create_event(db, state, position, stock, "buy_back", reason)
                return {"position_id": position.id, "status": "buy_back", "event_id": event.id}
            return {"position_id": position.id, "status": "waiting_buyback", "score": buy_sig.score}

        # --- 开仓通知态超过确认有效期则失效 ---
        if state.state in {"buy_t_notified", "sell_open_notified"} and state.signal_expires_at and state.signal_expires_at < _now():
            state.state = "invalidated"
            event = await self._create_event(db, state, position, stock, "invalidated", "做T信号超过确认有效期")
            return {"position_id": position.id, "status": "invalidated", "event_id": event.id}

        # 当日做T次数上限:<=0 表示不限。完成一轮后(state=completed)仍可在限额内继续找机会。
        max_cycles = int(params.get("max_cycles_per_day", self.max_cycles_per_day))
        unlimited = max_cycles <= 0
        if not unlimited and state.cycle_count >= max_cycles:
            return {"position_id": position.id, "status": state.state}
        if state.state not in {"idle", "completed"}:
            return {"position_id": position.id, "status": state.state}
        # 完成一轮后冷却,避免高频扫描下立刻又开仓(0=不冷却)
        cooldown_min = int(params.get("cycle_cooldown_minutes", 3))
        if (
            state.state == "completed"
            and cooldown_min > 0
            and state.last_signal_at
            and (_now() - state.last_signal_at) < timedelta(minutes=cooldown_min)
        ):
            return {"position_id": position.id, "status": "completed"}

        # --- idle:按方向计算多/空入场信号,谁满足谁触发(同分优先正T) ---
        direction = str(params.get("direction", "both") or "both").lower()
        position_ratio = min(max(float(params.get("position_ratio", self.position_ratio)), 0.0), 0.3)
        sellable = max(int(position.sellable_quantity if position.sellable_quantity is not None else position.quantity), 0)

        candidates: list[tuple[str, str, Any, int]] = []  # (side, action, signal, recommended)
        display_signal: Any = None
        skip_reason: str | None = None
        if direction in {"both", "long"}:
            long_signal = compute_base_position_vwap_t(daily, minute, **thresholds)
            display_signal = display_signal or long_signal
            if long_signal.action == "buy_t":
                cash_quantity = int(float(account.available_funds or 0) / max(float(long_signal.current_price or 0), 0.01))
                rec = (min(int(sellable * position_ratio), cash_quantity) // 100) * 100
                if rec >= 100:
                    candidates.append(("long", "buy_t", long_signal, rec))
                else:
                    skip_reason = (
                        f"低吸已达标(分{long_signal.score}),但建议数量不足100股:"
                        f"可卖底仓 {sellable}×{position_ratio:.0%}={int(sellable * position_ratio)} 股,"
                        f"可用资金可买 {cash_quantity} 股"
                    )
        if direction in {"both", "short"}:
            short_signal = compute_base_position_vwap_t_short(daily, minute, **thresholds)
            if display_signal is None or short_signal.score > getattr(display_signal, "score", 0):
                display_signal = short_signal
            if short_signal.action == "sell_open":
                rec = (int(sellable * position_ratio) // 100) * 100  # 卖底仓,不占用现金
                if rec >= 100:
                    candidates.append(("short", "sell_open", short_signal, rec))
                elif not skip_reason:
                    skip_reason = (
                        f"高抛已达标(分{short_signal.score}),但可卖底仓不足100股:"
                        f"底仓 {sellable}×{position_ratio:.0%}={int(sellable * position_ratio)} 股"
                    )

        if not candidates:
            if display_signal is not None:
                state.score = display_signal.score
                state.current_price = display_signal.current_price
                state.vwap = display_signal.vwap
                context = {**display_signal.to_dict(), "position_ratio": position_ratio}
                if skip_reason:
                    context["skip_reason"] = skip_reason
                state.context = context
            status = "skipped" if skip_reason else "observe"
            return {"position_id": position.id, "status": status, "score": getattr(display_signal, "score", 0), "reason": skip_reason}

        # 同分优先正T(买):long 在前,稳定排序按分数降序
        candidates.sort(key=lambda c: (c[2].score, c[0] == "long"), reverse=True)
        side, action, signal, recommended = candidates[0]

        context = signal.to_dict()
        context["position_ratio"] = position_ratio
        context["direction"] = side
        state.score = signal.score
        state.current_price = signal.current_price
        state.vwap = signal.vwap
        state.support_price = signal.support_price
        state.stop_loss_price = signal.stop_loss_price
        state.target_price = signal.target_price
        state.recommended_quantity = recommended
        state.entry_price = signal.current_price
        state.context = context
        ttl_minutes = max(1, int(params.get("signal_ttl_minutes", self.signal_ttl_minutes)))
        state.signal_expires_at = _now() + timedelta(minutes=ttl_minutes)
        state.state = "buy_t_notified" if side == "long" else "sell_open_notified"
        event = await self._create_event(db, state, position, stock, action, signal.reason)
        return {"position_id": position.id, "status": action, "score": signal.score, "event_id": event.id}

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
            triggered = sum(item.get("status") in {"buy_t", "sell_t", "sell_open", "buy_back", "invalidated"} for item in results)
            return {"scanned": len(results), "triggered": triggered, "results": results}
        finally:
            db.close()

    async def manual_action(self, state_id: int, action: str) -> dict[str, Any]:
        """用户手动驱动状态机:标记已低吸/已高抛(开始盯对侧)、完成、重置。"""
        profile = get_strategy_profile_map().get("base_position_vwap_t") or {}
        params = profile.get("params") if isinstance(profile.get("params"), dict) else {}
        db = SessionLocal()
        try:
            state = db.query(TMonitorState).filter(TMonitorState.id == state_id).first()
            if not state:
                return {"success": False, "error": "做T状态不存在"}

            if action == "reset":
                state.state = "idle"
                state.cycle_count = 0
                state.signal_expires_at = None
                db.commit()
                return {"success": True, "state": state.state}

            if action == "mark_done":
                state.state = "completed"
                state.cycle_count = (state.cycle_count or 0) + 1
                state.signal_expires_at = None
                db.commit()
                return {"success": True, "state": state.state, "cycle_count": state.cycle_count}

            if action not in {"mark_long_open", "mark_short_open"}:
                return {"success": False, "error": f"未知操作 {action}"}

            position = db.query(Position).filter(Position.id == state.position_id).first()
            stock = db.query(Stock).filter(Stock.id == position.stock_id).first() if position else None
            if not position or not stock:
                return {"success": False, "error": "持仓不存在"}

            daily, minute = await asyncio.gather(
                asyncio.to_thread(fetch_klines_sync, stock.symbol, "CN", days=120, interval="1d", cache_ttl_sec=60),
                asyncio.to_thread(fetch_klines_sync, stock.symbol, "CN", days=320, interval="1min", cache_ttl_sec=30),
            )
            if not minute:
                return {"success": False, "error": "暂无分钟K数据,无法计算参考位"}
            thresholds = dict(
                min_score=int(params.get("min_score", 70)),
                min_vwap_deviation_pct=float(params.get("min_vwap_deviation_pct", 0.003)),
                min_profit_pct=float(params.get("min_profit_pct", 0.008)),
                max_stop_pct=float(params.get("max_stop_pct", 0.015)),
            )
            current = float(getattr(minute[-1], "close", 0) if not isinstance(minute[-1], dict) else minute[-1].get("close", 0)) or 0.0
            if action == "mark_long_open":
                sig = compute_base_position_vwap_t(daily, minute, **thresholds)
                side, new_state = "long", "waiting_exit"
                stop = sig.stop_loss_price if sig.stop_loss_price else round(current * 0.985, 4)
                target = sig.target_price if sig.target_price else round(current * 1.008, 4)
            else:
                sig = compute_base_position_vwap_t_short(daily, minute, **thresholds)
                side, new_state = "short", "waiting_buyback"
                stop = sig.stop_loss_price if sig.stop_loss_price else round(current * 1.015, 4)
                target = sig.target_price if sig.target_price else round(current * 0.992, 4)

            entry = sig.current_price or current
            state.state = new_state
            state.entry_price = entry
            state.current_price = entry
            state.vwap = sig.vwap
            state.support_price = sig.support_price
            state.stop_loss_price = stop
            state.target_price = target
            state.signal_expires_at = None
            state.context = {**sig.to_dict(), "direction": side, "manual": True, "stop_loss_price": stop, "target_price": target}
            db.commit()
            return {"success": True, "state": state.state}
        except Exception as exc:
            db.rollback()
            return {"success": False, "error": str(exc)}
        finally:
            db.close()

    async def execute_leg(self, state_id: int, action: str, price: float, quantity: int) -> dict[str, Any]:
        """记录一腿实际成交(价+量)。开仓进入对应等待态;平仓计算 realized 并摊低持仓成本。

        action: long_open / short_open / long_close / short_close
        """
        if action not in {"long_open", "short_open", "long_close", "short_close"}:
            return {"success": False, "error": f"未知操作 {action}"}
        try:
            price = float(price)
            quantity = int(quantity)
        except (TypeError, ValueError):
            return {"success": False, "error": "成交价/数量格式错误"}
        if price <= 0 or quantity <= 0:
            return {"success": False, "error": "成交价与数量需大于 0"}

        profile = get_strategy_profile_map().get("base_position_vwap_t") or {}
        params = profile.get("params") if isinstance(profile.get("params"), dict) else {}
        min_profit = float(params.get("min_profit_pct", 0.008))
        max_stop = float(params.get("max_stop_pct", 0.015))

        db = SessionLocal()
        try:
            state = db.query(TMonitorState).filter(TMonitorState.id == state_id).first()
            if not state:
                return {"success": False, "error": "做T状态不存在"}
            position = db.query(Position).filter(Position.id == state.position_id).first()
            if not position:
                return {"success": False, "error": "持仓不存在"}
            ctx = dict(state.context or {})

            if action in {"long_open", "short_open"}:
                side = "long" if action == "long_open" else "short"
                if side == "long":
                    state.state = "waiting_exit"
                    target = round(price * (1 + min_profit), 4)
                    stop = round(price * (1 - max_stop), 4)
                else:
                    state.state = "waiting_buyback"
                    target = round(price * (1 - min_profit), 4)
                    stop = round(price * (1 + max_stop), 4)
                state.entry_price = price
                state.current_price = price
                state.target_price = target
                state.stop_loss_price = stop
                state.recommended_quantity = quantity
                state.signal_expires_at = None
                ctx.update(direction=side, leg_entry_price=price, leg_qty=quantity, manual=True)
                state.context = ctx
                db.commit()
                return {"success": True, "state": state.state, "target_price": target, "stop_loss_price": stop}

            # 平仓:long_close / short_close
            entry = float(ctx.get("leg_entry_price") or state.entry_price or 0.0)
            qty = quantity or int(ctx.get("leg_qty") or 0)
            if entry <= 0 or qty <= 0:
                return {"success": False, "error": "缺少开仓价或数量,无法计算盈亏"}
            realized = qty * (price - entry) if action == "long_close" else qty * (entry - price)
            realized = round(realized, 2)

            new_cost = None
            if position.quantity and position.quantity > 0:
                new_cost = (position.cost_price * position.quantity - realized) / position.quantity
                new_cost = round(max(new_cost, 0.0001), 4)
                position.cost_price = new_cost

            state.state = "completed"
            state.cycle_count = (state.cycle_count or 0) + 1
            state.current_price = price
            state.signal_expires_at = None
            ctx.update(last_realized=realized, close_price=price, close_qty=qty)
            state.context = ctx
            db.commit()
            return {"success": True, "state": state.state, "realized": realized, "new_cost_price": new_cost}
        except Exception as exc:
            db.rollback()
            return {"success": False, "error": str(exc)}
        finally:
            db.close()


ENGINE = TMonitorEngine()
