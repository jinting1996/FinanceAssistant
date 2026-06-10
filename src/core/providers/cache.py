"""带 TTL 的轻量级缓存。

- 单进程内存缓存,够 60s 调度器叠加防抖用,不依赖 Redis。
- 线程安全:用 `threading.Lock` 包裹读写。
- 不做后台清理,过期 key 在下次 get 时被动剔除,避免后台线程。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any


@dataclass
class _Entry:
    value: Any
    expires_at: float


class TTLCache:
    def __init__(self, default_ttl_sec: float = 20.0, max_size: int = 1024):
        self._default_ttl = default_ttl_sec
        self._max_size = max_size
        self._lock = threading.Lock()
        self._store: dict[str, _Entry] = {}

    def get(self, key: str) -> Any | None:
        now = time.monotonic()
        with self._lock:
            entry = self._store.get(key)
            if not entry:
                return None
            if entry.expires_at <= now:
                # 过期被动剔除
                del self._store[key]
                return None
            return entry.value

    def set(self, key: str, value: Any, ttl_sec: float | None = None) -> None:
        ttl = ttl_sec if ttl_sec is not None else self._default_ttl
        if ttl <= 0:
            return  # 显式不缓存
        expires = time.monotonic() + ttl
        with self._lock:
            # 超过容量时简单丢最早过期的(粗略 LRU,够用)
            if len(self._store) >= self._max_size and key not in self._store:
                oldest = min(self._store.items(), key=lambda kv: kv[1].expires_at)
                del self._store[oldest[0]]
            self._store[key] = _Entry(value=value, expires_at=expires)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)
