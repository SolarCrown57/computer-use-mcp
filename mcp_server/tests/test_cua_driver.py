import asyncio
import inspect
import json
from typing import Any

import pytest
from mcp import types

from mcp_server.tools import cua_driver


@pytest.fixture(autouse=True)
def reset_driver_cache():
    cua_driver._reset_driver_cache()
    yield
    cua_driver._reset_driver_cache()


def _compatible(capabilities: list[str]) -> dict[str, Any]:
    return {
        "available": True,
        "compatible": True,
        "message": "Driver is compatible.",
        "actual_version": "0.7.1-sc.1",
        "expected_version": "0.7.1-sc.1",
        "protocol_version": "sc.background.v1",
        "expected_protocol": "sc.background.v1",
        "capabilities": capabilities,
        "missing_capabilities": [],
        "status": {"ok": True},
    }


class _CompletedProcess:
    def __init__(self, stdout: bytes, stderr: bytes = b"", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.payload = None
        self.killed = False
        self.waited = False

    async def communicate(self, payload=None):
        self.payload = payload
        return self.stdout, self.stderr

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        self.waited = True
        return self.returncode


class _HangingProcess(_CompletedProcess):
    def __init__(self):
        super().__init__(b"")
        self.returncode = None
        self.started = asyncio.Event()

    async def communicate(self, payload=None):
        self.payload = payload
        self.started.set()
        await asyncio.Future()


class _CompletesWhenCancelledProcess(_CompletedProcess):
    def __init__(self, response: dict[str, Any] | None = None):
        super().__init__(b"")
        self.returncode = None
        self.cancel_requested = asyncio.Event()
        self.response = response

    async def communicate(self, payload=None):
        self.payload = payload
        await self.cancel_requested.wait()
        self.returncode = 1
        if self.response is not None:
            return json.dumps(self.response).encode(), b""
        return (
            b'{"ok":false,"verified":false,"error_code":"operation_cancelled",'
            b'"confirmed_written":3,"dispatched":5,"total":10,'
            b'"target":{"pid":42,"window_id":84},'
            b'"foreground":{"preserved":true}}',
            b"",
        )


def test_run_driver_returns_complete_structured_contract(monkeypatch):
    process = _CompletedProcess(
        json.dumps(
            {
                "ok": True,
                "verified": True,
                "message": "done",
                "target": {"pid": 42},
                "foreground": {"unchanged": True},
            }
        ).encode()
    )

    async def fake_create(*args, **kwargs):
        return process

    monkeypatch.setattr(cua_driver, "_driver_path", lambda: "fake-driver")
    monkeypatch.setattr(cua_driver.asyncio, "create_subprocess_exec", fake_create)

    result = asyncio.run(
        cua_driver._run_driver(
            ["call", "click"],
            stdin_json={"pid": 42},
            timeout=1,
            operation_id="op-1",
        )
    )

    assert result["ok"] is True
    assert result["available"] is True
    assert result["verified"] is True
    assert result["error_code"] is None
    assert result["stdout"]
    assert result["stderr"] == ""
    assert result["details"] == {}
    assert result["target"] == {"pid": 42}
    assert result["foreground"] == {"unchanged": True}
    assert json.loads(process.payload) == {"pid": 42}


def test_run_driver_normalises_unknown_driver_error(monkeypatch):
    process = _CompletedProcess(b'{"ok":false,"error_code":"unsafe_guess"}', returncode=1)

    async def fake_create(*args, **kwargs):
        return process

    monkeypatch.setattr(cua_driver, "_driver_path", lambda: "fake-driver")
    monkeypatch.setattr(cua_driver.asyncio, "create_subprocess_exec", fake_create)

    result = asyncio.run(cua_driver._run_driver(["call", "click"], timeout=1))

    assert result["ok"] is False
    assert result["error_code"] == "background_unavailable"
    assert result["details"]["driver_error_code"] == "unsafe_guess"


def test_run_driver_timeout_cancels_then_reaps(monkeypatch):
    process = _HangingProcess()
    cancelled = []

    async def fake_create(*args, **kwargs):
        return process

    async def fake_cancel(command, operation_id):
        cancelled.append((command, operation_id, process.killed))
        return True

    monkeypatch.setattr(cua_driver, "_driver_path", lambda: "fake-driver")
    monkeypatch.setattr(cua_driver.asyncio, "create_subprocess_exec", fake_create)
    monkeypatch.setattr(cua_driver, "_send_cancel", fake_cancel)

    result = asyncio.run(
        cua_driver._run_driver(["call", "type_text"], timeout=0.001, operation_id="op-timeout")
    )

    assert result["error_code"] == "operation_timeout"
    assert result["details"]["cancellation_sent"] is True
    assert cancelled == [("fake-driver", "op-timeout", False)]
    assert process.killed and process.waited


def test_run_driver_timeout_preserves_cancelled_text_progress(monkeypatch):
    process = _CompletesWhenCancelledProcess()

    async def fake_create(*args, **kwargs):
        return process

    async def fake_cancel(command, operation_id):
        assert (command, operation_id) == ("fake-driver", "op-timeout")
        process.cancel_requested.set()
        return True

    monkeypatch.setattr(cua_driver, "_driver_path", lambda: "fake-driver")
    monkeypatch.setattr(cua_driver.asyncio, "create_subprocess_exec", fake_create)
    monkeypatch.setattr(cua_driver, "_send_cancel", fake_cancel)

    result = asyncio.run(
        cua_driver._run_driver(
            ["call", "type_text"], timeout=0.001, operation_id="op-timeout"
        )
    )

    assert result["error_code"] == "operation_timeout"
    assert result["confirmed_written"] == 3
    assert result["dispatched"] == 5
    assert result["total"] == 10
    assert result["target"] == {"pid": 42, "window_id": 84}
    assert result["foreground"] == {"preserved": True}
    assert result["details"]["driver_completion"]["error_code"] == "operation_cancelled"
    assert process.killed is False


def test_run_driver_timeout_extracts_nested_progress_without_scanning_noise(monkeypatch):
    process = _CompletesWhenCancelledProcess(
        {
            "ok": False,
            "verified": False,
            "error_code": "operation_cancelled",
            "details": {
                "driver_result": {
                    "confirmed_written": 3,
                    "dispatched": 5,
                    "total": 10,
                },
                "telemetry": {
                    "confirmed_written": 91,
                    "dispatched": 92,
                    "total": 93,
                },
            },
        }
    )

    async def fake_create(*args, **kwargs):
        return process

    async def fake_cancel(command, operation_id):
        process.cancel_requested.set()
        return True

    monkeypatch.setattr(cua_driver, "_driver_path", lambda: "fake-driver")
    monkeypatch.setattr(cua_driver.asyncio, "create_subprocess_exec", fake_create)
    monkeypatch.setattr(cua_driver, "_send_cancel", fake_cancel)

    result = asyncio.run(
        cua_driver._run_driver(
            ["call", "type_text"], timeout=0.001, operation_id="op-nested"
        )
    )

    assert result["confirmed_written"] == 3
    assert result["dispatched"] == 5
    assert result["total"] == 10
    assert result["details"]["driver_completion"]["confirmed_written"] == 3


def test_write_diagnostics_prefers_shallow_progress_and_promotes_nested_fields():
    result = cua_driver._add_write_diagnostics(
        {
            "ok": False,
            "confirmed_written": 2,
            "details": {
                "driver_result": {
                    "confirmed_written": 7,
                    "dispatched": 5,
                    "total": 10,
                    "metadata": {"dispatched": 98},
                },
                "unrelated": {"dispatched": 99, "total": 100},
            },
        },
        10,
    )

    assert result["confirmed_written"] == 2
    assert result["dispatched"] == 5
    assert result["total"] == 10
    assert result["confirmed_characters_written"] == 2
    assert result["dispatched_characters"] == 5


def test_run_driver_task_cancellation_cancels_then_reaps(monkeypatch):
    process = _HangingProcess()
    cancelled = []

    async def fake_create(*args, **kwargs):
        return process

    async def fake_cancel(command, operation_id):
        cancelled.append(operation_id)
        return True

    monkeypatch.setattr(cua_driver, "_driver_path", lambda: "fake-driver")
    monkeypatch.setattr(cua_driver.asyncio, "create_subprocess_exec", fake_create)
    monkeypatch.setattr(cua_driver, "_send_cancel", fake_cancel)

    async def exercise():
        task = asyncio.create_task(
            cua_driver._run_driver(["call", "click"], timeout=30, operation_id="op-cancel")
        )
        await process.started.wait()
        task.cancel()
        return await task

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(exercise())

    assert cancelled == ["op-cancel"]
    assert process.killed and process.waited


def test_compatibility_uses_lock_version_protocol_and_required_tools(monkeypatch):
    lock = cua_driver._read_driver_lock()
    calls = []

    async def fake_run(args, **kwargs):
        calls.append(args)
        if args == ["status", "--json"]:
            return {
                "ok": True,
                "compatible": True,
                "driver_version": lock["version"],
                "driver_protocol": lock["protocol"],
                "capabilities": lock["required_tools"],
            }
        if args == ["list-tools"]:
            return {"ok": True, "text": ""}
        raise AssertionError(args)

    monkeypatch.setattr(cua_driver, "_driver_path", lambda: "fake-driver")
    monkeypatch.setattr(cua_driver, "_run_driver", fake_run)

    first = asyncio.run(cua_driver._driver_compatibility())
    second = asyncio.run(cua_driver._driver_compatibility())

    assert first["compatible"] is True
    assert first["actual_version"] == lock["version"]
    assert first["protocol_version"] == lock["protocol"]
    assert first["missing_capabilities"] == []
    assert second == first
    assert calls.count(["status", "--json"]) == 2
    assert ["list-tools"] not in calls


def test_compatibility_fails_closed_on_version_protocol_and_capability(monkeypatch):
    async def fake_run(args, **kwargs):
        if args == ["status", "--json"]:
            return {
                "ok": True,
                "compatible": True,
                "driver_version": "0.7.1",
                "driver_protocol": "upstream",
                "capabilities": ["click"],
            }
        if args == ["list-tools"]:
            return {"ok": True, "text": "click: click"}
        raise AssertionError(args)

    monkeypatch.setattr(cua_driver, "_driver_path", lambda: "fake-driver")
    monkeypatch.setattr(cua_driver, "_run_driver", fake_run)

    result = asyncio.run(cua_driver._driver_compatibility())

    assert result["compatible"] is False
    assert "version" in result["message"]
    assert "protocol" in result["message"]
    assert result["missing_capabilities"]


def test_compatibility_rejects_unconfirmed_active_daemon(monkeypatch):
    lock = cua_driver._read_driver_lock()

    async def fake_run(args, **kwargs):
        assert args == ["status", "--json"]
        return {
            "ok": True,
            "compatible": False,
            "driver_version": lock["version"],
            "driver_protocol": lock["protocol"],
            "capabilities": lock["required_tools"],
            "daemon": {"running": True, "compatible": False},
        }

    monkeypatch.setattr(cua_driver, "_driver_path", lambda: "fake-driver")
    monkeypatch.setattr(cua_driver, "_run_driver", fake_run)

    result = asyncio.run(cua_driver._driver_compatibility())

    assert result["compatible"] is False
    assert "did not confirm" in result["message"]


def test_call_driver_tool_adds_operation_id_and_rejects_unverified_success(monkeypatch):
    captured = {}

    async def fake_compatibility(timeout=10):
        return _compatible(["click"])

    async def fake_run(args, *, stdin_json, timeout, operation_id):
        captured.update(args=args, payload=stdin_json, operation_id=operation_id)
        return {
            "ok": True,
            "available": True,
            "verified": False,
            "stdout": "",
            "stderr": "",
            "target": {"pid": 1},
            "foreground": {},
        }

    monkeypatch.setattr(cua_driver, "_driver_compatibility", fake_compatibility)
    monkeypatch.setattr(cua_driver, "_run_driver", fake_run)

    result = asyncio.run(cua_driver._call_driver_tool("click", {"pid": 1}, 10))

    assert captured["args"] == ["call", "click"]
    assert captured["payload"]["operation_id"] == captured["operation_id"]
    assert result["error_code"] == "background_no_effect"


def test_call_driver_tool_rejects_incompatible_and_missing_tools(monkeypatch):
    async def incompatible(timeout=10):
        result = _compatible([])
        result.update(compatible=False, message="wrong protocol")
        return result

    monkeypatch.setattr(cua_driver, "_driver_compatibility", incompatible)
    result = asyncio.run(cua_driver._call_driver_tool("click", {}, 10))
    assert result["error_code"] == "driver_incompatible"

    async def compatible_without_click(timeout=10):
        return _compatible(["list_windows"])

    monkeypatch.setattr(cua_driver, "_driver_compatibility", compatible_without_click)
    result = asyncio.run(cua_driver._call_driver_tool("click", {}, 10))
    assert result["error_code"] == "tool_unsupported"


def test_call_driver_tool_rejects_wrong_target_and_foreground_change(monkeypatch):
    responses = iter(
        [
            {
                "ok": True,
                "available": True,
                "verified": True,
                "target": {"pid": 999, "window_id": 888},
                "foreground": {"unchanged": True},
            },
            {
                "ok": True,
                "available": True,
                "verified": True,
                "target": {"pid": 1, "window_id": 2},
                "foreground": {"unchanged": False},
            },
        ]
    )

    async def compatible(timeout=10):
        return _compatible(["click"])

    async def fake_run(*_args, **_kwargs):
        return next(responses)

    monkeypatch.setattr(cua_driver, "_driver_compatibility", compatible)
    monkeypatch.setattr(cua_driver, "_run_driver", fake_run)
    arguments = {"pid": 1, "window_id": 2, "delivery_mode": "background"}

    wrong_target = asyncio.run(cua_driver._call_driver_tool("click", arguments, 10))
    changed_foreground = asyncio.run(cua_driver._call_driver_tool("click", arguments, 10))

    assert wrong_target["error_code"] == "target_mismatch"
    assert changed_foreground["error_code"] == "foreground_changed"


def test_call_driver_tool_rejects_invalid_target_before_input(monkeypatch):
    called = False

    async def compatible(timeout=10):
        return _compatible(["click"])

    async def fake_run(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("input must not be dispatched")

    monkeypatch.setattr(cua_driver, "_driver_compatibility", compatible)
    monkeypatch.setattr(cua_driver, "_run_driver", fake_run)

    bad_pid = asyncio.run(
        cua_driver._call_driver_tool("click", {"pid": 0, "delivery_mode": "background"}, 10)
    )
    bad_delivery = asyncio.run(
        cua_driver._call_driver_tool("click", {"pid": 1, "delivery_mode": "surprise"}, 10)
    )
    auto_delivery = asyncio.run(
        cua_driver._call_driver_tool("click", {"pid": 1, "delivery_mode": "auto"}, 10)
    )

    assert bad_pid["error_code"] == "invalid_selector"
    assert bad_delivery["error_code"] == "invalid_selector"
    assert auto_delivery["error_code"] == "invalid_selector"
    assert called is False


def test_cancelled_compatibility_probe_never_continues_to_input(monkeypatch):
    calls = []

    async def fake_run(args, **kwargs):
        calls.append(args)
        raise asyncio.CancelledError

    monkeypatch.setattr(cua_driver, "_driver_path", lambda: "fake-driver")
    monkeypatch.setattr(cua_driver, "_run_driver", fake_run)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cua_driver._call_driver_tool("click", {"pid": 1}, 10))
    assert calls == [["status", "--json"]]


def test_status_exposes_expected_and_actual_compatibility(monkeypatch):
    async def fake_compatibility(timeout=10):
        result = _compatible(["click"])
        result.update(
            status={"ok": True, "stdout": "{}", "stderr": ""},
            lock_path="C:/repo/cua-driver.lock.json",
            published=False,
        )
        return result

    monkeypatch.setattr(cua_driver, "_driver_compatibility", fake_compatibility)
    monkeypatch.setattr(cua_driver, "_driver_path", lambda: "fake-driver")

    result = asyncio.run(cua_driver.cua_driver_status(timeout=10))

    assert result["ok"] is True
    assert result["verified"] is True
    assert result["compatible"] is True
    assert result["actual_version"] == result["expected_version"]
    assert result["protocol_version"] == result["expected_protocol"]
    assert result["capabilities"] == ["click"]
    assert result["missing_capabilities"] == []


def test_launch_validates_exactly_one_selector_and_instance_policy(monkeypatch):
    monkeypatch.setattr(cua_driver, "_driver_path", lambda: None)

    no_selector = asyncio.run(
        cua_driver.cua_driver_launch_app(
            name=None,
            path=None,
            bundle_id=None,
            aumid=None,
            launch_path=None,
            urls=None,
            additional_arguments=None,
            start_minimized=False,
            instance_policy="reuse",
            preserve_foreground=True,
            timeout=60,
        )
    )
    two_selectors = asyncio.run(
        cua_driver.cua_driver_launch_app(
            name="Notepad",
            path="notepad.exe",
            bundle_id=None,
            aumid=None,
            launch_path=None,
            urls=None,
            additional_arguments=None,
            start_minimized=False,
            instance_policy="reuse",
            preserve_foreground=True,
            timeout=60,
        )
    )
    bad_policy = asyncio.run(
        cua_driver.cua_driver_launch_app(
            name="Notepad",
            path=None,
            bundle_id=None,
            aumid=None,
            launch_path=None,
            urls=None,
            additional_arguments=None,
            start_minimized=False,
            instance_policy="sometimes",
            preserve_foreground=True,
            timeout=60,
        )
    )

    assert no_selector["error_code"] == "invalid_selector"
    assert two_selectors["error_code"] == "invalid_selector"
    assert bad_policy["error_code"] == "invalid_selector"


def test_launch_defaults_and_reconciles_pid_window_and_foreground(monkeypatch):
    captured = []
    window_snapshots = iter(
        [
            {"ok": True, "verified": True, "windows": []},
            {"ok": True, "verified": True, "windows": [{"pid": 200, "window_id": 300}]},
        ]
    )

    async def fake_call(tool, arguments, timeout, **kwargs):
        captured.append((tool, arguments, timeout, kwargs))
        if tool == "launch_app":
            return {
                "ok": True,
                "available": True,
                "verified": True,
                "stdout": "",
                "stderr": "",
                "launcher_pid": 100,
                "target_pid": 200,
                "window_id": 300,
                "launched": True,
                "reused": False,
                "target": {
                    "selector": "path",
                    "value": "C:\\Windows\\System32\\notepad.exe",
                    "pid": 200,
                    "window_id": 300,
                },
                "foreground": {"before": 50, "after": 50},
            }
        assert tool == "list_windows"
        return next(window_snapshots)

    monkeypatch.setattr(cua_driver, "_call_driver_tool", fake_call)

    signature = inspect.signature(cua_driver.cua_driver_launch_app).parameters
    assert signature["start_minimized"].default.default is False
    assert signature["instance_policy"].default.default == "reuse"
    assert signature["preserve_foreground"].default.default is True

    result = asyncio.run(
        cua_driver.cua_driver_launch_app(
            name=None,
            path="  C:\\Windows\\System32\\notepad.exe  ",
            bundle_id=None,
            aumid=None,
            launch_path=None,
            urls=None,
            additional_arguments=None,
            start_minimized=False,
            instance_policy="reuse",
            preserve_foreground=True,
            timeout=60,
        )
    )

    assert result["ok"] is True
    assert result["verified"] is True
    assert result["target"] == {
        "pid": 200,
        "window_id": 300,
        "selector": "path",
        "value": "C:\\Windows\\System32\\notepad.exe",
    }
    launch_call = next(call for call in captured if call[0] == "launch_app")
    launch_arguments = launch_call[1]
    assert launch_arguments["instance_policy"] == "reuse"
    assert launch_arguments["preserve_foreground"] is True
    assert launch_arguments["start_minimized"] is False
    list_window_calls = [call for call in captured if call[0] == "list_windows"]
    assert len(list_window_calls) == 2
    assert all(call[3] == {"require_verified": False} for call in list_window_calls)


def test_launch_fails_when_window_or_foreground_cannot_be_verified(monkeypatch):
    async def fake_call(tool, arguments, timeout, **kwargs):
        if tool == "launch_app":
            return {
                "ok": True,
                "available": True,
                "verified": True,
                "stdout": "",
                "stderr": "",
                "launcher_pid": 100,
                "target_pid": 200,
                "window_id": 300,
                "launched": True,
                "reused": False,
                "target": {"pid": 200, "window_id": 300},
                "foreground": {},
            }
        return {"ok": True, "windows": [{"pid": 999, "window_id": 300}]}

    monkeypatch.setattr(cua_driver, "_call_driver_tool", fake_call)
    result = asyncio.run(
        cua_driver.cua_driver_launch_app(
            name="Notepad",
            path=None,
            bundle_id=None,
            aumid=None,
            launch_path=None,
            urls=None,
            additional_arguments=None,
            start_minimized=False,
            instance_policy="new",
            preserve_foreground=True,
            timeout=60,
        )
    )

    assert result["error_code"] == "target_mismatch"


def test_restore_without_activate_delegates_and_checks_foreground(monkeypatch):
    captured = {}

    async def fake_call(tool, arguments, timeout, **kwargs):
        captured.update(tool=tool, arguments=arguments, timeout=timeout)
        return {
            "ok": True,
            "available": True,
            "verified": True,
            "stdout": "",
            "stderr": "",
            "window_id": 123,
            "target": {"pid": 45, "window_id": 123},
            "foreground": {"unchanged": True},
        }

    monkeypatch.setattr(cua_driver, "_call_driver_tool", fake_call)
    result = asyncio.run(
        cua_driver.cua_driver_restore_without_activate(window_id=123, pid=45, timeout=20)
    )

    assert result["ok"] is True
    assert captured == {
        "tool": "restore_without_activate",
        "arguments": {"window_id": 123, "pid": 45},
        "timeout": 20,
    }


def test_restore_without_activate_resolves_omitted_pid_strictly(monkeypatch):
    calls = []

    async def fake_call(tool, arguments, timeout, **kwargs):
        calls.append((tool, arguments, timeout, kwargs))
        if tool == "list_windows":
            return {
                "ok": True,
                "available": True,
                "windows": [{"pid": 45, "window_id": 123}],
            }
        return {
            "ok": True,
            "available": True,
            "verified": True,
            "window_id": 123,
            "target": {"pid": 45, "window_id": 123},
            "foreground": {"unchanged": True},
        }

    monkeypatch.setattr(cua_driver, "_call_driver_tool", fake_call)
    result = asyncio.run(
        cua_driver.cua_driver_restore_without_activate(window_id=123, pid=None, timeout=20)
    )

    assert result["ok"] is True
    assert calls == [
        ("list_windows", {}, 20, {"require_verified": False}),
        ("restore_without_activate", {"window_id": 123, "pid": 45}, 20, {}),
    ]


@pytest.mark.parametrize(
    ("windows", "error_code"),
    [
        ([], "target_mismatch"),
        ([{"pid": 45, "window_id": 123}, {"pid": 45, "window_id": 123}], "target_ambiguous"),
        ([{"window_id": 123}], "target_mismatch"),
    ],
)
def test_restore_without_activate_rejects_unresolved_pid(monkeypatch, windows, error_code):
    async def fake_call(tool, arguments, timeout, **kwargs):
        assert tool == "list_windows"
        return {"ok": True, "available": True, "windows": windows}

    monkeypatch.setattr(cua_driver, "_call_driver_tool", fake_call)
    result = asyncio.run(
        cua_driver.cua_driver_restore_without_activate(window_id=123, pid=None, timeout=20)
    )

    assert result["ok"] is False
    assert result["error_code"] == error_code


def test_screenshot_uses_formal_tool_and_returns_mcp_image(monkeypatch):
    async def fake_call(tool, arguments, timeout, **kwargs):
        assert tool == "screenshot"
        assert arguments == {"pid": 45, "window_id": 123}
        return {
            "ok": True,
            "available": True,
            "verified": True,
            "stdout": "",
            "stderr": "",
            "png_base64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
            "mime_type": "image/png",
            "width": 1,
            "height": 1,
            "target": {"pid": 45, "window_id": 123},
            "foreground": {"unchanged": True},
        }

    monkeypatch.setattr(cua_driver, "_call_driver_tool", fake_call)
    result = asyncio.run(cua_driver.cua_driver_screenshot(pid=45, window_id=123, timeout=60))

    assert isinstance(result, list)
    assert isinstance(result[0], types.TextContent)
    assert isinstance(result[1], types.ImageContent)
    assert result[1].data.startswith("iVBORw0KGgo")
    assert result[1].mimeType == "image/png"


def test_screenshot_fails_closed_on_incomplete_payload(monkeypatch):
    async def fake_call(tool, arguments, timeout, **kwargs):
        return {
            "ok": True,
            "available": True,
            "verified": True,
            "stdout": "",
            "stderr": "",
            "target": {"desktop": True},
            "foreground": {},
        }

    monkeypatch.setattr(cua_driver, "_call_driver_tool", fake_call)
    result = asyncio.run(cua_driver.cua_driver_screenshot(pid=None, window_id=None, timeout=60))

    assert result["error_code"] == "invalid_selector"


def test_generic_call_routes_specialised_tools_through_safety_wrappers(monkeypatch):
    routed = []

    async def fake_launch(**kwargs):
        routed.append(("launch", kwargs))
        return {"ok": False, "error_code": "background_unavailable"}

    async def fake_screenshot(**kwargs):
        routed.append(("screenshot", kwargs))
        return {"ok": False, "error_code": "target_mismatch"}

    async def fake_restore(**kwargs):
        routed.append(("restore", kwargs))
        return {"ok": False, "error_code": "foreground_changed"}

    monkeypatch.setattr(cua_driver, "cua_driver_launch_app", fake_launch)
    monkeypatch.setattr(cua_driver, "cua_driver_screenshot", fake_screenshot)
    monkeypatch.setattr(cua_driver, "cua_driver_restore_without_activate", fake_restore)

    launch = asyncio.run(
        cua_driver.cua_driver_call(
            tool="launch_app", arguments={"name": "Notepad"}, timeout=10
        )
    )
    screenshot = asyncio.run(
        cua_driver.cua_driver_call(
            tool="screenshot", arguments={"pid": 1, "window_id": 2}, timeout=11
        )
    )
    restore = asyncio.run(
        cua_driver.cua_driver_call(
            tool="restore_without_activate", arguments={"pid": 1, "window_id": 2}, timeout=12
        )
    )

    assert launch["error_code"] == "background_unavailable"
    assert screenshot["error_code"] == "target_mismatch"
    assert restore["error_code"] == "foreground_changed"
    assert routed == [
        (
            "launch",
            {
                "name": "Notepad",
                "path": None,
                "bundle_id": None,
                "aumid": None,
                "launch_path": None,
                "urls": None,
                "additional_arguments": None,
                "start_minimized": False,
                "instance_policy": "reuse",
                "preserve_foreground": True,
                "timeout": 10,
            },
        ),
        ("screenshot", {"pid": 1, "window_id": 2, "timeout": 11}),
        ("restore", {"window_id": 2, "pid": 1, "timeout": 12}),
    ]


@pytest.mark.parametrize(
    "tool",
    [
        "future_tool",
        "type_text_chars",
        "set_config",
        "start_recording",
        "stop_recording",
        "get_recording_state",
        "replay_trajectory",
        "install_ffmpeg",
        "start_session",
        "end_session",
        "cancel_operation",
        "move_cursor",
        "mouse_button_down",
        "mouse_button_up",
        "mouse_drag",
        "parallel_mouse_drag",
        "set_agent_cursor_enabled",
        "set_agent_cursor_motion",
        "set_agent_cursor_style",
    ],
)
def test_generic_call_rejects_unaudited_mutators_without_calling_driver(monkeypatch, tool):
    calls = []

    async def forbidden_call(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("blocked tools must not reach the driver")

    monkeypatch.setattr(cua_driver, "_call_driver_tool", forbidden_call)
    monkeypatch.setattr(cua_driver, "_driver_path", lambda: "cua-driver")

    result = asyncio.run(
        cua_driver.cua_driver_call(tool=tool, arguments={"unsafe": True}, timeout=10)
    )

    assert result["ok"] is False
    assert result["available"] is True
    assert result["verified"] is False
    assert result["error_code"] == "tool_unsupported"
    assert result["details"]["tool"] == tool
    assert calls == []


@pytest.mark.parametrize(
    ("tool", "arguments", "require_verified"),
    [
        (
            "right_click",
            {"pid": 1, "window_id": 2, "x": 3, "y": 4, "delivery_mode": "background"},
            True,
        ),
        ("get_config", {}, False),
        ("get_window_state", {"pid": 1, "window_id": 2}, True),
    ],
)
def test_generic_call_allows_audited_strict_and_read_only_tools(
    monkeypatch, tool, arguments, require_verified
):
    calls = []

    async def fake_call(called_tool, called_arguments, timeout, **kwargs):
        calls.append((called_tool, called_arguments, timeout, kwargs))
        return {"ok": False, "error_code": "background_unavailable"}

    monkeypatch.setattr(cua_driver, "_call_driver_tool", fake_call)

    result = asyncio.run(
        cua_driver.cua_driver_call(tool=tool, arguments=arguments, timeout=13)
    )

    assert result["error_code"] == "background_unavailable"
    assert calls == [(tool, arguments, 13, {"require_verified": require_verified})]


def test_generic_call_allows_page_mutator_to_fail_closed_in_driver(monkeypatch):
    calls = []
    arguments = {"action": "execute_javascript", "pid": 1, "script": "window.close()"}

    async def fake_call(tool, payload, timeout, **kwargs):
        calls.append((tool, payload, timeout, kwargs))
        return {
            "ok": False,
            "verified": False,
            "error_code": "tool_unsupported",
            "message": "Windows page mutations are disabled.",
        }

    monkeypatch.setattr(cua_driver, "_call_driver_tool", fake_call)

    result = asyncio.run(
        cua_driver.cua_driver_call(tool="page", arguments=arguments, timeout=14)
    )

    assert result["error_code"] == "tool_unsupported"
    assert calls == [("page", arguments, 14, {"require_verified": False})]


def test_type_text_uses_dynamic_timeout_and_reports_confirmed_write_count(monkeypatch):
    captured = {}

    async def fake_call(tool, arguments, timeout, **kwargs):
        captured.update(tool=tool, arguments=arguments, timeout=timeout)
        return {
            "ok": False,
            "available": True,
            "verified": False,
            "error_code": "operation_timeout",
            "message": "timeout",
            "stdout": "",
            "stderr": "",
            "details": {"characters_written": 3},
            "target": {"pid": 45},
            "foreground": {},
        }

    monkeypatch.setattr(cua_driver, "_call_driver_tool", fake_call)
    text = "x" * 100
    result = asyncio.run(
        cua_driver.cua_driver_type_text(
            pid=45,
            text=text,
            window_id=123,
            element_index=None,
            delay_ms=200,
            dispatch="background",
            timeout=3,
        )
    )

    assert captured["timeout"] == 25
    assert captured["arguments"]["delivery_mode"] == "background"
    assert "dispatch" not in captured["arguments"]
    assert result["confirmed_characters_written"] == 3
    assert result["details"]["requested_characters"] == 100


def test_type_text_preserves_driver_confirmed_and_dispatched_progress(monkeypatch):
    async def fake_call(tool, arguments, timeout, **kwargs):
        assert tool == "type_text"
        return {
            "ok": False,
            "available": True,
            "verified": False,
            "error_code": "operation_cancelled",
            "message": "cancelled",
            "stdout": "",
            "stderr": "",
            "confirmed_written": 3,
            "dispatched": 5,
            "details": {},
            "target": {"pid": 45, "window_id": 123},
            "foreground": {"preserved": True},
        }

    monkeypatch.setattr(cua_driver, "_call_driver_tool", fake_call)
    result = asyncio.run(
        cua_driver.cua_driver_type_text(
            pid=45,
            text="abcdefghij",
            window_id=123,
            element_index=None,
            delay_ms=0,
            dispatch="background",
            timeout=3,
        )
    )

    assert result["confirmed_characters_written"] == 3
    assert result["dispatched_characters"] == 5
    assert result["details"]["confirmed_characters_written"] == 3
    assert result["details"]["dispatched_characters"] == 5


def test_unavailable_contract_preserves_legacy_and_strict_fields(monkeypatch):
    monkeypatch.setattr(cua_driver, "_driver_command", lambda: "missing-driver")
    result = cua_driver._unavailable()

    assert result["ok"] is False
    assert result["available"] is False
    assert result["command"] == "missing-driver"
    assert result["error_code"] == "driver_incompatible"
    for field in ("stdout", "stderr", "verified", "message", "details", "target", "foreground"):
        assert field in result


def test_diagnostic_wrappers_remain_available_without_compatibility_gate(monkeypatch):
    commands = []
    diagnostics = []

    async def fake_run(args, **kwargs):
        commands.append((args, kwargs))
        if args == ["list-tools"]:
            return {"ok": True, "text": "click: click\nlist_windows: list", "stdout": "", "stderr": ""}
        return {"ok": True, "stdout": "", "stderr": "", "verified": False}

    async def fake_diagnostic(tool, arguments, timeout):
        diagnostics.append((tool, arguments, timeout))
        return {"ok": True}

    monkeypatch.setattr(cua_driver, "_run_driver", fake_run)
    monkeypatch.setattr(cua_driver, "_call_diagnostic_tool", fake_diagnostic)

    doctor = asyncio.run(cua_driver.cua_driver_doctor(json_output=True, timeout=12))
    permissions = asyncio.run(cua_driver.cua_driver_check_permissions(timeout=13))
    tools_result = asyncio.run(cua_driver.cua_driver_list_tools(timeout=14))
    described = asyncio.run(cua_driver.cua_driver_describe_tool(tool="click", timeout=15))

    assert doctor["ok"] and permissions["ok"] and described["ok"]
    assert commands[0][0] == ["doctor", "--json"]
    assert commands[-1][0] == ["describe", "click"]
    assert diagnostics == [("check_permissions", {}, 13)]
    assert tools_result["tools"] == ["click", "list_windows"]


def test_thin_tool_wrappers_forward_complete_arguments(monkeypatch):
    calls = []

    async def fake_call(tool, arguments, timeout, **kwargs):
        calls.append((tool, arguments, timeout, kwargs))
        return {
            "ok": False,
            "available": True,
            "verified": False,
            "error_code": "background_unavailable",
            "message": "stub",
            "stdout": "",
            "stderr": "",
            "details": {},
            "target": {},
            "foreground": {},
        }

    monkeypatch.setattr(cua_driver, "_call_driver_tool", fake_call)

    async def exercise():
        await cua_driver.cua_driver_call(
            tool="right_click",
            arguments={"pid": 1, "window_id": 2, "x": 3, "y": 4},
            timeout=11,
        )
        await cua_driver.cua_driver_list_apps(timeout=12)
        await cua_driver.cua_driver_kill_app(pid=1, timeout=13)
        await cua_driver.cua_driver_list_windows(pid=2, timeout=14)
        await cua_driver.cua_driver_list_windows(pid=None, timeout=15)
        await cua_driver.cua_driver_get_window_state(
            pid=3, window_id=4, capture_mode="vision", query="Button", timeout=16
        )
        await cua_driver.cua_driver_click(
            pid=5,
            window_id=6,
            element_index=7,
            x=8,
            y=9,
            button="right",
            count=2,
            dispatch="foreground",
            from_zoom=True,
            timeout=17,
        )
        await cua_driver.cua_driver_double_click(
            pid=10,
            window_id=11,
            element_index=12,
            x=13,
            y=14,
            dispatch="foreground",
            from_zoom=True,
            timeout=18,
        )
        await cua_driver.cua_driver_press_key(
            pid=15,
            key="home",
            window_id=16,
            element_index=17,
            modifiers=["shift"],
            dispatch="foreground",
            timeout=19,
        )
        await cua_driver.cua_driver_hotkey(
            pid=18,
            keys=["ctrl", "a"],
            window_id=19,
            dispatch="foreground",
            timeout=20,
        )
        await cua_driver.cua_driver_set_value(
            pid=20, window_id=21, element_index=22, value="value", timeout=21
        )
        await cua_driver.cua_driver_scroll(
            pid=23,
            window_id=24,
            element_index=25,
            direction="up",
            by="page",
            amount=2,
            dispatch="foreground",
            timeout=22,
        )
        await cua_driver.cua_driver_zoom(
            pid=26, window_id=27, x1=1, y1=2, x2=3, y2=4, timeout=23
        )
        await cua_driver.cua_driver_bring_to_front(pid=28, window_id=29, timeout=24)
        await cua_driver.cua_driver_set_agent_cursor_enabled(enabled=False, timeout=25)
        await cua_driver.cua_driver_get_agent_cursor_state(timeout=26)

    asyncio.run(exercise())

    assert [call[0] for call in calls] == [
        "right_click",
        "list_apps",
        "kill_app",
        "list_windows",
        "list_windows",
        "get_window_state",
        "click",
        "double_click",
        "press_key",
        "hotkey",
        "set_value",
        "scroll",
        "zoom",
        "bring_to_front",
        "set_agent_cursor_enabled",
        "get_agent_cursor_state",
    ]
    assert calls[3][1] == {"pid": 2}
    assert calls[4][1] == {}
    assert calls[1][3] == {"require_verified": False}
    assert calls[3][3] == {"require_verified": False}
    assert calls[4][3] == {"require_verified": False}
    assert calls[5][1]["query"] == "Button"
    assert calls[6][1]["from_zoom"] is True
    for call_index in (6, 7, 8, 9, 11):
        assert calls[call_index][1]["delivery_mode"] == "foreground"
        assert "dispatch" not in calls[call_index][1]
    assert calls[9][1]["window_id"] == 19
    assert calls[11][1]["by"] == "page"


def test_get_window_state_default_uses_the_canonical_capture_mode() -> None:
    parameter = inspect.signature(cua_driver.cua_driver_get_window_state).parameters[
        "capture_mode"
    ]
    assert parameter.default.default == "ax"


def test_command_path_install_hint_and_lock_override(monkeypatch, tmp_path):
    executable = tmp_path / "cua-driver.exe"
    executable.write_bytes(b"driver")
    monkeypatch.setenv("CUA_DRIVER_COMMAND", str(executable))
    assert cua_driver._driver_command() == str(executable)
    assert cua_driver._driver_path() == str(executable)

    monkeypatch.setenv("CUA_DRIVER_COMMAND", str(tmp_path / "missing.exe"))
    assert cua_driver._driver_path() is None
    monkeypatch.setattr(cua_driver.shutil, "which", lambda command: f"PATH/{command}")
    monkeypatch.setenv("CUA_DRIVER_COMMAND", "cua-driver-custom")
    assert cua_driver._driver_path() == "PATH/cua-driver-custom"

    monkeypatch.setattr(cua_driver.platform, "system", lambda: "Linux")
    assert "lock" in cua_driver._install_hint()
    monkeypatch.setenv("CUA_DRIVER_LOCK", str(tmp_path / "driver-lock.json"))
    assert cua_driver._lock_path() == (tmp_path / "driver-lock.json").resolve()


@pytest.mark.parametrize(
    "payload, message",
    [
        ("not-json", "Unable to read"),
        ({"schema_version": 2}, "schema_version"),
        ({"schema_version": 1, "version": "", "protocol": "p", "required_tools": []}, "version"),
        ({"schema_version": 1, "version": "1.0.0", "protocol": "", "required_tools": []}, "protocol"),
        ({"schema_version": 1, "version": "1.0.0", "protocol": "p", "required_tools": [1]}, "required_tools"),
    ],
)
def test_driver_lock_validation_errors(monkeypatch, tmp_path, payload, message):
    lock_path = tmp_path / "lock.json"
    lock_path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("CUA_DRIVER_LOCK", str(lock_path))

    with pytest.raises(ValueError, match=message):
        cua_driver._read_driver_lock()


def test_result_parsing_and_normalisation_edge_cases():
    assert cua_driver._parse_stdout("   ") == {}
    assert cua_driver._parse_stdout("plain output") == {"text": "plain output"}
    assert cua_driver._normalise_version(None) is None
    assert cua_driver._normalise_version("development") == "development"

    scalar = cua_driver._normalise_result(
        ["value"], return_code=0, stdout="[]", stderr=""
    )
    assert scalar["result"] == ["value"]
    assert scalar["message"] == "cua-driver completed successfully."

    failed = cua_driver._normalise_result(
        {"ok": False, "details": "detail", "foreground": 99},
        return_code=0,
        stdout="",
        stderr="driver failed",
    )
    assert failed["error_code"] == "background_unavailable"
    assert failed["details"] == {"driver_details": "detail"}
    assert failed["foreground"] == {"current": 99}
    assert failed["message"] == "driver failed"

    non_boolean = cua_driver._normalise_result(
        {"ok": "false", "verified": "false"},
        return_code=0,
        stdout="",
        stderr="",
    )
    assert non_boolean["ok"] is False
    assert non_boolean["verified"] is False
    assert non_boolean["error_code"] == "driver_incompatible"


def test_send_cancel_success_timeout_and_spawn_error(monkeypatch):
    success = _CompletedProcess(
        b'{"ok":true,"verified":true,"operation_id":"op","found":true,"cancelled":true}'
    )
    commands = []

    async def successful_create(*args, **kwargs):
        commands.append((args, kwargs))
        return success

    monkeypatch.setattr(cua_driver.asyncio, "create_subprocess_exec", successful_create)
    assert asyncio.run(cua_driver._send_cancel("driver", "op")) is True
    assert commands[0][0] == ("driver", "cancel", "op")
    assert success.payload is None

    hanging = _HangingProcess()

    async def hanging_create(*args, **kwargs):
        return hanging

    async def immediate_timeout(awaitable, timeout):
        awaitable.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr(cua_driver.asyncio, "create_subprocess_exec", hanging_create)
    monkeypatch.setattr(cua_driver.asyncio, "wait_for", immediate_timeout)
    assert asyncio.run(cua_driver._send_cancel("driver", "op")) is False
    assert hanging.killed and hanging.waited

    async def failed_create(*args, **kwargs):
        raise OSError("cannot spawn")

    monkeypatch.setattr(cua_driver.asyncio, "create_subprocess_exec", failed_create)
    assert asyncio.run(cua_driver._send_cancel("driver", "op")) is False


def test_run_driver_unavailable_and_spawn_failure(monkeypatch):
    monkeypatch.setattr(cua_driver, "_driver_path", lambda: None)
    assert asyncio.run(cua_driver._run_driver(["status"]))["available"] is False

    async def failed_create(*args, **kwargs):
        raise OSError("blocked")

    monkeypatch.setattr(cua_driver, "_driver_path", lambda: "driver")
    monkeypatch.setattr(cua_driver.asyncio, "create_subprocess_exec", failed_create)
    result = asyncio.run(cua_driver._run_driver(["status"]))
    assert result["error_code"] == "driver_incompatible"
    assert "blocked" in result["stderr"]


def test_tool_name_and_identity_helpers_cover_supported_shapes():
    assert cua_driver._tool_names({"tools": {"click": {}, "scroll": {}}}) == {"click", "scroll"}
    assert cua_driver._tool_names(
        {"capabilities": [{"name": "click"}, "scroll: scroll", {"other": "ignored"}]}
    ) == {"click", "scroll"}
    assert cua_driver._positive_int(True) is None
    assert cua_driver._positive_int("bad") is None
    assert cua_driver._positive_int("4") == 4
    assert cua_driver._windows_from_result({"result": [{"pid": 1}, "bad"]}) == [{"pid": 1}]
    assert cua_driver._windows_from_result({"result": "bad"}) == []
    assert cua_driver._window_identity({"owner_pid": "2", "hwnd": "3"}) == (2, 3)
    assert cua_driver._foreground_preserved({"foreground_unchanged": False}) is False
    assert cua_driver._foreground_preserved({"foreground": 1}) is None
    assert cua_driver._foreground_preserved({"foreground": {}}) is None


def test_compatibility_reports_lock_error_unavailable_and_status_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(cua_driver, "_lock_path", lambda: tmp_path / "missing.json")
    lock_error = asyncio.run(cua_driver._driver_compatibility())
    assert lock_error["compatible"] is False
    assert "missing.json" in lock_error["message"]

    monkeypatch.setattr(cua_driver, "_lock_path", lambda: cua_driver.Path(__file__).resolve().parents[2] / "cua-driver.lock.json")
    monkeypatch.setattr(cua_driver, "_driver_path", lambda: None)
    unavailable = asyncio.run(cua_driver._driver_compatibility())
    assert unavailable["available"] is False
    assert unavailable["expected_version"] == "0.7.1-sc.1"

    cua_driver._reset_driver_cache()
    lock = cua_driver._read_driver_lock()

    async def fake_run(args, **kwargs):
        if args == ["status", "--json"]:
            return {"ok": False, "driver_protocol": 123, "capabilities": lock["required_tools"]}
        if args == ["--version"]:
            return {"ok": True, "stdout": f"cua-driver {lock['version']}"}
        if args == ["list-tools"]:
            return {"ok": True, "text": ""}
        raise AssertionError(args)

    monkeypatch.setattr(cua_driver, "_driver_path", lambda: "fake-driver")
    monkeypatch.setattr(cua_driver, "_run_driver", fake_run)
    fallback = asyncio.run(cua_driver._driver_compatibility())
    assert fallback["actual_version"] == lock["version"]
    assert fallback["protocol_version"] is None
    assert "status command failed" in fallback["message"]


def test_diagnostic_unknown_tool_is_normalised(monkeypatch):
    async def fake_run(args, **kwargs):
        return {"ok": False, "stdout": "", "stderr": "Unknown tool: missing"}

    monkeypatch.setattr(cua_driver, "_run_driver", fake_run)
    result = asyncio.run(cua_driver._call_diagnostic_tool("missing", {}, 1))
    assert result["error_code"] == "tool_unsupported"


def test_reconcile_launch_rejects_ambiguous_and_unverified_foreground(monkeypatch):
    base = {
        "ok": True,
        "available": True,
        "verified": True,
        "stdout": "",
        "stderr": "",
        "launcher_pid": 1,
        "target_pid": 2,
        "window_id": 3,
        "launched": True,
        "reused": False,
        "target": {"selector": "name", "value": "Notepad", "pid": 2, "window_id": 3},
        "foreground": {},
    }

    async def duplicate_windows(tool, arguments, timeout, **kwargs):
        return {"ok": True, "windows": [{"pid": 2, "window_id": 3}, {"pid": 2, "window_id": 3}]}

    monkeypatch.setattr(cua_driver, "_call_driver_tool", duplicate_windows)
    ambiguous = asyncio.run(
        cua_driver._reconcile_launch(
            dict(base),
            prelaunch_windows=[],
            selector_name="name",
            selector_value="Notepad",
            instance_policy="reuse",
            preserve_foreground=True,
            timeout=1,
        )
    )
    assert ambiguous["error_code"] == "target_ambiguous"

    async def exact_window(tool, arguments, timeout, **kwargs):
        return {"ok": True, "windows": [{"pid": 2, "window_id": 3}]}

    monkeypatch.setattr(cua_driver, "_call_driver_tool", exact_window)
    foreground = asyncio.run(
        cua_driver._reconcile_launch(
            dict(base),
            prelaunch_windows=[],
            selector_name="name",
            selector_value="Notepad",
            instance_policy="reuse",
            preserve_foreground=True,
            timeout=1,
        )
    )
    assert foreground["error_code"] == "foreground_changed"


def test_restore_and_screenshot_failure_branches(monkeypatch):
    responses = [
        {"ok": False, "error_code": "background_unavailable"},
        {"ok": True, "window_id": 999, "target": {"pid": 45}, "foreground": {"unchanged": True}},
        {"ok": True, "window_id": 123, "target": {"pid": 45}, "foreground": {}},
    ]

    async def fake_call(tool, arguments, timeout, **kwargs):
        return responses.pop(0)

    monkeypatch.setattr(cua_driver, "_call_driver_tool", fake_call)
    first = asyncio.run(cua_driver.cua_driver_restore_without_activate(123, 45, 1))
    second = asyncio.run(cua_driver.cua_driver_restore_without_activate(123, 45, 1))
    third = asyncio.run(cua_driver.cua_driver_restore_without_activate(123, 45, 1))
    assert first["error_code"] == "background_unavailable"
    assert second["error_code"] == "target_mismatch"
    assert third["error_code"] == "foreground_changed"

    missing_target = cua_driver._normalise_screenshot(
        {
            "ok": True,
            "screenshot_png_b64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
            "screenshot_mime_type": "image/png",
            "width": 1,
            "height": 1,
            "target": {},
        },
        expected_pid=45,
        expected_window_id=123,
    )
    assert missing_target["error_code"] == "target_mismatch"
    assert cua_driver._normalise_screenshot(
        {"ok": False}, expected_pid=45, expected_window_id=123
    ) == {"ok": False}
    assert cua_driver._add_write_diagnostics({"ok": True}, 3) == {"ok": True}
