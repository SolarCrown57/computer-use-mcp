from __future__ import annotations

import asyncio

import anyio

from mcp_server import main
from mcp_server.tools import _lifespan
from mcp_server.tools import cua_sessions


def test_parent_watchdog_cancels_stdio_server(monkeypatch) -> None:
    server_started = False
    server_stopped = False

    async def run_stdio_async() -> None:
        nonlocal server_started, server_stopped
        server_started = True
        try:
            await anyio.sleep_forever()
        finally:
            server_stopped = True

    monkeypatch.setattr(main.MCP, "run_stdio_async", run_stdio_async)
    monkeypatch.setattr(main, "_process_exists", lambda _pid: False)

    asyncio.run(
        main._run_stdio_with_parent_watchdog(
            watched=[123],
            poll_interval=0.001,
            shutdown_timeout=0.1,
        )
    )

    assert server_started is True
    assert server_stopped is True


def test_natural_stdio_completion_stops_watchdog(monkeypatch) -> None:
    server_completed = False

    async def run_stdio_async() -> None:
        nonlocal server_completed
        await anyio.sleep(0)
        server_completed = True

    def process_exists(_pid: int) -> bool:
        return True

    monkeypatch.setattr(main.MCP, "run_stdio_async", run_stdio_async)
    monkeypatch.setattr(main, "_process_exists", process_exists)
    monkeypatch.setattr(main, "_force_exit", lambda _code: (_ for _ in ()).throw(AssertionError("forced exit")))

    asyncio.run(
        main._run_stdio_with_parent_watchdog(
            watched=[123],
            poll_interval=0.001,
            shutdown_timeout=0.1,
        )
    )

    assert server_completed is True


def test_lifespan_cleanup_is_shielded_from_outer_cancellation(monkeypatch) -> None:
    cleanup_called = False

    class FakeManager:
        async def close_all(self) -> None:
            nonlocal cleanup_called
            await anyio.sleep(0)
            cleanup_called = True

    monkeypatch.setattr(cua_sessions, "get_cua_manager", lambda: FakeManager())

    async def run() -> None:
        context = _lifespan(None)
        await context.__aenter__()
        with anyio.CancelScope() as cancel_scope:
            cancel_scope.cancel()
            await context.__aexit__(None, None, None)

    asyncio.run(run())

    assert cleanup_called is True


def test_parent_watchdog_forces_exit_when_cleanup_exceeds_deadline(monkeypatch) -> None:
    cleanup_started = anyio.Event()
    cleanup_release = anyio.Event()
    forced: list[int] = []

    async def run_stdio_async() -> None:
        try:
            await anyio.sleep_forever()
        finally:
            with anyio.CancelScope(shield=True):
                cleanup_started.set()
                await cleanup_release.wait()

    class FakeManager:
        @staticmethod
        def active_session_ids() -> list[str]:
            return ["stuck-session"]

    def force_exit(exit_code: int) -> None:
        forced.append(exit_code)
        cleanup_release.set()

    monkeypatch.setattr(main.MCP, "run_stdio_async", run_stdio_async)
    monkeypatch.setattr(main, "_process_exists", lambda _pid: False)
    monkeypatch.setattr(main, "_force_exit", force_exit)
    monkeypatch.setattr(cua_sessions, "get_cua_manager", lambda: FakeManager())

    asyncio.run(
        main._run_stdio_with_parent_watchdog(
            watched=[123],
            poll_interval=0.001,
            shutdown_timeout=0.01,
        )
    )

    assert cleanup_started.is_set()
    assert forced == [1]
