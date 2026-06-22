import asyncio
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.collectors.kline_collector import KlineCollector
from src.core.kline_service import fetch_kline_response_sync
from src.core.providers import ProviderRequest, get_kline_orchestrator
from src.core.signals.price_action import compute_price_action, price_action_params_from_dict
from src.core.strategy_catalog import get_strategy_profile_map
from src.models.market import MarketCode

router = APIRouter()
BATCH_CONCURRENCY = 5


class KlineItem(BaseModel):
    symbol: str = Field(..., description="股票代码")
    market: str = Field(..., description="市场: CN/HK/US")
    days: int | None = Field(default=60, description="K线天数")
    interval: str | None = Field(default="1d", description="周期: 1min/5min/1d/1w/1m")


class KlineBatchRequest(BaseModel):
    items: list[KlineItem]


class KlineSummaryItem(BaseModel):
    symbol: str = Field(..., description="股票代码")
    market: str = Field(..., description="市场: CN/HK/US")


class KlineSummaryBatchRequest(BaseModel):
    items: list[KlineSummaryItem]


def _parse_market(market: str) -> MarketCode:
    try:
        return MarketCode(market)
    except ValueError:
        raise HTTPException(400, f"不支持的市场: {market}")


def _serialize_klines(klines) -> list[dict]:
    return [
        {
            "date": k.date,
            "open": k.open,
            "close": k.close,
            "high": k.high,
            "low": k.low,
            "volume": k.volume,
            "amount": getattr(k, "amount", None),
            "source": getattr(k, "source", "") or "",
        }
        for k in klines
    ]


def _klines_meta(klines) -> dict:
    latest = None
    sources: list[str] = []
    for k in klines or []:
        date_value = getattr(k, "date", None)
        if date_value and (latest is None or str(date_value) > latest):
            latest = str(date_value)
        source = getattr(k, "source", "") or ""
        if source and source not in sources:
            sources.append(source)
    return {
        "data_as_of": latest,
        "source": ",".join(sources) if sources else None,
    }


def _aggregate_klines(klines, interval: str) -> list:
    """Aggregate daily klines to week/month."""

    iv = (interval or "1d").lower()
    if iv in ("1d", "day", "d"):
        return klines
    if iv not in ("1w", "1m", "week", "month", "w", "m"):
        return klines

    parsed = []
    for k in klines or []:
        try:
            dt = datetime.strptime(k.date, "%Y-%m-%d")
        except Exception:
            continue
        parsed.append((dt, k))

    parsed.sort(key=lambda x: x[0])
    buckets: dict[str, list] = {}
    for dt, k in parsed:
        if iv in ("1w", "week", "w"):
            y, w, _ = dt.isocalendar()
            key = f"{y:04d}-W{w:02d}"
        else:
            key = f"{dt.year:04d}-{dt.month:02d}"
        buckets.setdefault(key, []).append((dt, k))

    out = []
    for _, items in buckets.items():
        items.sort(key=lambda x: x[0])
        first = items[0][1]
        last = items[-1][1]
        high = max(it[1].high for it in items)
        low = min(it[1].low for it in items)
        vol = sum(it[1].volume for it in items)
        amounts = [getattr(it[1], "amount", None) for it in items]
        out.append(
            type(first)(
                date=items[-1][0].strftime("%Y-%m-%d"),
                open=first.open,
                close=last.close,
                high=high,
                low=low,
                volume=vol,
                amount=sum(amounts) if all(value is not None for value in amounts) else None,
                source=getattr(first, "source", "") or "",
            )
        )
    out.sort(key=lambda k: k.date)
    return out


def _is_intraday_interval(interval: str) -> bool:
    iv = (interval or "").lower()
    return iv in ("1min", "1minute", "minute", "min", "1", "5min", "5minute", "5m", "5")


def _normalize_intraday_interval(interval: str) -> str:
    iv = (interval or "").lower()
    if iv in ("5min", "5minute", "5m", "5"):
        return "5min"
    return "1min"


def _load_klines(collector: KlineCollector, symbol: str, days: int, interval: str):
    if _is_intraday_interval(interval):
        intraday_interval = _normalize_intraday_interval(interval)
        limit = min(max(int(days or 0), 60), 1200)
        return collector.get_intraday_klines(
            symbol,
            interval=intraday_interval,
            limit=limit,
        )
    klines = collector.get_klines(symbol, days=days)
    return _aggregate_klines(klines, interval)


