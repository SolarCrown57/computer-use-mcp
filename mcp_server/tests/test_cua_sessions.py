from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

import pytest

from mcp_server.tools import cua
from mcp_server.tools import cua_sessions


class DummyInstance:
    def __init__(self, name: str, *, disconnect_error: Exception | None = None) -> None:
        self.name = name
        self.disconnect_error = disconnect_error
        self.disconnect_calls = 0
        self.destroy_calls = 0

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        if self.disconnect_error is not None:
            raise self.disconnect_error

    async def destroy(self) -> None:
        self.destroy_calls += 1

    async def get_environment(self) -> dict[str, str]:
        return {"os": "test"}

    async def get_dimensions(self) -> tuple[int, int]:
        return 800, 600


def make_session(session_id: str, instance: Any, *, persistent: bool = True) -> cua_sessions.CuaSession:
    now = time.time()
    return cua_sessions.CuaSession(
        session_id=session_id,
        kind="connect",
        target=session_id,
        instance=instance,
        persistent=persistent,
        created_at=now,
        last_used_at=now,
    )


def install_fake_sdk(
    monkeypatch: pytest.MonkeyPatch,
    localhost_connect: Callable[[], Awaitable[Any]],
    *,
    sandbox: type[Any] | None = None,
) -> None:
    class FakeImage:
        @staticmethod
        def linux(**kwargs: Any) -> tuple[str, dict[str, Any]]:
            return "linux", kwargs

    class FakeLocalhost:
        @staticmethod
        async def connect() -> Any:
            return await localhost_connect()

    class FakeSandbox:
        pass

    monkeypatch.setattr(
        cua_sessions,
        "_load_cua_sdk",
        lambda: (FakeImage, FakeLocalhost, sandbox or FakeSandbox),
    )


