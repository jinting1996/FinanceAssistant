from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.core.json_safe import to_jsonable
from src.core.screener.formula import FormulaError, FormulaEvaluator, function_catalog, parse_formula
from src.core.screener.providers import get_screener_provider, normalize_universe_config
from src.core.task_manager import TaskHandle, task_manager
from src.web.database import SessionLocal, get_db
from src.web.models import StockScreenerFormula, StockScreenerResult, StockScreenerRun

logger = logging.getLogger(__name__)
router = APIRouter()


class ScreenerFormulaIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    description: str = ""
    formula: str = Field(..., min_length=1)
    universe_config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class ScreenerFormulaValidateIn(BaseModel):
    formula: str = Field(..., min_length=1)


class ScreenerRunIn(BaseModel):
    formula_id: int | None = None
    formula: str = ""
    universe_config: dict[str, Any] = Field(default_factory=dict)


def _iso(dt) -> str:
    if not dt:
        return ""
    try:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat(timespec="seconds")
    except Exception:
        return str(dt)


def _formula_row(row: StockScreenerFormula) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description or "",
        "formula": row.formula,
        "universe_config": row.universe_config or {},
        "enabled": bool(row.enabled),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _run_row(row: StockScreenerRun, *, include_results: bool = False, db: Session | None = None) -> dict:
    data = {
        "id": row.id,
        "formula_id": row.formula_id,
        "formula_snapshot": row.formula_snapshot,
        "universe_config": row.universe_config or {},
        "status": row.status,
        "task_id": row.task_id or "",
        "total_count": int(row.total_count or 0),
        "matched_count": int(row.matched_count or 0),
        "progress_current": int(row.progress_current or 0),
        "progress_total": int(row.progress_total or 0),
        "duration_ms": int(row.duration_ms or 0),
        "error": row.error or "",
        "created_at": _iso(row.created_at),
        "started_at": _iso(row.started_at),
        "finished_at": _iso(row.finished_at),
    }
    if include_results and db is not None:
        rows = (
            db.query(StockScreenerResult)
            .filter(StockScreenerResult.run_id == row.id)
            .order_by(StockScreenerResult.change_pct.desc(), StockScreenerResult.id.asc())
            .limit(500)
            .all()
        )
        data["results"] = [_result_row(x) for x in rows]
    return data


def _result_row(row: StockScreenerResult) -> dict:
    return {
        "run_id": row.run_id,
        "symbol": row.symbol,
        "market": row.market,
        "name": row.name or row.symbol,
        "board_code": row.board_code or "",
        "board_name": row.board_name or "",
        "last_close": row.last_close,
        "change_pct": row.change_pct,
        "matched": bool(row.matched),
        "reason": row.reason or "",
        "indicators": row.indicators or {},
    }


def _latest_change(klines) -> tuple[float | None, float | None]:
    if not klines:
        return None, None
    last = klines[-1]
    close = float(getattr(last, "close", 0) or 0)
    if len(klines) < 2:
        return close, None
    prev = float(getattr(klines[-2], "close", 0) or 0)
    if prev == 0:
        return close, None
    return close, (close - prev) / prev * 100


def _run_dedupe_key(formula_text: str, cfg: dict) -> str:
    raw = json.dumps({"formula": formula_text, "config": cfg}, ensure_ascii=False, sort_keys=True)
    return raw


def _run_screener_job(run_id: int, task: TaskHandle | None = None) -> dict:
    started = time.perf_counter()
    db = SessionLocal()
    try:
        run = db.query(StockScreenerRun).filter(StockScreenerRun.id == run_id).first()
        if not run:
            return {"run_id": run_id, "missing": True}

        cfg = normalize_universe_config(run.universe_config or {})
        program = parse_formula(run.formula_snapshot)
        provider = get_screener_provider(cfg.get("provider"))
        universe = provider.resolve_universe(db, cfg, limit=int(cfg.get("max_symbols") or 300))

        run.total_count = len(universe)
        run.progress_total = len(universe)
        run.progress_current = 0
        run.status = "running"
        run.started_at = datetime.now(timezone.utc)
        db.commit()
        if task:
            task.set_progress(0, len(universe))

        matched = 0
        for idx, stock in enumerate(universe, start=1):
            klines = provider.fetch_klines(stock, days=int(cfg.get("days") or 120))
            evaluated: dict[str, Any] = {"matched": False}
            if len(klines) >= 30:
                try:
                    evaluated = FormulaEvaluator(klines, symbol=stock.symbol).run(program)
                except FormulaError as e:
                    logger.debug("formula not evaluable for %s: %s", stock.symbol, e)
                except Exception as e:
                    logger.warning("formula run failed for %s: %s", stock.symbol, e)

            if evaluated.get("matched"):
                last_close, change_pct = _latest_change(klines)
                db.add(
                    StockScreenerResult(
                        run_id=run.id,
                        symbol=stock.symbol,
                        market=stock.market,
                        name=stock.name or stock.symbol,
                        board_code=stock.board_code,
                        board_name=stock.board_name,
                        last_close=last_close,
                        change_pct=change_pct,
                        matched=True,
                        reason="Formula matched on the latest trading day",
                        indicators=to_jsonable(evaluated.get("indicators") or {}),
                    )
                )
                matched += 1

            if idx % 5 == 0 or idx == len(universe):
                run.progress_current = idx
                run.progress_total = len(universe)
                run.matched_count = matched
                db.commit()
                if task:
                    task.set_progress(idx, len(universe))

        run.progress_current = len(universe)
        run.progress_total = len(universe)
        run.matched_count = matched
        run.status = "success"
        run.duration_ms = int((time.perf_counter() - started) * 1000)
        run.finished_at = datetime.now(timezone.utc)
        run.error = ""
        db.commit()
        return {"run_id": run.id, "total_count": len(universe), "matched_count": matched}
    except Exception as e:
        logger.exception("screener run failed: %s", e)
        run = db.query(StockScreenerRun).filter(StockScreenerRun.id == run_id).first()
        if run:
            run.status = "failed"
            run.error = str(e)[:2000]
            run.duration_ms = int((time.perf_counter() - started) * 1000)
            run.finished_at = datetime.now(timezone.utc)
            db.commit()
        raise
    finally:
        db.close()