def _load_klines_from_orchestrator(symbol: str, market: MarketCode, days: int, interval: str):
    if _is_intraday_interval(interval):
        normalized_interval = _normalize_intraday_interval(interval)
        fetch_days = min(max(int(days or 0), 60), 1200)
        req_interval = normalized_interval
        ttl = 45
    else:
        fetch_days = int(days or 60)
        req_interval = "1d"
        ttl = 60

    resp = fetch_kline_response_sync(
        symbol,
        market,
        days=fetch_days,
        interval=req_interval,
        cache_ttl_sec=ttl,
    )
    if not resp.success:
        raise HTTPException(502, resp.error or "K线数据源请求失败")
    klines = resp.data or []
    if _is_intraday_interval(interval):
        return klines
    return _aggregate_klines(klines, interval)


async def _load_klines_from_orchestrator_async(symbol: str, market: MarketCode, days: int, interval: str):
    if _is_intraday_interval(interval):
        normalized_interval = _normalize_intraday_interval(interval)
        fetch_days = min(max(int(days or 0), 60), 1200)
        req_interval = normalized_interval
        ttl = 45
    else:
        fetch_days = int(days or 60)
        req_interval = "1d"
        ttl = 60

    req = ProviderRequest(
        symbols=(symbol,),
        market=market.value,
        extra=(("days", fetch_days), ("interval", req_interval)),
    )
    resp = await get_kline_orchestrator().fetch(req, cache_ttl_sec=ttl)
    if not resp.success:
        raise HTTPException(502, resp.error or "K线数据源请求失败")
    klines = resp.data or []
    if _is_intraday_interval(interval):
        return klines
    return _aggregate_klines(klines, interval)


@router.get("/{symbol}")
def get_klines(symbol: str, market: str = "CN", days: int = 60, interval: str = "1d"):
    """获取单只股票K线数据"""
    market_code = _parse_market(market)
    klines = _load_klines_from_orchestrator(symbol, market_code, days, interval)
    meta = _klines_meta(klines)
    return {
        "symbol": symbol,
        "market": market_code.value,
        "days": days,
        "interval": _normalize_intraday_interval(interval) if _is_intraday_interval(interval) else interval,
        **meta,
        "klines": _serialize_klines(klines),
    }


@router.post("/batch")
async def get_klines_batch(payload: KlineBatchRequest):
    """批量获取K线数据"""
    if not payload.items:
        return []

    semaphore = asyncio.Semaphore(BATCH_CONCURRENCY)

    async def _one(item: KlineItem):
        market_code = _parse_market(item.market)
        days = item.days or 60
        interval = item.interval or "1d"
        async with semaphore:
            klines = await _load_klines_from_orchestrator_async(item.symbol, market_code, days, interval)
        meta = _klines_meta(klines)
        return {
            "symbol": item.symbol,
            "market": market_code.value,
            "days": days,
            "interval": _normalize_intraday_interval(interval) if _is_intraday_interval(interval) else interval,
            **meta,
            "klines": _serialize_klines(klines),
        }

    return await asyncio.gather(*[_one(item) for item in payload.items])


@router.get("/{symbol}/summary")
def get_kline_summary(symbol: str, market: str = "CN"):
    """获取单只股票K线摘要"""
    market_code = _parse_market(market)
    collector = KlineCollector(market_code)
    summary = collector.get_kline_summary(symbol)
    return {
        "symbol": symbol,
        "market": market_code.value,
        "summary": summary,
    }


@router.get("/{symbol}/price-action")
def get_price_action(symbol: str, market: str = "CN", days: int = 180):
    """Return PA signals, levels and chart markers for a stock."""
    market_code = _parse_market(market)
    profile = get_strategy_profile_map().get("price_action") or {}
    params = price_action_params_from_dict(profile.get("params"))
    klines = _load_klines_from_orchestrator(
        symbol,
        market_code,
        max(120, min(int(days or 180), 600)),
        "1d",
    )
    result = compute_price_action(klines, params=params).to_dict()
    return {
        "symbol": symbol,
        "market": market_code.value,
        "enabled": bool(profile.get("enabled", True)),
        **result,
    }


@router.post("/summary/batch")
async def get_kline_summary_batch(payload: KlineSummaryBatchRequest):
    """批量获取K线摘要"""
    if not payload.items:
        return []

    semaphore = asyncio.Semaphore(BATCH_CONCURRENCY)

    async def _one(item: KlineSummaryItem):
        market_code = _parse_market(item.market)
        async with semaphore:
            summary = await asyncio.to_thread(
                KlineCollector(market_code).get_kline_summary,
                item.symbol,
            )
        return {
            "symbol": item.symbol,
            "market": market_code.value,
            "summary": summary,
        }

    return await asyncio.gather(*[_one(item) for item in payload.items])
