"""底仓 VWAP 做 T 盯盘 API。"""

from fastapi import APIRouter, Depends, HTTPException
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


@router.post("/states/{state_id}/confirm-buy")
def confirm_buy(state_id: int, db: Session = Depends(get_db)) -> dict:
    state = db.query(TMonitorState).filter(TMonitorState.id == state_id).first()
    if not state:
        raise HTTPException(404, "做T状态不存在")
    if state.state != "buy_t_notified":
        raise HTTPException(400, "当前状态不能确认买入")
    from src.core.t_monitor_engine import _now

    if state.signal_expires_at and state.signal_expires_at < _now():
        state.state = "invalidated"
        db.commit()
        raise HTTPException(400, "低吸信号已过期，请等待下一次有效信号")
    state.state = "waiting_exit"
    db.commit()
    return {"success": True, "state": state.state}


@router.post("/states/{state_id}/confirm-sell")
def confirm_sell(state_id: int, db: Session = Depends(get_db)) -> dict:
    state = db.query(TMonitorState).filter(TMonitorState.id == state_id).first()
    if not state:
        raise HTTPException(404, "做T状态不存在")
    if state.state != "sell_t_notified":
        raise HTTPException(400, "当前状态不能确认卖出")
    state.state = "completed"
    state.cycle_count += 1
    db.commit()
    return {"success": True, "state": state.state, "cycle_count": state.cycle_count}
