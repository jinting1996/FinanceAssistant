from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from src.core.task_manager import task_manager, task_to_dict

router = APIRouter()


@router.get("")
def list_tasks(
    type: str = Query("", description="Optional task type filter."),
    limit: int = Query(50, ge=1, le=200),
):
    return {"items": [task_to_dict(x) for x in task_manager.list(type=type, limit=limit)]}


@router.get("/{task_id}")
def get_task(task_id: str):
    rec = task_manager.get(task_id)
    if not rec:
        raise HTTPException(404, "task not found")
    return task_to_dict(rec)
