"""真实账户成交流水 API（手动录入，服务每日复盘）"""
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from src.web.database import get_db
from src.web.models import Account, TradeRecord

logger = logging.getLogger(__name__)
router = APIRouter()

VALID_DIRECTIONS = {"buy", "sell"}
VALID_MARKETS = {"CN", "HK", "US"}


# ========== Pydantic Models ==========

class TradeRecordCreate(BaseModel):
    account_id: int | None = None
    symbol: str
    market: str = "CN"
    name: str = ""
    direction: str  # buy / sell
    price: float
    quantity: int
    amount: float | None = None  # 缺省时按 price * quantity 计算
    traded_at: datetime | None = None  # 缺省为当前时间
    note: str = ""

    @field_validator("direction")
    @classmethod
    def _check_direction(cls, v: str) -> str:
        if v not in VALID_DIRECTIONS:
            raise ValueError(f"direction 必须为 {VALID_DIRECTIONS}")
        return v

    @field_validator("market")
    @classmethod
    def _check_market(cls, v: str) -> str:
        v = (v or "CN").upper()
        if v not in VALID_MARKETS:
            raise ValueError(f"market 必须为 {VALID_MARKETS}")
        return v


class TradeRecordUpdate(BaseModel):
    account_id: int | None = None
    symbol: str | None = None
    market: str | None = None
    name: str | None = None
    direction: str | None = None
    price: float | None = None
    quantity: int | None = None
    amount: float | None = None
    traded_at: datetime | None = None
    note: str | None = None


def _serialize(rec: TradeRecord, account_name: str | None = None) -> dict:
    return {
        "id": rec.id,
        "account_id": rec.account_id,
        "account_name": account_name,
        "symbol": rec.symbol,
        "market": rec.market,
        "name": rec.name or "",
        "direction": rec.direction,
        "price": rec.price,
        "quantity": rec.quantity,
        "amount": rec.amount,
        "traded_at": rec.traded_at.isoformat() if rec.traded_at else None,
        "note": rec.note or "",
    }


def _day_range(date_str: str) -> tuple[datetime, datetime]:
    try:
        start = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, f"日期格式错误: {date_str}，应为 YYYY-MM-DD")
    return start, start + timedelta(days=1)


# ========== Endpoints ==========

@router.get("")
def list_trades(
    date: str | None = None,
    account_id: int | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    """查询成交流水。date=YYYY-MM-DD 按天过滤，缺省返回最近记录。"""
    query = db.query(TradeRecord)
    if date:
        start, end = _day_range(date)
        query = query.filter(TradeRecord.traded_at >= start, TradeRecord.traded_at < end)
    if account_id:
        query = query.filter(TradeRecord.account_id == account_id)
    records = (
        query.order_by(TradeRecord.traded_at.desc(), TradeRecord.id.desc())
        .limit(max(1, min(limit, 500)))
        .all()
    )

    account_names = {a.id: a.name for a in db.query(Account).all()}
    return [_serialize(r, account_names.get(r.account_id)) for r in records]


@router.post("")
def create_trade(body: TradeRecordCreate, db: Session = Depends(get_db)):
    """录入一笔成交。"""
    if body.price <= 0 or body.quantity <= 0:
        raise HTTPException(400, "成交价与数量需大于 0")
    if body.account_id is not None:
        account = db.query(Account).filter(Account.id == body.account_id).first()
        if not account:
            raise HTTPException(404, f"账户 {body.account_id} 不存在")

    rec = TradeRecord(
        account_id=body.account_id,
        symbol=body.symbol.strip(),
        market=body.market,
        name=(body.name or "").strip(),
        direction=body.direction,
        price=body.price,
        quantity=body.quantity,
        amount=body.amount if body.amount is not None else round(body.price * body.quantity, 2),
        traded_at=body.traded_at or datetime.now(),
        note=(body.note or "").strip(),
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return _serialize(rec)


@router.put("/{trade_id}")
def update_trade(trade_id: int, body: TradeRecordUpdate, db: Session = Depends(get_db)):
    """修改一笔成交记录。"""
    rec = db.query(TradeRecord).filter(TradeRecord.id == trade_id).first()
    if not rec:
        raise HTTPException(404, "成交记录不存在")

    updates = body.model_dump(exclude_unset=True)
    if "direction" in updates and updates["direction"] not in VALID_DIRECTIONS:
        raise HTTPException(400, f"direction 必须为 {VALID_DIRECTIONS}")
    if "market" in updates:
        updates["market"] = str(updates["market"]).upper()
        if updates["market"] not in VALID_MARKETS:
            raise HTTPException(400, f"market 必须为 {VALID_MARKETS}")
    if "price" in updates and (updates["price"] is None or updates["price"] <= 0):
        raise HTTPException(400, "成交价需大于 0")
    if "quantity" in updates and (updates["quantity"] is None or updates["quantity"] <= 0):
        raise HTTPException(400, "数量需大于 0")

    for key, value in updates.items():
        setattr(rec, key, value)
    # 未显式给 amount 时，价量变动后自动重算
    if "amount" not in updates and ("price" in updates or "quantity" in updates):
        rec.amount = round(rec.price * rec.quantity, 2)

    db.commit()
    db.refresh(rec)
    return _serialize(rec)


@router.delete("/{trade_id}")
def delete_trade(trade_id: int, db: Session = Depends(get_db)):
    """删除一笔成交记录。"""
    rec = db.query(TradeRecord).filter(TradeRecord.id == trade_id).first()
    if not rec:
        raise HTTPException(404, "成交记录不存在")
    db.delete(rec)
    db.commit()
    return {"success": True}