def test_custom_default_never_silently_connects_localhost(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def connect() -> DummyInstance:
        nonlocal calls
        calls += 1
        return DummyInstance("localhost")

    install_fake_sdk(monkeypatch, connect)
    manager = cua_sessions.CuaSessionManager(default_session_id="work")

    with pytest.raises(cua_sessions.CuaSessionNotFoundError, match="session_not_found") as exc_info:
        asyncio.run(manager.get_session())

    assert exc_info.value.error_code == "session_not_found"
    assert calls == 0


def test_literal_default_keeps_legacy_on_demand_localhost(monkeypatch: pytest.MonkeyPatch) -> None:
    instance = DummyInstance("localhost")

    async def connect() -> DummyInstance:
        return instance

    install_fake_sdk(monkeypatch, connect)
    manager = cua_sessions.CuaSessionManager(default_session_id="default")

    session = asyncio.run(manager.get_session())

    assert session.session_id == "default"
    assert session.instance is instance


def test_session_id_is_fixed_before_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = cua_sessions.CuaSessionManager(default_session_id="default")
    seen_lock = False

    async def connect() -> DummyInstance:
        nonlocal seen_lock
        seen_lock = "chosen" in manager._session_locks
        return DummyInstance("sdk-selected-name")

    install_fake_sdk(monkeypatch, connect)

    session = asyncio.run(manager.open_session(kind="localhost", session_id="chosen"))

    assert seen_lock is True
    assert session.session_id == "chosen"
    assert session.instance.name == "sdk-selected-name"


def test_concurrent_open_for_same_id_connects_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def connect() -> DummyInstance:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return DummyInstance("localhost")

    install_fake_sdk(monkeypatch, connect)
    manager = cua_sessions.CuaSessionManager(default_session_id="default")

    async def run() -> tuple[cua_sessions.CuaSession, cua_sessions.CuaSession]:
        first, second = await asyncio.gather(
            manager.open_session(kind="localhost", session_id="shared"),
            manager.open_session(kind="localhost", session_id="shared"),
        )
        return first, second

    first, second = asyncio.run(run())

    assert calls == 1
    assert first is second


def test_replace_connect_failure_keeps_old_session(monkeypatch: pytest.MonkeyPatch) -> None:
    old = DummyInstance("old")
    manager = cua_sessions.CuaSessionManager(default_session_id="default")
    asyncio.run(manager.register_session(make_session("shared", old)))

    async def connect() -> DummyInstance:
        raise RuntimeError("connect failed")

    install_fake_sdk(monkeypatch, connect)

    with pytest.raises(RuntimeError, match="connect failed"):
        asyncio.run(manager.open_session(kind="localhost", session_id="shared", replace=True))

    assert manager._sessions["shared"].instance is old
    assert old.disconnect_calls == 0
    assert old.destroy_calls == 0


def test_replace_disconnect_failure_rolls_back_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    old = DummyInstance("old", disconnect_error=RuntimeError("disconnect failed"))
    new = DummyInstance("new")
    manager = cua_sessions.CuaSessionManager(default_session_id="default")
    asyncio.run(manager.register_session(make_session("shared", old)))

    async def connect() -> DummyInstance:
        return new

    install_fake_sdk(monkeypatch, connect)

    with pytest.raises(RuntimeError, match="disconnect failed"):
        asyncio.run(manager.open_session(kind="localhost", session_id="shared", replace=True))

    assert manager._sessions["shared"].instance is old
    assert old.disconnect_calls == 1
    assert old.destroy_calls == 0
    assert new.disconnect_calls == 1


def test_replace_disconnects_without_destroying_old_session(monkeypatch: pytest.MonkeyPatch) -> None:
    old = DummyInstance("old")
    new = DummyInstance("new")
    manager = cua_sessions.CuaSessionManager(default_session_id="default")
    asyncio.run(manager.register_session(make_session("shared", old)))

    async def connect() -> DummyInstance:
        return new

    install_fake_sdk(monkeypatch, connect)

    session = asyncio.run(manager.open_session(kind="localhost", session_id="shared", replace=True))

    assert session.instance is new
    assert manager._sessions["shared"] is session
    assert old.disconnect_calls == 1
    assert old.destroy_calls == 0


def test_context_backed_replace_fails_before_connect_and_keeps_old_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = DummyInstance("old-ephemeral")
    context_exits = 0
    connect_calls = 0

    class EphemeralContext:
        async def __aexit__(self, *_args: Any) -> None:
            nonlocal context_exits
            context_exits += 1

    manager = cua_sessions.CuaSessionManager(default_session_id="default")
    old_session = make_session("shared", old, persistent=False)
    old_session.context = EphemeralContext()
    asyncio.run(manager.register_session(old_session))

    async def connect() -> DummyInstance:
        nonlocal connect_calls
        connect_calls += 1
        return DummyInstance("replacement")

    install_fake_sdk(monkeypatch, connect)

    with pytest.raises(
        cua_sessions.CuaSessionExplicitDestroyRequiredError,
        match="explicit_destroy_required",
    ):
        asyncio.run(manager.open_session(kind="localhost", session_id="shared", replace=True))

    assert manager._sessions["shared"] is old_session
    assert connect_calls == 0
    assert context_exits == 0
    assert old.disconnect_calls == 0
    assert old.destroy_calls == 0


def test_context_backed_close_requires_explicit_destroy_and_retains_session() -> None:
    instance = DummyInstance("ephemeral")
    context_exits = 0

    class EphemeralContext:
        async def __aexit__(self, *_args: Any) -> None:
            nonlocal context_exits
            context_exits += 1

    manager = cua_sessions.CuaSessionManager(default_session_id="default")
    session = make_session("ephemeral", instance, persistent=False)
    session.context = EphemeralContext()
    asyncio.run(manager.register_session(session))

    with pytest.raises(
        cua_sessions.CuaSessionExplicitDestroyRequiredError,
        match="explicit_destroy_required",
    ):
        asyncio.run(manager.close_session("ephemeral", destroy=False))

    assert manager._sessions["ephemeral"] is session
    assert context_exits == 0
    assert instance.disconnect_calls == 0
    assert instance.destroy_calls == 0

    result = asyncio.run(manager.close_session("ephemeral", destroy=True))
    assert result == {"session_id": "ephemeral", "closed": True, "destroyed": True}
    assert context_exits == 1
    assert "ephemeral" not in manager._sessions


def test_close_failure_retains_session_for_retry() -> None:
    instance = DummyInstance("failing", disconnect_error=RuntimeError("busy"))
    manager = cua_sessions.CuaSessionManager(default_session_id="default")
    asyncio.run(manager.register_session(make_session("failing", instance)))

    with pytest.raises(RuntimeError, match="busy"):
        asyncio.run(manager.close_session("failing"))

    assert manager._sessions["failing"].instance is instance

    instance.disconnect_error = None
    result = asyncio.run(manager.close_session("failing"))
    assert result == {"session_id": "failing", "closed": True, "destroyed": False}
    assert "failing" not in manager._sessions


def test_destroy_is_only_used_when_explicitly_requested() -> None:
    instance = DummyInstance("persistent")
    manager = cua_sessions.CuaSessionManager(default_session_id="default")
    asyncio.run(manager.register_session(make_session("persistent", instance)))

    result = asyncio.run(manager.close_session("persistent", destroy=True))

    assert result["destroyed"] is True
    assert instance.destroy_calls == 1
    assert instance.disconnect_calls == 0


def test_close_all_is_concurrent_and_aggregates_errors() -> None:
    started = 0
    all_started = asyncio.Event()

    class BarrierInstance(DummyInstance):
        async def disconnect(self) -> None:
            nonlocal started
            self.disconnect_calls += 1
            started += 1
            if started == 3:
                all_started.set()
            await asyncio.wait_for(all_started.wait(), timeout=0.2)
            if self.disconnect_error is not None:
                raise self.disconnect_error

    first = BarrierInstance("first", disconnect_error=RuntimeError("first failed"))
    second = BarrierInstance("second")
    third = BarrierInstance("third", disconnect_error=RuntimeError("third failed"))
    manager = cua_sessions.CuaSessionManager(default_session_id="default")

    async def run() -> ExceptionGroup:
        for session_id, instance in (("first", first), ("second", second), ("third", third)):
            await manager.register_session(make_session(session_id, instance))
        try:
            await manager.close_all()
        except ExceptionGroup as exc:
            return exc
        raise AssertionError("close_all should aggregate cleanup failures")

    error = asyncio.run(run())

    assert len(error.exceptions) == 2
    assert set(manager._sessions) == {"first", "third"}
    assert all(instance.disconnect_calls == 1 for instance in (first, second, third))


def test_resume_registers_transactionally_through_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    old = DummyInstance("old")
    resumed = DummyInstance("resumed")
    manager = cua_sessions.CuaSessionManager(default_session_id="default")
    asyncio.run(manager.register_session(make_session("attached", old)))

    class FakeSandbox:
        @staticmethod
        async def resume(name: str, *, local: bool, api_key: str | None) -> DummyInstance:
            assert (name, local, api_key) == ("box", True, None)
            return resumed

    monkeypatch.setattr(cua_sessions, "_load_cua_sdk", lambda: (object, object, FakeSandbox))
    monkeypatch.setattr(cua, "get_cua_manager", lambda: manager)

    result = asyncio.run(cua.cua_resume_sandbox(name="box", session_id="attached", local=True, api_key=None))

    assert result["session_id"] == "attached"
    assert manager._sessions["attached"].instance is resumed
    assert old.disconnect_calls == 1
    assert old.destroy_calls == 0


def test_concurrent_resume_for_same_id_runs_once_inside_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    old = DummyInstance("old")
    resumed = DummyInstance("resumed")
    manager = cua_sessions.CuaSessionManager(default_session_id="default")
    asyncio.run(manager.register_session(make_session("attached", old)))
    resume_started: asyncio.Event
    resume_release: asyncio.Event
    calls = 0
    lock_states: list[bool] = []

    class FakeSandbox:
        @staticmethod
        async def resume(name: str, *, local: bool, api_key: str | None) -> DummyInstance:
            nonlocal calls
            calls += 1
            lock_states.append(manager._lock_for("attached").locked())
            resume_started.set()
            await resume_release.wait()
            return resumed

    monkeypatch.setattr(cua_sessions, "_load_cua_sdk", lambda: (object, object, FakeSandbox))

    async def run() -> tuple[cua_sessions.CuaSession, cua_sessions.CuaSession]:
        nonlocal resume_started, resume_release
        resume_started = asyncio.Event()
        resume_release = asyncio.Event()
        first_task = asyncio.create_task(
            manager.resume_sandbox(name="box", session_id="attached")
        )
        await resume_started.wait()
        second_task = asyncio.create_task(
            manager.resume_sandbox(name="box", session_id="attached")
        )
        await asyncio.sleep(0)
        resume_release.set()
        return await asyncio.gather(first_task, second_task)

    first, second = asyncio.run(run())

    assert calls == 1
    assert lock_states == [True]
    assert first is second
    assert manager._sessions["attached"] is first
    assert old.disconnect_calls == 1


def test_later_resume_still_replaces_existing_session(monkeypatch: pytest.MonkeyPatch) -> None:
    first_instance = DummyInstance("first")
    second_instance = DummyInstance("second")
    manager = cua_sessions.CuaSessionManager(default_session_id="default")
    resumed = iter((first_instance, second_instance))
    lock_states: list[bool] = []

    class FakeSandbox:
        @staticmethod
        async def resume(name: str, *, local: bool, api_key: str | None) -> DummyInstance:
            lock_states.append(manager._lock_for("attached").locked())
            return next(resumed)

    monkeypatch.setattr(cua_sessions, "_load_cua_sdk", lambda: (object, object, FakeSandbox))

    async def run() -> tuple[cua_sessions.CuaSession, cua_sessions.CuaSession]:
        first = await manager.resume_sandbox(name="box", session_id="attached")
        second = await manager.resume_sandbox(name="box", session_id="attached")
        return first, second

    first, second = asyncio.run(run())

    assert first.instance is first_instance
    assert second.instance is second_instance
    assert first_instance.disconnect_calls == 1
    assert lock_states == [True, True]
    assert manager._sessions["attached"] is second


def test_resume_failure_keeps_existing_session(monkeypatch: pytest.MonkeyPatch) -> None:
    old = DummyInstance("old")
    manager = cua_sessions.CuaSessionManager(default_session_id="default")
    asyncio.run(manager.register_session(make_session("attached", old)))

    class FakeSandbox:
        @staticmethod
        async def resume(name: str, *, local: bool, api_key: str | None) -> DummyInstance:
            assert manager._lock_for("attached").locked()
            raise RuntimeError("resume failed")

    monkeypatch.setattr(cua_sessions, "_load_cua_sdk", lambda: (object, object, FakeSandbox))

    with pytest.raises(RuntimeError, match="resume failed"):
        asyncio.run(manager.resume_sandbox(name="box", session_id="attached"))

    assert manager._sessions["attached"].instance is old
    assert old.disconnect_calls == 0
    assert old.destroy_calls == 0


def test_resume_replace_failure_cleans_candidate_and_keeps_old(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = DummyInstance("old", disconnect_error=RuntimeError("disconnect failed"))
    candidate = DummyInstance("candidate")
    manager = cua_sessions.CuaSessionManager(default_session_id="default")
    asyncio.run(manager.register_session(make_session("attached", old)))

    class FakeSandbox:
        @staticmethod
        async def resume(name: str, *, local: bool, api_key: str | None) -> DummyInstance:
            assert manager._lock_for("attached").locked()
            return candidate

    monkeypatch.setattr(cua_sessions, "_load_cua_sdk", lambda: (object, object, FakeSandbox))

    with pytest.raises(RuntimeError, match="disconnect failed"):
        asyncio.run(manager.resume_sandbox(name="box", session_id="attached"))

    assert manager._sessions["attached"].instance is old
    assert old.disconnect_calls == 1
    assert old.destroy_calls == 0
    assert candidate.disconnect_calls == 1
    assert candidate.destroy_calls == 0


def test_make_image_supports_all_sources_and_platforms(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    class FakeImage:
        @staticmethod
        def _record(method: str, *args: Any, **kwargs: Any) -> str:
            calls.append((method, args, kwargs))
            return method

        from_registry = staticmethod(lambda ref: FakeImage._record("registry", ref))
        from_file = staticmethod(lambda path, **kwargs: FakeImage._record("file", path, **kwargs))
        linux = staticmethod(lambda **kwargs: FakeImage._record("linux", **kwargs))
        macos = staticmethod(lambda **kwargs: FakeImage._record("macos", **kwargs))
        windows = staticmethod(lambda **kwargs: FakeImage._record("windows", **kwargs))
        android = staticmethod(lambda **kwargs: FakeImage._record("android", **kwargs))

    monkeypatch.setattr(cua_sessions, "_load_cua_sdk", lambda: (FakeImage, object, object))

    assert cua_sessions.make_image(registry_ref="repo/image:tag") == "registry"
    assert cua_sessions.make_image("windows", image_path="disk.iso", agent_type="agent") == "file"
    assert cua_sessions.make_image("linux", distro="debian", version="12") == "linux"
    assert cua_sessions.make_image("mac") == "macos"
    assert cua_sessions.make_image("windows") == "windows"
    assert cua_sessions.make_image("android") == "android"
    with pytest.raises(ValueError, match="Unsupported CUA image"):
        cua_sessions.make_image("plan9")

    assert calls[1] == (
        "file",
        ("disk.iso",),
        {"os_type": "windows", "kind": "vm", "agent_type": "agent"},
    )


def test_connect_create_and_ephemeral_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    created = DummyInstance("created-by-sdk")
    connected = DummyInstance("connected")
    ephemeral = DummyInstance("ephemeral")
    context_exits = 0

    class FakeImage:
        @staticmethod
        def linux(**_kwargs: Any) -> str:
            return "linux-image"

    class EphemeralContext:
        async def __aenter__(self) -> DummyInstance:
            return ephemeral

        async def __aexit__(self, *_args: Any) -> None:
            nonlocal context_exits
            context_exits += 1

    class FakeSandbox:
        @staticmethod
        async def connect(name: str, **_kwargs: Any) -> DummyInstance:
            assert name == "remote"
            return connected

        @staticmethod
        async def create(image: str, **_kwargs: Any) -> DummyInstance:
            assert image == "linux-image"
            return created

        @staticmethod
        def ephemeral(image: str, **_kwargs: Any) -> EphemeralContext:
            assert image == "linux-image"
            return EphemeralContext()

    class FakeLocalhost:
        @staticmethod
        async def connect() -> DummyInstance:
            raise AssertionError("localhost should not be used")

    monkeypatch.setattr(
        cua_sessions,
        "_load_cua_sdk",
        lambda: (FakeImage, FakeLocalhost, FakeSandbox),
    )
    monkeypatch.setattr(cua_sessions.uuid, "uuid4", lambda: "allocated-before-create")
    manager = cua_sessions.CuaSessionManager(default_session_id="default")

    async def run() -> tuple[cua_sessions.CuaSession, cua_sessions.CuaSession, cua_sessions.CuaSession]:
        connect_session = await manager.open_session(kind="connect", name="remote")
        create_session = await manager.open_session(kind="create")
        ephemeral_session = await manager.open_session(kind="ephemeral", session_id="temporary")
        await manager.close_session("temporary", destroy=True)
        return connect_session, create_session, ephemeral_session

    connect_session, create_session, ephemeral_session = asyncio.run(run())

    assert connect_session.session_id == "remote"
    assert connect_session.persistent is True
    assert create_session.session_id == "allocated-before-create"
    assert create_session.target == "created-by-sdk"
    assert ephemeral_session.persistent is False
    assert context_exits == 1
    assert ephemeral.destroy_calls == 0


def test_mode_validation_and_connect_selector_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    async def connect() -> DummyInstance:
        return DummyInstance("localhost")

    install_fake_sdk(monkeypatch, connect)
    manager = cua_sessions.CuaSessionManager(default_session_id="default")

    with pytest.raises(ValueError, match="kind must be"):
        asyncio.run(manager.open_session(kind="invalid"))
    with pytest.raises(ValueError, match="requires name"):
        asyncio.run(manager.open_session(kind="connect"))


def test_register_duplicate_discards_candidate_and_returns_existing() -> None:
    old = DummyInstance("old")
    candidate = DummyInstance("candidate")
    manager = cua_sessions.CuaSessionManager(default_session_id="default")

    async def run() -> cua_sessions.CuaSession:
        old_session = await manager.register_session(make_session("shared", old))
        result = await manager.register_session(make_session("shared", candidate))
        assert result is old_session
        return result

    result = asyncio.run(run())

    assert result.instance is old
    assert candidate.disconnect_calls == 1


def test_candidate_cleanup_failure_does_not_replace_existing(caplog) -> None:
    old = DummyInstance("old")
    candidate = DummyInstance("candidate", disconnect_error=RuntimeError("cleanup failed"))
    manager = cua_sessions.CuaSessionManager(default_session_id="default")

    async def run() -> cua_sessions.CuaSession:
        await manager.register_session(make_session("shared", old))
        return await manager.register_session(make_session("shared", candidate))

    result = asyncio.run(run())

    assert result.instance is old
    assert "Failed to clean up unregistered CUA session" in caplog.text


def test_session_info_tolerates_optional_probe_failures() -> None:
    class ProbeFailure(DummyInstance):
        async def get_environment(self) -> dict[str, str]:
            raise RuntimeError("environment unavailable")

        async def get_dimensions(self) -> tuple[int, int]:
            raise RuntimeError("dimensions unavailable")

    manager = cua_sessions.CuaSessionManager(default_session_id="default")
    instance = ProbeFailure("")

    async def run() -> list[dict[str, Any]]:
        await manager.register_session(make_session("probe", instance))
        assert (await manager.get_session("probe")).instance is instance
        return await manager.list_sessions()

    result = asyncio.run(run())

    assert result[0]["session_id"] == "probe"
    assert manager.active_session_ids() == ["probe"]
    assert "name" not in result[0]
    assert "environment" not in result[0]
    assert "width" not in result[0]


def test_close_missing_session_and_empty_close_all() -> None:
    manager = cua_sessions.CuaSessionManager(default_session_id="default")

    with pytest.raises(cua_sessions.CuaSessionNotFoundError, match="session_not_found"):
        asyncio.run(manager.close_session("missing"))
    asyncio.run(manager.close_all())


def test_invalid_configured_default_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cua_sessions, "cua_config", {"default_session": " "})
    with pytest.raises(ValueError, match="non-empty string"):
        cua_sessions.CuaSessionManager()
