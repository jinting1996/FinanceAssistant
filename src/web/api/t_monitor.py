"""底仓 VWAP 做 T 盯盘 API。"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.core.t_monitor_engine import ENGINE
from src.web.database import get_db
from src.web.models import Position, Stock, TMonitorState, TSignalEvent

router = APIRouter()


def _state_dict(row: TMonitorState, position: Position, stock: Stock) -> dict:
    return {
        "id": row.id,
        "position_id": row.position_id,
        "trade_date": row.trade_date,
        "state": row.state,
        "cycle_count": row.cycle_count,
        "score": row.score,
        "recommended_quantity": row.recommended_quantity,
        "entry_price": row.entry_price,
        "current_price": row.current_price,
        "vwap": row.vwap,
        "support_price": row.support_price,
        "stop_loss_price": row.stop_loss_price,
        "target_price": row.target_price,
        "signal_expires_at": row.signal_expires_at,
        "context": row.context or {},
        "stock_symbol": stock.symbol,
        "stock_name": stock.name,
        "sellable_quantity": position.sellable_quantity,
        "updated_at": row.updated_at,
    }


T_STRATEGY_CODE = "base_position_vwap_t"


@router.get("/params")
def get_params() -> dict:
    """读取做T策略当前参数。"""
    from src.core.strategy_catalog import get_strategy_params

    return get_strategy_params(T_STRATEGY_CODE)


class TParamsUpdate(BaseModel):
    direction: str | None = None        # both / long / short
    exit_mode: str | None = None        # price / trail / price_or_score
    min_score: int | None = None
    position_ratio: float | None = None
    max_cycles_per_day: int | None = None
    cycle_cooldown_minutes: int | None = None
    trail_pct: float | None = None
    profit_atr_mult: float | None = None
    stop_atr_mult: float | None = None
    min_profit_pct: float | None = None
    max_stop_pct: float | None = None


@router.put("/params")
def put_params(payload: TParamsUpdate) -> dict:
    """更新做T策略参数(仅合并提供的字段,带基本校验)。"""
    from src.core.strategy_catalog import update_strategy_params

    raw = payload.model_dump(exclude_none=True)
    if not raw:
        raise HTTPException(400, "没有可更新的参数")
    partial: dict = {}
    if "direction" in raw:
        if raw["direction"] not in ("both", "long", "short"):
            raise HTTPException(400, "direction 仅支持 both/long/short")
        partial["direction"] = raw["direction"]
    if "exit_mode" in raw:
        if raw["exit_mode"] not in ("price", "trail", "price_or_score"):
            raise HTTPException(400, "exit_mode 仅支持 price/trail/price_or_score")
        partial["exit_mode"] = raw["exit_mode"]
    if "min_score" in raw:
        partial["min_score"] = max(0, min(100, int(raw["min_score"])))
    if "position_ratio" in raw:
        partial["position_ratio"] = max(0.0, min(1.0, float(raw["position_ratio"])))
    if "max_cycles_per_day" in raw:
        partial["max_cycles_per_day"] = max(0, int(raw["max_cycles_per_day"]))
    if "cycle_cooldown_minutes" in raw:
        partial["cycle_cooldown_minutes"] = max(0, int(raw["cycle_cooldown_minutes"]))
    for key in ("trail_pct", "profit_atr_mult", "stop_atr_mult", "min_profit_pct", "max_stop_pct"):
        if key in raw:
            partial[key] = max(0.0, float(raw[key]))
    return update_strategy_params(T_STRATEGY_CODE, partial)


@router.get("/states")
def list_states(db: Session = Depends(get_db)) -> list[dict]:
    rows = (
        db.query(TMonitorState, Position, Stock)
        .join(Position, TMonitorState.position_id == Position.id)
        .join(Stock, Position.stock_id == Stock.id)
        .order_by(TMonitorState.trade_date.desc(), TMonitorState.score.desc())
        .limit(200)
        .all()
    )
    latest: dict[int, dict] = {}
    for state, position, stock in rows:
        latest.setdefault(position.id, _state_dict(state, position, stock))
    return list(latest.values())


@router.get("/events")
def list_events(position_id: int | None = None, limit: int = 50, db: Session = Depends(get_db)) -> list[dict]:
    query = db.query(TSignalEvent)
    if position_id is not None:
        query = query.filter(TSignalEvent.position_id == position_id)
    rows = query.order_by(TSignalEvent.created_at.desc()).limit(min(max(limit, 1), 200)).all()
    return [
        {
            "id": row.id,
            "position_id": row.position_id,
            "signal_id": row.signal_id,
            "trade_date": row.trade_date,
            "action": row.action,
            "score": row.score,
            "current_price": row.current_price,
            "vwap": row.vwap,
            "support_price": row.support_price,
            "stop_loss_price": row.stop_loss_price,
            "target_price": row.target_price,
            "recommended_quantity": row.recommended_quantity,
            "reason": row.reason,
            "notify_success": row.notify_success,
            "notify_error": row.notify_error,
            "created_at": row.created_at,
        }
        for row in rows
    ]


@router.post("/scan")
async def scan(position_id: int | None = None) -> dict:
    return await ENGINE.scan_once(position_id=position_id, bypass_market_hours=True)


@router.post("/states/{state_id}/manual")
async def manual_action(state_id: int, action: str) -> dict:
    """手动驱动状态机:mark_long_open / mark_short_open / mark_done / reset。"""
    result = await ENGINE.manual_action(state_id, action)
    if not result.get("success"):
        raise HTTPException(400, result.get("error") or "操作失败")
    return result


class ExecuteLegRequest(BaseModel):
    action: str  # long_open / short_open / long_close / short_close
    price: float
    quantity: int = 0


@router.post("/states/{state_id}/execute")
async def execute_leg(state_id: int, payload: ExecuteLegRequest) -> dict:
    """记录一腿实际成交(价+量);平仓时摊低持仓成本。"""
    result = await ENGINE.execute_leg(state_id, payload.action, payload.price, payload.quantity)
    if not result.get("success"):
        raise HTTPException(400, result.get("error") or "操作失败")
    return result


@router.post("/states/{state_id}/confirm-buy")
def confirm_buy(state_id: int, db: Session = Depends(get_db)) -> dict:
    state = db.query(TMonitorState).filter(TMonitorState.id == state_id).first()
    if not state:
        raise HTTPException(404, "做T状态不存在")
    from src.core.t_monitor_engine import _now

    if state.state == "buy_t_notified":
        # 正T:确认低吸买入,进入等待卖出
        if state.signal_expires_at and state.signal_expires_at < _now():
            state.state = "invalidated"
            db.commit()
            raise HTTPException(400, "低吸信号已过期，请等待下一次有效信号")
        state.state = "waiting_exit"
        db.commit()
        return {"success": True, "state": state.state}
    if state.state == "buy_back_notified":
        # 倒T:确认买回平仓,本轮完成
        state.state = "completed"
        state.cycle_count += 1
        db.commit()
        return {"success": True, "state": state.state, "cycle_count": state.cycle_count}
    raise HTTPException(400, "当前状态不能确认买入")


@router.post("/states/{state_id}/confirm-sell")
def confirm_sell(state_id: int, db: Session = Depends(get_db)) -> dict:
    state = db.query(TMonitorState).filter(TMonitorState.id == state_id).first()
    if not state:
        raise HTTPException(404, "做T状态不存在")
    from src.core.t_monitor_engine import _now

    if state.state == "sell_t_notified":
        # 正T:确认止盈卖出,本轮完成
        state.state = "completed"
        state.cycle_count += 1
        db.commit()
        return {"success": True, "state": state.state, "cycle_count": state.cycle_count}
    if state.state == "sell_open_notified":
        # 倒T:确认高抛开仓,进入等待买回
        if state.signal_expires_at and state.signal_expires_at < _now():
            state.state = "invalidated"
            db.commit()
            raise HTTPException(400, "高抛信号已过期，请等待下一次有效信号")
        state.state = "waiting_buyback"
        db.commit()
        return {"success": True, "state": state.state}
    raise HTTPException(400, "当前状态不能确认卖出")
