from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Protocol

from sqlalchemy.orm import Session

from src.collectors.kline_collector import KlineData
from src.core.providers import ProviderRequest, get_discovery_orchestrator, get_kline_orchestrator
from src.web.models import Stock, WatchedBoard

logger = logging.getLogger(__name__)


@dataclass
class ScreenerStock:
    symbol: str
    market: str = "CN"
    name: str = ""
    board_code: str = ""
    board_name: str = ""
    sources: list[str] = field(default_factory=list)


class ScreenerDataProvider(Protocol):
    name: str

    def resolve_universe(self, db: Session, config: dict, *, limit: int) -> list[ScreenerStock]:
        ...

    def fetch_klines(self, stock: ScreenerStock, *, days: int) -> list[KlineData]:
        ...

    def fetch_quotes(self, stocks: list[ScreenerStock]) -> dict[str, dict]:
        ...


def normalize_universe_config(config: dict | None) -> dict:
    raw = config if isinstance(config, dict) else {}
    include_watchlist = bool(raw.get("include_watchlist", True))
    include_watched_boards = bool(raw.get("include_watched_boards", True))
    board_codes = raw.get("board_codes") if isinstance(raw.get("board_codes"), list) else []
    board_codes = [str(x).strip().upper() for x in board_codes if str(x).strip()]
    max_symbols = int(raw.get("max_symbols") or 300)
    return {
        "market": "CN",
        "provider": str(raw.get("provider") or "panwatch").strip() or "panwatch",
        "include_watchlist": include_watchlist,
        "include_watched_boards": include_watched_boards,
        "board_codes": board_codes,
        "max_symbols": max(1, min(max_symbols, 300)),
        "days": max(30, min(int(raw.get("days") or 120), 500)),
    }


class PanWatchScreenerDataProvider:
    name = "panwatch"

    def resolve_universe(self, db: Session, config: dict, *, limit: int) -> list[ScreenerStock]:
        cfg = normalize_universe_config(config)
        rows: dict[str, ScreenerStock] = {}

        if cfg["include_watchlist"]:
            stocks = (
                db.query(Stock)
                .filter(Stock.market == "CN")
                .order_by(Stock.sort_order.asc(), Stock.id.asc())
                .all()
            )
            for s in stocks:
                self._upsert(
                    rows,
                    ScreenerStock(
                        symbol=str(s.symbol or "").strip(),
                        market="CN",
                        name=str(s.name or "").strip(),
                        sources=["watchlist"],
                    ),
                )
                if len(rows) >= limit:
                    return list(rows.values())[:limit]

        if cfg["include_watched_boards"]:
            query = db.query(WatchedBoard).filter(
                WatchedBoard.market == "CN",
                WatchedBoard.enabled == True,  # noqa: E712
            )
            selected_codes = set(cfg["board_codes"])
            if selected_codes:
                query = query.filter(WatchedBoard.board_code.in_(selected_codes))
            boards = query.order_by(WatchedBoard.sort_order.asc(), WatchedBoard.id.asc()).all()
            orchestrator = get_discovery_orchestrator()
            per_board_limit = max(20, min(80, limit))
            for board in boards:
                if len(rows) >= limit:
                    break
                try:
                    resp = orchestrator.fetch_sync(
                        ProviderRequest(
                            market="CN",
                            extra=(
                                ("kind", "board_stocks"),
                                ("board_code", board.board_code),
                                ("mode", "turnover"),
                                ("limit", per_board_limit),
                            ),
                        ),
                        cache_ttl_sec=60,
                    )
                    items = resp.data if resp.success else []
                    if not resp.success:
                        raise RuntimeError(resp.error or "empty board stocks")
                except Exception as e:
                    logger.warning("fetch board stocks failed for %s: %s", board.board_code, e)
                    continue
                for it in items:
                    symbol = self._item_value(it, "symbol")
                    name = self._item_value(it, "name")
                    self._upsert(
                        rows,
                        ScreenerStock(
                            symbol=str(symbol or "").strip(),
                            market="CN",
                            name=str(name or "").strip(),
                            board_code=str(board.board_code or "").strip(),
                            board_name=str(board.board_name or "").strip(),
                            sources=["watched_board"],
                        ),
                    )
                    if len(rows) >= limit:
                        break

        return list(rows.values())[:limit]

    def _item_value(self, item, key: str):
        if isinstance(item, dict):
            return item.get(key)
        return getattr(item, key, None)

    def _upsert(self, rows: dict[str, ScreenerStock], stock: ScreenerStock) -> None:
        if not stock.symbol:
            return
        key = f"{stock.market}:{stock.symbol}"
        existing = rows.get(key)
        if not existing:
            rows[key] = stock
            return
        if not existing.name and stock.name:
            existing.name = stock.name
        if not existing.board_code and stock.board_code:
            existing.board_code = stock.board_code
            existing.board_name = stock.board_name
        for source in stock.sources:
            if source not in existing.sources:
                existing.sources.append(source)

    def fetch_klines(self, stock: ScreenerStock, *, days: int) -> list[KlineData]:
        try:
            resp = get_kline_orchestrator().fetch_sync(
                ProviderRequest(
                    symbols=(stock.symbol,),
                    market=stock.market or "CN",
                    extra=(("days", int(days or 120)),),
                ),
                cache_ttl_sec=60,
            )
            return resp.data if resp.success and resp.data else []
        except Exception as e:
            logger.warning("fetch screener kline failed for %s: %s", stock.symbol, e)
            return []

    def fetch_quotes(self, stocks: list[ScreenerStock]) -> dict[str, dict]:
        return {}


class UnconfiguredScreenerDataProvider:
    def __init__(self, name: str):
        self.name = name

    def resolve_universe(self, db: Session, config: dict, *, limit: int) -> list[ScreenerStock]:
        raise RuntimeError(f"{self.name} provider 未配置")

    def fetch_klines(self, stock: ScreenerStock, *, days: int) -> list[KlineData]:
        return []

    def fetch_quotes(self, stocks: list[ScreenerStock]) -> dict[str, dict]:
        return {}


def get_screener_provider(name: str | None) -> ScreenerDataProvider:
    key = (name or "panwatch").strip().lower()
    if key == "panwatch":
        return PanWatchScreenerDataProvider()
    if key in {"ifind", "tdx_mcp"}:
        return UnconfiguredScreenerDataProvider(key)
    return PanWatchScreenerDataProvider()