@router.get("/functions")
def get_functions():
    return function_catalog()


@router.get("/formulas")
def list_formulas(db: Session = Depends(get_db)):
    rows = (
        db.query(StockScreenerFormula)
        .order_by(StockScreenerFormula.updated_at.desc(), StockScreenerFormula.id.desc())
        .all()
    )
    return {"items": [_formula_row(r) for r in rows]}


@router.post("/formulas")
def create_formula(payload: ScreenerFormulaIn, db: Session = Depends(get_db)):
    try:
        parse_formula(payload.formula)
    except FormulaError as e:
        raise HTTPException(400, str(e))
    row = StockScreenerFormula(
        name=payload.name.strip(),
        description=payload.description.strip(),
        formula=payload.formula.strip(),
        universe_config=normalize_universe_config(payload.universe_config),
        enabled=payload.enabled,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _formula_row(row)


@router.put("/formulas/{formula_id}")
def update_formula(formula_id: int, payload: ScreenerFormulaIn, db: Session = Depends(get_db)):
    row = db.query(StockScreenerFormula).filter(StockScreenerFormula.id == formula_id).first()
    if not row:
        raise HTTPException(404, "formula not found")
    try:
        parse_formula(payload.formula)
    except FormulaError as e:
        raise HTTPException(400, str(e))
    row.name = payload.name.strip()
    row.description = payload.description.strip()
    row.formula = payload.formula.strip()
    row.universe_config = normalize_universe_config(payload.universe_config)
    row.enabled = payload.enabled
    db.commit()
    db.refresh(row)
    return _formula_row(row)


@router.delete("/formulas/{formula_id}")
def delete_formula(formula_id: int, db: Session = Depends(get_db)):
    row = db.query(StockScreenerFormula).filter(StockScreenerFormula.id == formula_id).first()
    if not row:
        raise HTTPException(404, "formula not found")
    db.delete(row)
    db.commit()
    return {"ok": True}


@router.post("/formulas/validate")
def validate_formula(payload: ScreenerFormulaValidateIn):
    try:
        parse_formula(payload.formula)
        return {"valid": True, "message": "formula is valid"}
    except FormulaError as e:
        return {"valid": False, "message": str(e)}


@router.post("/runs")
def create_run(payload: ScreenerRunIn, db: Session = Depends(get_db)):
    formula_text = (payload.formula or "").strip()
    formula_id = payload.formula_id
    cfg = normalize_universe_config(payload.universe_config)

    if formula_id:
        row = db.query(StockScreenerFormula).filter(StockScreenerFormula.id == formula_id).first()
        if not row:
            raise HTTPException(404, "formula not found")
        formula_text = row.formula
        if not payload.universe_config:
            cfg = normalize_universe_config(row.universe_config or {})

    if not formula_text:
        raise HTTPException(400, "missing formula")
    try:
        parse_formula(formula_text)
    except FormulaError as e:
        raise HTTPException(400, str(e))

    dedupe_key = _run_dedupe_key(formula_text, cfg)
    active_rows = (
        db.query(StockScreenerRun)
        .filter(
            StockScreenerRun.status.in_(["queued", "running"]),
            StockScreenerRun.formula_snapshot == formula_text,
        )
        .order_by(StockScreenerRun.id.desc())
        .limit(20)
        .all()
    )
    existing = next((x for x in active_rows if normalize_universe_config(x.universe_config or {}) == cfg), None)
    if existing:
        return _run_row(existing)

    run = StockScreenerRun(
        formula_id=formula_id,
        formula_snapshot=formula_text,
        universe_config=cfg,
        status="queued",
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    rec = task_manager.submit(
        type="screener",
        name=f"screener-run-{run.id}",
        fn=lambda handle: _run_screener_job(run.id, handle),
        dedupe_key=f"screener:{dedupe_key}",
        stale_after_seconds=3600,
    )
    run.task_id = rec.id
    run.progress_current = rec.progress_current
    run.progress_total = rec.progress_total
    db.commit()
    db.refresh(run)
    return _run_row(run)


@router.get("/runs/{run_id}")
def get_run(run_id: int, db: Session = Depends(get_db)):
    run = db.query(StockScreenerRun).filter(StockScreenerRun.id == run_id).first()
    if not run:
        raise HTTPException(404, "run not found")
    return _run_row(run, include_results=True, db=db)


@router.delete("/runs/{run_id}")
def delete_run(run_id: int, db: Session = Depends(get_db)):
    run = db.query(StockScreenerRun).filter(StockScreenerRun.id == run_id).first()
    if not run:
        raise HTTPException(404, "run not found")
    db.delete(run)
    db.commit()
    return {"ok": True}


@router.get("/runs")
def list_runs(limit: int = Query(20, ge=1, le=100), db: Session = Depends(get_db)):
    rows = (
        db.query(StockScreenerRun)
        .order_by(StockScreenerRun.created_at.desc(), StockScreenerRun.id.desc())
        .limit(limit)
        .all()
    )
    return {"items": [_run_row(r) for r in rows]}
