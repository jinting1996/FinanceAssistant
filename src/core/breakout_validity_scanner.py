"""突破有效性策略:A股主板全市场扫描 + 模拟盘持仓失效监控。

买点:状态=valid_active 当日写入 buy 信号(模拟盘按入场区间自动建仓);
卖点:止损价=max(G_SUPPORT,L0)、止盈价=G0×1.15 由模拟盘引擎盯价;
     动态失效(连续3日<G0/放量破位/跌停破位)由每日收盘后的持仓复评写 sell 信号强平。
扫描范围:沪深主板(60/000/001/002/003 开头),剔除创业板/科创板/北交所/ST/退市。
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any

from src.core.kline_service import fetch_klines_sync
from src.core.signals.breakout_validity import (
    BreakoutParams,
    BreakoutValidityResult,
    compute_breakout_validity,
)
from src.web.database import SessionLocal
from src.web.models import PaperTradingPosition, StrategySignalRun
from src.web.stock_list import get_stock_list

logger = logging.getLogger(__name__)

STRATEGY_CODE = "breakout_validity"
STRATEGY_NAME = "突破有效性"

MAIN_BOARD_PREFIXES = ("60", "000", "001", "002", "003")
KLINE_DAYS = 200          # 60日前高回看 + 均线/量能余量
SCAN_MAX_WORKERS = 8
KLINE_CACHE_TTL = 3600.0


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def build_universe() -> list[dict]:
    """沪深主板股票清单(剔除双创/北交所在前缀层面天然排除,再剔除 ST/退市)。"""
    out = []
    for s in get_stock_list():
        symbol = str(s.get("symbol") or "")
        name = str(s.get("name") or "")
        if s.get("market") not in (None, "CN", "cn"):
            continue
        if not symbol.startswith(MAIN_BOARD_PREFIXES):
            continue
        if "ST" in name.upper() or "退" in name:
            continue
        out.append({"symbol": symbol, "name": name})
    return out


def _evaluate(symbol: str, params: BreakoutParams) -> BreakoutValidityResult | None:
    try:
        klines = fetch_klines_sync(symbol, "CN", days=KLINE_DAYS, interval="1d", cache_ttl_sec=KLINE_CACHE_TTL)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[突破有效性] %s 拉取K线失败: %s", symbol, exc)
        return None
    if not klines:
        return None
    try:
        return compute_breakout_validity(klines, params=params)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[突破有效性] %s 计算异常: %s", symbol, exc)
        return None


def _signal_score(result: BreakoutValidityResult) -> float:
    hits = sum(1 for v in (result.paths or {}).values() if v)
    score = 70.0 + 5.0 * hits
    if result.extension is not None and result.extension <= 1.05:
        score += 5.0  # 离突破位近,空间占优(评分卡"位置与空间"简化)
    return min(score, 95.0)


def _upsert_signal(db, *, symbol: str, name: str, action: str, result: BreakoutValidityResult, snapshot: str) -> None:
    row = (
        db.query(StrategySignalRun)
        .filter(
            StrategySignalRun.snapshot_date == snapshot,
            StrategySignalRun.stock_symbol == symbol,
            StrategySignalRun.stock_market == "CN",
            StrategySignalRun.strategy_code == STRATEGY_CODE,
            StrategySignalRun.source_candidate_id.is_(None),
        )
        .first()
    )
    close = float(result.close or 0)
    score = _signal_score(result) if action == "buy" else 0.0
    fields: dict[str, Any] = {
        "stock_name": name,
        "strategy_name": STRATEGY_NAME,
        "status": "active",
        "action": action,
        "action_label": "买入" if action == "buy" else "卖出",
        "signal": f"breakout_{result.state}",
        "reason": result.reason,
        "evidence": list(result.evidence or []),
        "score": score,
        "rank_score": score,
        "holding_days": 10,
        "entry_low": round(close * 0.99, 2) if action == "buy" else None,
        "entry_high": round(close * 1.03, 2) if action == "buy" else None,
        "stop_loss": result.stop_loss if action == "buy" else None,
        "target_price": result.target_price if action == "buy" else None,
        "invalidation": f"收盘<max(G_SUPPORT,L0)={result.stop_loss} 或 连续3日<G0/放量破位/跌停破位",
        "source_pool": "market_scan",
        "payload": {
            "d0_date": result.d0_date,
            "g0": result.g0,
            "g_support": result.g_support,
            "l0": result.l0,
            "state": result.state,
            "paths": result.paths,
            "extension": result.extension,
            "fail_reason": result.fail_reason,
        },
    }
    if row:
        for key, value in fields.items():
            setattr(row, key, value)
    else:
        db.add(StrategySignalRun(
            snapshot_date=snapshot,
            stock_symbol=symbol,
            stock_market="CN",
            strategy_code=STRATEGY_CODE,
            **fields,
        ))


def scan_market(*, limit: int | None = None, params: BreakoutParams | None = None) -> dict:
    """全市场扫描:valid_active 写 buy 信号;历史 active buy 信号先置为过期。"""
    started = time.time()
    p = params or BreakoutParams()
    universe = build_universe()
    if limit:
        universe = universe[:limit]
    snapshot = _today()
    hits: list[tuple[dict, BreakoutValidityResult]] = []
    scanned = 0
    with ThreadPoolExecutor(max_workers=SCAN_MAX_WORKERS) as pool:
        futures = {pool.submit(_evaluate, s["symbol"], p): s for s in universe}
        for future in as_completed(futures):
            stock = futures[future]
            scanned += 1
            result = future.result()
            if result and result.state == "valid_active":
                hits.append((stock, result))

    db = SessionLocal()
    try:
        # 旧的 buy 信号过期,避免模拟盘按过时价格区间建仓
        db.query(StrategySignalRun).filter(
            StrategySignalRun.strategy_code == STRATEGY_CODE,
            StrategySignalRun.status == "active",
            StrategySignalRun.action == "buy",
            StrategySignalRun.snapshot_date < snapshot,
        ).update({"status": "inactive"}, synchronize_session=False)
        for stock, result in hits:
            _upsert_signal(db, symbol=stock["symbol"], name=stock["name"], action="buy", result=result, snapshot=snapshot)
        db.commit()
    finally:
        db.close()

    elapsed = round(time.time() - started, 1)
    summary = {
        "scanned": scanned,
        "universe": len(universe),
        "valid_active": len(hits),
        "symbols": [s["symbol"] for s, _ in hits],
        "elapsed_sec": elapsed,
    }
    logger.info("[突破有效性] 扫描完成: %s", summary)
    return summary


def monitor_positions(*, params: BreakoutParams | None = None) -> dict:
    """模拟盘持仓复评:锚点冻结重放,failed/invalidated 写 sell 信号触发强平。"""
    p = params or BreakoutParams()
    snapshot = _today()
    db = SessionLocal()
    try:
        positions = (
            db.query(PaperTradingPosition)
            .filter(
                PaperTradingPosition.status == "open",
                PaperTradingPosition.strategy_code == STRATEGY_CODE,
            )
            .all()
        )
        checked, closed_signals = 0, []
        for pos in positions:
            anchor = None
            if pos.signal_run_id:
                sig = db.query(StrategySignalRun).filter(StrategySignalRun.id == pos.signal_run_id).first()
                if sig and isinstance(sig.payload, dict):
                    anchor = sig.payload.get("d0_date")
            try:
                klines = fetch_klines_sync(pos.stock_symbol, "CN", days=KLINE_DAYS, interval="1d", cache_ttl_sec=300)
                result = compute_breakout_validity(klines, params=p, anchor_d0_date=anchor)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[突破有效性] 持仓 %s 复评失败: %s", pos.stock_symbol, exc)
                continue
            checked += 1
            if result.state in ("failed", "invalidated"):
                _upsert_signal(db, symbol=pos.stock_symbol, name=pos.stock_name or "", action="sell", result=result, snapshot=snapshot)
                closed_signals.append(f"{pos.stock_symbol}:{result.fail_reason}")
        db.commit()
        summary = {"positions": len(positions), "checked": checked, "sell_signals": closed_signals}
        if closed_signals:
            logger.info("[突破有效性] 持仓失效强平信号: %s", summary)
        return summary
    finally:
        db.close()


def run_daily() -> dict:
    """收盘后例行:先复评持仓失效,再扫全市场新信号。"""
    monitor = monitor_positions()
    scan = scan_market()
    return {"monitor": monitor, "scan": scan}
