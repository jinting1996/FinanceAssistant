"""Shared HTTP/runtime helpers for async data providers."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from typing import Any
from weakref import WeakKeyDictionary

import httpx

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None
_loop_lock = threading.Lock()
_clients: "WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient]" = WeakKeyDictionary()


def resolve_http_proxy() -> str | None:
    """Resolve the user-configured proxy in the same order existing callers used."""

    try:
        from src.core.notifier import get_global_proxy

        proxy = (get_global_proxy() or "").strip()
        if proxy:
            return proxy
    except Exception:
        pass

    try:
        from src.config import Settings

        proxy = (Settings().http_proxy or "").strip()
        if proxy:
            return proxy
    except Exception:
        pass

    return None


def _ensure_loop() -> asyncio.AbstractEventLoop:
    global _loop, _loop_thread
    with _loop_lock:
        if _loop and _loop.is_running():
            return _loop

        loop = asyncio.new_event_loop()

        def _run() -> None:
            asyncio.set_event_loop(loop)
            loop.run_forever()

        thread = threading.Thread(target=_run, name="finance-async-runtime", daemon=True)
        thread.start()
        _loop = loop
        _loop_thread = thread
        return loop


def run_sync(coro: Coroutine[Any, Any, Any]):
    """Run an async coroutine from sync code, including inside an existing event loop."""

    loop = _ensure_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result()


async def get_async_client() -> httpx.AsyncClient:
    """Return a shared AsyncClient for the current event loop."""

    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client and not client.is_closed:
        return client

    proxy = resolve_http_proxy()
    kwargs: dict[str, Any] = {
        "headers": {"User-Agent": DEFAULT_USER_AGENT},
        "timeout": httpx.Timeout(10.0, connect=5.0),
        "follow_redirects": True,
    }
    if proxy:
        kwargs["proxy"] = proxy
    client = httpx.AsyncClient(**kwargs)
    _clients[loop] = client
    return client


async def aclose_clients() -> None:
    """Close all shared AsyncClient instances."""

    clients = list(_clients.values())
    _clients.clear()
    for client in clients:
        if not client.is_closed:
            await client.aclose()
