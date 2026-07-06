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
    _atr,
    _exclude_today,
    cn_price_limit_ratio,
    compute_base_position_vwap_t,
    compute_base_position_vwap_t_short,
    evaluate_t_exit,
    evaluate_t_exit_short,
)

# 倒T(先卖后买)相关的动作与状态
_SHORT_ACTIONS = {"sell_open", "buy_back", "buy_back_stop"}
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
            "sell_t_stop": "做T卖出提醒(止损)",
            "buy_back_stop": "做T买回提醒(止损)",
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
        # 同一信号已记录过则直接复用,避免 signal_id 唯一约束在 flush 处抛 IntegrityError
        # 把整轮扫描回滚(同样会导致该持仓现价冻结)。同信号也无需重复通知。
        existing = db.query(TSignalEvent).filter(TSignalEvent.signal_id == signal_id).first()
        if existing is not None:
            state.last_signal_id = signal_id
            return existing
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
        # 通知是副作用:发送失败只记录到事件,绝不能让异常冒泡。否则会被
        # scan_once 的 except 捕获并 db.rollback(),把这只持仓本轮扫描(含现价/
        # 评分刷新、状态流转)整体回滚——表现为"唯独有信号的那只持仓现价永久冻结"。
        try:
            await self._notify(db, event, stock)
        except Exception as exc:  # noqa: BLE001 - 通知失败不可影响盯盘事务
            event.notify_success = False
            event.notify_error = str(exc)[:500]
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
            asyncio.to_thread(fetch_klines_sync, stock.symbol, "CN", days=320, interval="1min", cache_ttl_sec=5),
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
        # 先无条件刷新现价:下方多个状态分支会提前 return(到达次数上限/已触发信号待确认/
        # 失效/完成冷却中),若不在此统一更新,这些持仓的现价会一直冻结在上次入场时的价。
        if current > 0:
            state.current_price = current
        thresholds = dict(
            min_score=int(params.get("min_score", 70)),
            min_vwap_deviation_pct=float(params.get("min_vwap_deviation_pct", 0.003)),
            min_profit_pct=float(params.get("min_profit_pct", 0.008)),
            max_stop_pct=float(params.get("max_stop_pct", 0.015)),
            profit_atr_mult=float(params.get("profit_atr_mult", 0.5)),
            stop_atr_mult=float(params.get("stop_atr_mult", 0.5)),
            # 涨跌停闸门:按板块算涨跌停比例,信号据此结合位置/量价拦截高抛/低吸
            limit_ratio=cn_price_limit_ratio(stock.symbol, getattr(stock, "name", "") or ""),
        )
        # 离场方式:price=仅固定价触发 / price_or_score=价格或评分任一 / trail=跟踪止盈(尽量多吃)
        exit_mode = str(params.get("exit_mode", "price")).lower()
        score_exit = exit_mode == "price_or_score"
        trail_mode = exit_mode == "trail"
        trail_pct = max(0.0, float(params.get("trail_pct", 0.003)))
        min_profit = thresholds["min_profit_pct"]

        # 与现价同理:tscore 也只在 idle/离场态写入,其余提前 return 的分支
        # (次数上限/*_notified/invalidated/completed冷却)会让 score 冻结。
        # 这里按方向先算一个"当前做T质量分"兜底刷新;idle/离场分支会用各自更
        # 精确的分数覆盖。score 是离散打分,反映 setup 质量而非价格高低。
        _direction = str(params.get("direction", "both") or "both").lower()
        _disp_sig: Any = None
        _disp_side = "long"
        if _direction in {"both", "long"}:
            _disp_sig = compute_base_position_vwap_t(daily, minute, **thresholds)
        if _direction in {"both", "short"}:
            _short_sig = compute_base_position_vwap_t_short(daily, minute, **thresholds)
            if _disp_sig is None or _short_sig.score > _disp_sig.score:
                _disp_sig = _short_sig
                _disp_side = "short"
        if _disp_sig is not None:
            state.score = _disp_sig.score
            if _disp_sig.vwap:
                state.vwap = _disp_sig.vwap
            state.context = {
                **(state.context or {}),
                "score_detail": _disp_sig.score_detail,
                "score_side": _disp_side,
            }

        # --- 离场态:实时重算"离场质量分"(平仓本质是反向入场) ---
        if state.state == "waiting_exit":
            # 正T 卖出 = 高抛质量(反向 short 入场分)
            price_hit = evaluate_t_exit(
                current,
                vwap=float(state.vwap or current),
                target_price=float(state.target_price or current),
                stop_loss_price=float(state.stop_loss_price or 0),
            )
            sell_sig = compute_base_position_vwap_t_short(daily, minute, **thresholds)
            entry = float(state.entry_price or current)
            ctx = {
                **(state.context or {}),
                "exit_score": sell_sig.score,
                "exit_reason": sell_sig.reason,
                "score_detail": sell_sig.score_detail,
                "score_side": "short",
            }
            # 跟踪止盈:进入盈利区后记录最高价,自高点回落 trail_pct 才卖
            trail_hit = False
            if trail_mode:
                eff_profit = float(sell_sig.metrics.get("eff_profit_pct") or min_profit)
                in_profit = current >= entry * (1 + eff_profit)
                peak = float(ctx.get("extreme_price") or current)
                if in_profit:
                    peak = max(peak, current)
                    ctx["extreme_price"] = peak
                    trail_hit = current <= peak * (1 - trail_pct)
            state.current_price = current
            state.score = sell_sig.score
            state.context = ctx
            if price_hit == "invalidated":
                state.state = "invalidated"
                event = await self._create_event(db, state, position, stock, "sell_t_stop", "触及止损位,止损卖出(认亏离场)")
                return {"position_id": position.id, "status": "invalidated", "event_id": event.id}
            do_close = trail_hit if trail_mode else (price_hit == "sell_t" or (score_exit and sell_sig.action == "sell_open"))
            if do_close:
                state.state = "sell_t_notified"
                if trail_mode:
                    reason = f"跟踪止盈:自高点 {ctx.get('extreme_price')} 回落 {trail_pct:.1%}"
                elif price_hit == "sell_t":
                    reason = "价格到达止盈目标位(含ATR自适应)"
                else:
                    reason = f"高抛质量达标(分{sell_sig.score}):{sell_sig.reason}"
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
            entry = float(state.entry_price or current)
            ctx = {
                **(state.context or {}),
                "buyback_score": buy_sig.score,
                "buyback_reason": buy_sig.reason,
                "score_detail": buy_sig.score_detail,
                "score_side": "long",
            }
            # 跟踪止盈:进入盈利区后记录最低价,自低点反弹 trail_pct 才买回
            trail_hit = False
            if trail_mode:
                eff_profit = float(buy_sig.metrics.get("eff_profit_pct") or min_profit)
                in_profit = current <= entry * (1 - eff_profit)
                trough = float(ctx.get("extreme_price") or current)
                if in_profit:
                    trough = min(trough, current)
                    ctx["extreme_price"] = trough
                    trail_hit = current >= trough * (1 + trail_pct)
            state.current_price = current
            state.score = buy_sig.score
            state.context = ctx
            if price_hit == "invalidated":
                state.state = "invalidated"
                event = await self._create_event(db, state, position, stock, "buy_back_stop", "触及止损位,止损买回(认亏离场)")
                return {"position_id": position.id, "status": "invalidated", "event_id": event.id}
            do_close = trail_hit if trail_mode else (price_hit == "buy_back" or (score_exit and buy_sig.action == "buy_t"))
            if do_close:
                state.state = "buy_back_notified"
                if trail_mode:
                    reason = f"跟踪止盈:自低点 {ctx.get('extreme_price')} 反弹 {trail_pct:.1%}"
                elif price_hit == "buy_back":
                    reason = "价格回落到买回目标位(含ATR自适应)"
                else:
                    reason = f"低吸质量达标(分{buy_sig.score}):{buy_sig.reason}"
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
        position_ratio = min(max(float(params.get("position_ratio", self.position_ratio)), 0.0), 1.0)
        sellable = max(int(position.sellable_quantity if position.sellable_quantity is not None else position.quantity), 0)

        candidates: list[tuple[str, str, Any, int]] = []  # (side, action, signal, recommended)
        display_signal: Any = None
        display_side = "long"
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
                display_side = "short"
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
                context["score_side"] = display_side
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
        context["score_side"] = side
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
                asyncio.to_thread(fetch_klines_sync, stock.symbol, "CN", days=320, interval="1min", cache_ttl_sec=5),
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
        profit_atr_mult = float(params.get("profit_atr_mult", 0.5))
        stop_atr_mult = float(params.get("stop_atr_mult", 0.5))

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
                # 止盈/止损按 ATR 自适应(地板与 ATR 倍数取大),与自动信号一致
                eff_profit, eff_stop = min_profit, max_stop
                try:
                    stock = db.query(Stock).filter(Stock.id == position.stock_id).first()
                    if stock:
                        daily = await asyncio.to_thread(
                            fetch_klines_sync, stock.symbol, "CN", days=120, interval="1d", cache_ttl_sec=60
                        )
                        atr = _atr(_exclude_today(daily, _now().strftime("%Y-%m-%d")), 14)
                        if atr and price > 0:
                            ratio = atr / price
                            eff_profit = max(min_profit, profit_atr_mult * ratio)
                            eff_stop = max(max_stop, stop_atr_mult * ratio)
                except Exception:
                    pass
                if side == "long":
                    state.state = "waiting_exit"
                    target = round(price * (1 + eff_profit), 4)
                    stop = round(price * (1 - eff_stop), 4)
                else:
                    state.state = "waiting_buyback"
                    target = round(price * (1 - eff_profit), 4)
                    stop = round(price * (1 + eff_stop), 4)
                state.entry_price = price
                state.current_price = price
                state.target_price = target
                state.stop_loss_price = stop
                state.recommended_quantity = quantity
                state.signal_expires_at = None
                ctx.pop("extreme_price", None)  # 新开一轮,清掉上一轮的跟踪极值
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
