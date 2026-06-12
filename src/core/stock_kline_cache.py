"""SQLite-backed daily stock K-line cache."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from src.collectors.kline_collector import KlineData
from src.web.database import SessionLocal
from src.web.models import StockKlineCache


def _parse_day(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _row_to_kline(row: StockKlineCache) -> KlineData:
    return KlineData(
        date=row.date,
        open=float(row.open),
        close=float(row.close),
        high=float(row.high),
        low=float(row.low),
        volume=float(row.volume or 0),
        source=row.source or "sqlite",
    )


def _value(kline, field: str, default=0):
    if isinstance(kline, dict):
        return kline.get(field, default)
    return getattr(kline, field, default)


def _kline_date(kline) -> str:
    return str(_value(kline, "date", "") or "")[:10]


def load_cached_daily_klines(
    market: str,
    symbol: str,
    *,
    days: int,
    max_stale_days: int = 5,
    db=None,
    today: date | None = None,
) -> tuple[list[KlineData], bool]:
    """Return cached klines and whether they satisfy the request."""

    session = db or SessionLocal()
    close_session = db is None
    try:
        rows = (
            session.query(StockKlineCache)
            .filter(
                StockKlineCache.market == market,
                StockKlineCache.symbol == symbol,
            )
            .order_by(StockKlineCache.date.desc())
            .limit(max(1, int(days or 1)))
            .all()
        )
        rows = list(reversed(rows))
        out = [_row_to_kline(row) for row in rows]
        if len(out) < max(1, int(days or 1)):
            return out, False
        latest = _parse_day(out[-1].date)
        current = today or date.today()
        if latest is None:
            return out, False
        return out, (current - latest) <= timedelta(days=max_stale_days)
    finally:
        if close_session:
            session.close()


def calculate_increment_days(
    cached: list[KlineData],
    requested_days: int,
    *,
    today: date | None = None,
) -> int:
    """Compute a small fetch window that overlaps cached data for adjustment checks."""

    if not cached:
        return max(1, int(requested_days or 1))
    latest = _parse_day(cached[-1].date)
    if latest is None:
        return max(1, int(requested_days or 1))
    current = today or date.today()
    delta_days = max(0, (current - latest).days)
    return min(max(delta_days + 10, 30), max(1, int(requested_days or 1)))


def upsert_daily_klines(
    market: str,
    symbol: str,
    klines: list[KlineData],
    *,
    db=None,
) -> dict[str, int | bool]:
    """Upsert fetched daily klines, clearing existing rows if adjusted data drift is detected."""

    session = db or SessionLocal()
    close_session = db is None
    try:
        existing = {
            row.date: row
            for row in (
                session.query(StockKlineCache)
                .filter(
                    StockKlineCache.market == market,
                    StockKlineCache.symbol == symbol,
                )
                .all()
            )
        }
        reset = False
        for k in klines or []:
            k_date = _kline_date(k)
            row = existing.get(k_date)
            if not row:
                continue
            old_close = float(row.close or 0)
            new_close = float(_value(k, "close", 0) or 0)
            if old_close and abs(old_close - new_close) / old_close > 0.001:
                reset = True
                break

        if reset:
            session.query(StockKlineCache).filter(
                StockKlineCache.market == market,
                StockKlineCache.symbol == symbol,
            ).delete(synchronize_session=False)
            existing = {}

        upserted = 0
        for k in klines or []:
            k_date = _kline_date(k)
            if not _parse_day(k_date):
                continue
            row = existing.get(k_date)
            if row:
                row.open = float(_value(k, "open", 0) or 0)
                row.high = float(_value(k, "high", 0) or 0)
                row.low = float(_value(k, "low", 0) or 0)
                row.close = float(_value(k, "close", 0) or 0)
                row.volume = float(_value(k, "volume", 0) or 0)
                row.source = _value(k, "source", "") or row.source or ""
            else:
                session.add(
                    StockKlineCache(
                        market=market,
                        symbol=symbol,
                        date=k_date,
                        open=float(_value(k, "open", 0) or 0),
                        high=float(_value(k, "high", 0) or 0),
                        low=float(_value(k, "low", 0) or 0),
                        close=float(_value(k, "close", 0) or 0),
                        volume=float(_value(k, "volume", 0) or 0),
                        source=_value(k, "source", "") or "",
                    )
                )
            upserted += 1

        session.commit()
        return {"upserted": upserted, "reset": reset}
    except Exception:
        session.rollback()
        raise
    finally:
        if close_session:
            session.close()
