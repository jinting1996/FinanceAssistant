from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import threading
import time
from typing import Callable, Any


TaskFn = Callable[["TaskHandle"], Any]


@dataclass
class TaskRecord:
    id: str
    type: str
    name: str
    status: str = "queued"
    progress_current: int = 0
    progress_total: int = 0
    result: dict = field(default_factory=dict)
    error: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    stale_after_seconds: int = 1800


class TaskHandle:
    def __init__(self, manager: "TaskManager", task_id: str):
        self.manager = manager
        self.task_id = task_id

    def set_progress(self, current: int, total: int | None = None) -> None:
        self.manager.set_progress(self.task_id, current=current, total=total)

    def set_result(self, result: dict) -> None:
        self.manager.set_result(self.task_id, result)


class TaskManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._seq = 0
        self._tasks: dict[str, TaskRecord] = {}
        self._active_by_key: dict[str, str] = {}

    def submit(
        self,
        *,
        type: str,
        name: str,
        fn: TaskFn,
        dedupe_key: str = "",
        progress_total: int = 0,
        stale_after_seconds: int = 1800,
    ) -> TaskRecord:
        with self._lock:
            self._mark_stale_locked()
            if dedupe_key:
                existing_id = self._active_by_key.get(dedupe_key)
                existing = self._tasks.get(existing_id or "")
                if existing and existing.status in {"queued", "running"}:
                    return existing

            self._seq += 1
            task_id = f"{type}-{int(time.time() * 1000)}-{self._seq}"
            record = TaskRecord(
                id=task_id,
                type=type,
                name=name,
                progress_total=max(0, int(progress_total or 0)),
                stale_after_seconds=stale_after_seconds,
            )
            self._tasks[task_id] = record
            if dedupe_key:
                self._active_by_key[dedupe_key] = task_id

        thread = threading.Thread(
            target=self._run,
            args=(task_id, fn, dedupe_key),
            daemon=True,
            name=f"task-{task_id}",
        )
        thread.start()
        return record

    def _run(self, task_id: str, fn: TaskFn, dedupe_key: str) -> None:
        self.set_status(task_id, "running", started=True)
        handle = TaskHandle(self, task_id)
        try:
            result = fn(handle)
            if isinstance(result, dict):
                self.set_result(task_id, result)
            self.set_status(task_id, "success", finished=True)
        except Exception as e:
            self.set_error(task_id, str(e))
            self.set_status(task_id, "failed", finished=True)
        finally:
            if dedupe_key:
                with self._lock:
                    if self._active_by_key.get(dedupe_key) == task_id:
                        self._active_by_key.pop(dedupe_key, None)

    def set_status(self, task_id: str, status: str, *, started: bool = False, finished: bool = False) -> None:
        with self._lock:
            rec = self._tasks.get(task_id)
            if not rec:
                return
            rec.status = status
            now = datetime.now(timezone.utc)
            if started:
                rec.started_at = now
            if finished:
                rec.finished_at = now

    def set_progress(self, task_id: str, *, current: int, total: int | None = None) -> None:
        with self._lock:
            rec = self._tasks.get(task_id)
            if not rec:
                return
            rec.progress_current = max(0, int(current or 0))
            if total is not None:
                rec.progress_total = max(0, int(total or 0))

    def set_result(self, task_id: str, result: dict) -> None:
        with self._lock:
            rec = self._tasks.get(task_id)
            if rec:
                rec.result = result

    def set_error(self, task_id: str, error: str) -> None:
        with self._lock:
            rec = self._tasks.get(task_id)
            if rec:
                rec.error = str(error or "")[:2000]

    def get(self, task_id: str) -> TaskRecord | None:
        with self._lock:
            self._mark_stale_locked()
            return self._tasks.get(task_id)

    def list(self, *, type: str = "", limit: int = 50) -> list[TaskRecord]:
        with self._lock:
            self._mark_stale_locked()
            rows = list(self._tasks.values())
            if type:
                rows = [r for r in rows if r.type == type]
            rows.sort(key=lambda r: r.created_at, reverse=True)
            return rows[: max(1, min(int(limit or 50), 200))]

    def _mark_stale_locked(self) -> None:
        now = datetime.now(timezone.utc)
        for rec in self._tasks.values():
            if rec.status not in {"queued", "running"}:
                continue
            ref = rec.started_at or rec.created_at
            if (now - ref).total_seconds() > rec.stale_after_seconds:
                rec.status = "stale"
                rec.finished_at = now


task_manager = TaskManager()


def task_to_dict(rec: TaskRecord) -> dict:
    def iso(dt):
        return dt.isoformat(timespec="seconds") if dt else ""

    return {
        "id": rec.id,
        "type": rec.type,
        "name": rec.name,
        "status": rec.status,
        "progress_current": rec.progress_current,
        "progress_total": rec.progress_total,
        "result": rec.result or {},
        "error": rec.error or "",
        "created_at": iso(rec.created_at),
        "started_at": iso(rec.started_at),
        "finished_at": iso(rec.finished_at),
    }

