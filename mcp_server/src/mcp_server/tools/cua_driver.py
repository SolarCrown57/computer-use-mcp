from __future__ import annotations

import asyncio
import base64
import binascii
import json
import math
import os
import platform
import re
import shutil
import struct
import uuid
import zlib
from contextlib import suppress
from pathlib import Path
from typing import Any, Optional

from mcp import types
from pydantic import Field

from mcp_server.common.config import cua_driver_config
from mcp_server.tools import MCP


_LOCK_FILE_NAME = "cua-driver.lock.json"
_SUPPORTED_LOCK_SCHEMA = 1
_DEFAULT_ERROR_CODE = "background_unavailable"
_TEXT_TIMEOUT_MARGIN_SECONDS = 5
_CANCEL_TIMEOUT_SECONDS = 2
_CANCEL_GRACE_SECONDS = 0.25
_ERROR_CODES = {
    "driver_incompatible",
    "tool_unsupported",
    "invalid_selector",
    "target_ambiguous",
    "target_mismatch",
    "background_unavailable",
    "background_no_effect",
    "foreground_changed",
    "operation_timeout",
    "operation_cancelled",
}

_compatibility_cache_key: Optional[tuple[Any, ...]] = None
_compatibility_cache: Optional[dict[str, Any]] = None

_SPECIALISED_TOOLS = {
    "launch_app",
    "restore_without_activate",
    "screenshot",
}
_GENERIC_CALL_STRICT_TOOLS = {
    "bring_to_front",
    "click",
    "double_click",
    "drag",
    "hotkey",
    "kill_app",
    "press_key",
    "right_click",
    "scroll",
    "set_value",
    "type_text",
}
_GENERIC_CALL_READ_ONLY_TOOLS = {
    "check_for_update",
    "check_permissions",
    "debug_window_info",
    "get_accessibility_tree",
    "get_agent_cursor_state",
    "get_config",
    "get_cursor_position",
    "get_desktop_state",
    "get_screen_size",
    "get_window_state",
    "health_report",
    "list_apps",
    "list_windows",
    "probe",
    "zoom",
}
_GENERIC_CALL_DRIVER_GATED_TOOLS = {"page"}
_GENERIC_CALL_TOOLS = (
    _GENERIC_CALL_STRICT_TOOLS
    | _GENERIC_CALL_READ_ONLY_TOOLS
    | _GENERIC_CALL_DRIVER_GATED_TOOLS
)
_TARGETED_WINDOW_TOOLS = {
    "click",
    "double_click",
    "drag",
    "get_window_state",
    "hotkey",
    "press_key",
    "right_click",
    "restore_without_activate",
    "screenshot",
    "scroll",
    "set_value",
    "type_text",
    "zoom",
}
_TARGETED_PID_TOOLS = _TARGETED_WINDOW_TOOLS | {"bring_to_front", "kill_app"}
_FOREGROUND_EXEMPT_TOOLS = {
    "bring_to_front",
    "kill_app",
    "launch_app",
}
_DELIVERY_MODE_TOOLS = {
    "click",
    "double_click",
    "drag",
    "hotkey",
    "press_key",
    "right_click",
    "scroll",
    "type_text",
}
_WRITE_PROGRESS_ALIASES = {
    "confirmed_written": (
        "confirmed_written",
        "confirmed_characters_written",
        "characters_written",
        "written_count",
        "written",
    ),
    "dispatched": (
        "dispatched",
        "dispatched_characters",
        "dispatched_written",
    ),
    "total": ("total",),
}
_WRITE_PROGRESS_CONTAINERS = ("details", "driver_result", "driver_completion")


def _driver_command() -> str:
    return os.getenv("CUA_DRIVER_COMMAND") or cua_driver_config.get("command", "cua-driver")


def _driver_path() -> Optional[str]:
    command = _driver_command()
    if os.path.isabs(command) or os.sep in command or bool(os.altsep and os.altsep in command):
        return command if os.path.exists(command) else None
    return shutil.which(command)


def _install_hint() -> str:
    if platform.system() == "Windows":
        return "powershell -ExecutionPolicy Bypass -File .\\scripts\\install-cua-driver.ps1"
    return "Use the pinned release described by cua-driver.lock.json."


def _lock_path() -> Path:
    override = os.getenv("CUA_DRIVER_LOCK")
    if override:
        return Path(override).expanduser().resolve()
    for parent in Path(__file__).resolve().parents:
        candidate = parent / _LOCK_FILE_NAME
        if candidate.exists():
            return candidate
    return Path(__file__).resolve().parents[4] / _LOCK_FILE_NAME


def _read_driver_lock() -> dict[str, Any]:
    path = _lock_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Unable to read {path}: {exc}") from exc

    if not isinstance(data, dict) or data.get("schema_version") != _SUPPORTED_LOCK_SCHEMA:
        raise ValueError(f"Unsupported or missing schema_version in {path}")
    if not isinstance(data.get("version"), str) or not data["version"].strip():
        raise ValueError(f"Missing version in {path}")
    if not isinstance(data.get("protocol"), str) or not data["protocol"].strip():
        raise ValueError(f"Missing protocol in {path}")
    required_tools = data.get("required_tools")
    if not isinstance(required_tools, list) or not all(
        isinstance(tool, str) and tool.strip() for tool in required_tools
    ):
        raise ValueError(f"Invalid required_tools in {path}")
    return data


def _reset_driver_cache() -> None:
    global _compatibility_cache, _compatibility_cache_key
    _compatibility_cache = None
    _compatibility_cache_key = None


def _failure(
    error_code: str,
    message: str,
    *,
    available: bool,
    stdout: str = "",
    stderr: str = "",
    details: Optional[dict[str, Any]] = None,
    target: Optional[dict[str, Any]] = None,
    foreground: Optional[dict[str, Any]] = None,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "ok": False,
        "available": available,
        "stdout": stdout,
        "stderr": stderr,
        "verified": False,
        "error_code": error_code,
        "message": message,
        "details": details or {},
        "target": target or {},
        "foreground": foreground or {},
        **extra,
    }


def _unavailable() -> dict[str, Any]:
    return _failure(
        "driver_incompatible",
        "cua-driver is not installed or not on PATH.",
        available=False,
        command=_driver_command(),
        install=_install_hint(),
    )


def _normalise_result(
    parsed: Any,
    *,
    return_code: int,
    stdout: str,
    stderr: str,
) -> dict[str, Any]:
    payload = dict(parsed) if isinstance(parsed, dict) else {"result": parsed}
    raw_ok = payload.get("ok")
    protocol_error = raw_ok is not None and not isinstance(raw_ok, bool)
    driver_ok = return_code == 0 and (raw_ok is True or raw_ok is None)
    details = payload.get("details")
    if not isinstance(details, dict):
        details = {} if details is None else {"driver_details": details}

    driver_error_code = payload.get("error_code")
    if protocol_error:
        driver_ok = False
        details["invalid_ok_value"] = raw_ok
        driver_error_code = "driver_incompatible"
    if not driver_ok and not isinstance(driver_error_code, str):
        driver_error_code = _DEFAULT_ERROR_CODE
    if not driver_ok and driver_error_code not in _ERROR_CODES:
        details["driver_error_code"] = driver_error_code
        driver_error_code = _DEFAULT_ERROR_CODE
    message = payload.get("message")
    if protocol_error:
        message = "cua-driver returned a non-boolean ok field."
    if not isinstance(message, str) or not message:
        message = "cua-driver completed successfully." if driver_ok else (
            stderr.strip() or stdout.strip() or "cua-driver operation failed."
        )

    payload.update(
        {
            "ok": driver_ok,
            "available": True,
            "exit_code": return_code,
            "stdout": stdout.strip(),
            "stderr": stderr.strip(),
            "verified": driver_ok and payload.get("verified") is True,
            "error_code": None if driver_ok else driver_error_code,
            "message": message,
            "details": details,
            "target": payload.get("target") if isinstance(payload.get("target"), dict) else {},
            "foreground": (
                payload.get("foreground")
                if isinstance(payload.get("foreground"), dict)
                else ({"current": payload["foreground"]} if payload.get("foreground") is not None else {})
            ),
        }
    )
    return payload


async def _send_cancel(command: str, operation_id: str) -> bool:
    process: Optional[asyncio.subprocess.Process] = None
    try:
        process = await asyncio.create_subprocess_exec(
            command,
            "cancel",
            operation_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(
            process.communicate(), timeout=_CANCEL_TIMEOUT_SECONDS
        )
        parsed = _parse_stdout(stdout.decode("utf-8", errors="replace"))
        return (
            process.returncode == 0
            and isinstance(parsed, dict)
            and parsed.get("ok") is True
            and parsed.get("verified") is True
            and parsed.get("operation_id") == operation_id
            and parsed.get("found") is True
            and parsed.get("cancelled") is True
        )
    except (OSError, asyncio.TimeoutError):
        return False
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()


async def _cancel_and_reap(
    process: asyncio.subprocess.Process,
    command: str,
    operation_id: Optional[str],
    communication: Optional[asyncio.Task[tuple[bytes, bytes]]] = None,
) -> tuple[bool, Optional[tuple[bytes, bytes]]]:
    cancellation_sent = False
    if operation_id:
        cancellation_sent = await _send_cancel(command, operation_id)

    completed_output: Optional[tuple[bytes, bytes]] = None
    if communication is not None:
        try:
            completed_output = await asyncio.wait_for(
                asyncio.shield(communication), timeout=_CANCEL_GRACE_SECONDS
            )
        except asyncio.TimeoutError:
            pass

    if process.returncode is None:
        if communication is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=_CANCEL_GRACE_SECONDS)
            except asyncio.TimeoutError:
                pass
        if process.returncode is None:
            process.kill()
            await process.wait()

    if communication is not None and completed_output is None:
        if not communication.done():
            communication.cancel()
        with suppress(asyncio.CancelledError, OSError):
            completed_output = await communication
    return cancellation_sent, completed_output


def _parse_stdout(stdout: str) -> Any:
    text = stdout.strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"text": text}


def _extract_write_progress(data: Any) -> dict[str, int]:
    """Read progress only from the result and known driver-result wrappers."""
    if not isinstance(data, dict):
        return {}

    progress: dict[str, int] = {}
    sources = [data]
    seen: set[int] = set()
    source_index = 0
    while source_index < len(sources):
        source = sources[source_index]
        source_index += 1
        source_id = id(source)
        if source_id in seen:
            continue
        seen.add(source_id)

        for progress_key, aliases in _WRITE_PROGRESS_ALIASES.items():
            if progress_key in progress:
                continue
            for alias in aliases:
                value = source.get(alias)
                if isinstance(value, bool):
                    continue
                try:
                    count = int(value)
                except (TypeError, ValueError):
                    continue
                if count >= 0:
                    progress[progress_key] = count
                    break

        for container_key in _WRITE_PROGRESS_CONTAINERS:
            nested = source.get(container_key)
            if isinstance(nested, dict) and id(nested) not in seen:
                sources.append(nested)

    return progress


async def _run_driver(
    args: list[str],
    *,
    stdin_json: Optional[dict[str, Any]] = None,
    timeout: int = 60,
    operation_id: Optional[str] = None,
) -> dict[str, Any]:
    command = _driver_path()
    if not command:
        return _unavailable()

    stdin = asyncio.subprocess.PIPE if stdin_json is not None else None
    try:
        process = await asyncio.create_subprocess_exec(
            command,
            *args,
            stdin=stdin,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return _failure(
            "driver_incompatible",
            f"Unable to start cua-driver: {exc}",
            available=False,
            stderr=str(exc),
            command=command,
        )
    payload = None if stdin_json is None else json.dumps(stdin_json).encode("utf-8")
    communication = asyncio.create_task(process.communicate(payload))
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            asyncio.shield(communication), timeout=timeout
        )
    except asyncio.TimeoutError:
        cancellation_sent, completion = await asyncio.shield(
            _cancel_and_reap(process, command, operation_id, communication)
        )
        completion_result: dict[str, Any] = {}
        if completion is not None:
            completion_stdout = completion[0].decode("utf-8", errors="replace")
            completion_stderr = completion[1].decode("utf-8", errors="replace")
            completion_result = _normalise_result(
                _parse_stdout(completion_stdout),
                return_code=process.returncode or 0,
                stdout=completion_stdout,
                stderr=completion_stderr,
            )
        details = {
            "timeout_seconds": timeout,
            "operation_id": operation_id,
            "cancellation_sent": cancellation_sent,
        }
        completion_progress = _extract_write_progress(completion_result)
        if completion_result:
            details["driver_completion"] = {
                key: completion_result.get(key)
                for key in ("error_code", "message")
                if key in completion_result
            }
            details["driver_completion"].update(completion_progress)
        failure = _failure(
            "operation_timeout",
            f"cua-driver timed out after {timeout}s.",
            available=True,
            stderr=f"cua-driver timed out after {timeout}s",
            details=details,
            target=completion_result.get("target"),
            foreground=completion_result.get("foreground"),
            exit_code=None,
            operation_id=operation_id,
        )
        failure.update(completion_progress)
        return failure
    except asyncio.CancelledError:
        await asyncio.shield(
            _cancel_and_reap(process, command, operation_id, communication)
        )
        raise

    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    parsed = _parse_stdout(stdout)
    return _normalise_result(
        parsed,
        return_code=process.returncode or 0,
        stdout=stdout,
        stderr=stderr,
    )


def _find_value(data: Any, names: set[str]) -> Any:
    if isinstance(data, dict):
        for key, value in data.items():
            if key in names and value not in (None, "", [], {}):
                return value
        for value in data.values():
            found = _find_value(value, names)
            if found not in (None, "", [], {}):
                return found
    return None


def _normalise_version(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    match = re.search(r"(?:cua-driver(?:-rs)?\s+|v)?(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)", value)
    return match.group(1) if match else value.strip() or None


def _tool_names(data: Any, *, include_text: bool = True) -> set[str]:
    value = _find_value(data, {"capabilities", "tools"})
    names: set[str] = set()
    if isinstance(value, dict):
        names.update(str(name).strip() for name in value if str(name).strip())
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                names.add(item.split(":", 1)[0].strip())
            elif isinstance(item, dict) and isinstance(item.get("name"), str):
                names.add(item["name"].strip())

    text = data.get("text") if isinstance(data, dict) else None
    if include_text and isinstance(text, str):
        names.update(
            line.split(":", 1)[0].strip()
            for line in text.splitlines()
            if line.strip()
        )
    return {name for name in names if name}


async def _driver_compatibility(timeout: int = 10) -> dict[str, Any]:
    global _compatibility_cache, _compatibility_cache_key

    lock_path = _lock_path()
    try:
        stat = lock_path.stat()
        lock = _read_driver_lock()
    except (OSError, ValueError) as exc:
        command_available = _driver_path() is not None
        return {
            "available": command_available,
            "compatible": False,
            "message": str(exc),
            "actual_version": None,
            "expected_version": None,
            "protocol_version": None,
            "expected_protocol": None,
            "capabilities": [],
            "missing_capabilities": [],
            "status": _failure("driver_incompatible", str(exc), available=command_available),
        }

    command = _driver_path()
    if not command:
        return {
            "available": False,
            "compatible": False,
            "message": "cua-driver is not installed or not on PATH.",
            "actual_version": None,
            "expected_version": _normalise_version(lock["version"]),
            "protocol_version": None,
            "expected_protocol": lock["protocol"],
            "capabilities": [],
            "missing_capabilities": sorted(str(tool) for tool in lock["required_tools"]),
            "status": _unavailable(),
            "published": bool(lock.get("published", False)),
            "lock_path": str(lock_path),
        }

    try:
        command_stat = Path(command).stat()
        command_mtime = command_stat.st_mtime_ns
        command_size = command_stat.st_size
    except OSError:
        command_mtime = 0
        command_size = 0
    status = await _run_driver(["status", "--json"], timeout=timeout)
    version_output: Optional[dict[str, Any]] = None
    actual_version = _normalise_version(
        _find_value(status, {"version", "actual_version", "driver_version", "cua_driver_version"})
    )
    if actual_version is None:
        version_output = await _run_driver(["--version"], timeout=timeout)
        actual_version = _normalise_version(
            _find_value(version_output, {"version", "driver_version", "text"})
            or version_output.get("stdout")
        )

    protocol_version = _find_value(status, {"protocol", "protocol_version", "driver_protocol"})
    if not isinstance(protocol_version, str):
        protocol_version = None

    capabilities = _tool_names(status, include_text=False)
    daemon = status.get("daemon") if isinstance(status.get("daemon"), dict) else {}
    cache_key = (
        command,
        command_mtime,
        command_size,
        stat.st_mtime_ns,
        stat.st_size,
        daemon.get("running"),
        daemon.get("pid"),
        actual_version,
        protocol_version,
        tuple(sorted(capabilities)),
    )
    if _compatibility_cache_key == cache_key and _compatibility_cache is not None:
        cached = dict(_compatibility_cache)
        cached["status"] = status
        return cached

    list_tools: Optional[dict[str, Any]] = None
    if not capabilities:
        list_tools = await _run_driver(["list-tools"], timeout=timeout)
        capabilities = _tool_names(list_tools)
    required_tools = {str(tool) for tool in lock["required_tools"]}
    missing = sorted(required_tools - capabilities)
    expected_version = _normalise_version(lock["version"])
    expected_protocol = lock["protocol"]

    reasons: list[str] = []
    if not status.get("ok"):
        reasons.append("status command failed")
    if status.get("compatible") is not True:
        reasons.append("driver or active daemon did not confirm protocol compatibility")
    if actual_version != expected_version:
        reasons.append(f"version {actual_version!r} does not match {expected_version!r}")
    if protocol_version != expected_protocol:
        reasons.append(f"protocol {protocol_version!r} does not match {expected_protocol!r}")
    if missing:
        reasons.append(f"missing required tools: {', '.join(missing)}")

    result = {
        "available": True,
        "compatible": not reasons,
        "message": "Driver is compatible." if not reasons else "; ".join(reasons),
        "actual_version": actual_version,
        "expected_version": expected_version,
        "protocol_version": protocol_version,
        "expected_protocol": expected_protocol,
        "capabilities": sorted(capabilities),
        "missing_capabilities": missing,
        "status": status,
        "version_output": version_output,
        "list_tools": list_tools,
        "published": bool(lock.get("published", False)),
        "lock_path": str(lock_path),
    }
    _compatibility_cache_key = cache_key
    _compatibility_cache = dict(result)
    return result


async def _call_driver_tool(
    tool: str,
    arguments: Optional[dict[str, Any]],
    timeout: int,
    *,
    require_verified: bool = True,
) -> dict[str, Any]:
    compatibility = await _driver_compatibility(timeout=min(timeout, 10))
    if not compatibility["compatible"]:
        return _failure(
            "driver_incompatible",
            compatibility["message"],
            available=compatibility["available"],
            details={key: value for key, value in compatibility.items() if key != "status"},
        )
    if tool not in compatibility["capabilities"]:
        return _failure(
            "tool_unsupported",
            f"The compatible cua-driver does not expose {tool!r}.",
            available=True,
            details={"tool": tool, "capabilities": compatibility["capabilities"]},
        )

    operation_id = uuid.uuid4().hex
    payload = dict(arguments or {})
    if tool in _TARGETED_PID_TOOLS and _positive_int(payload.get("pid")) is None:
        return _failure(
            "invalid_selector",
            f"{tool} requires a positive target pid.",
            available=True,
            details={"tool": tool, "pid": payload.get("pid")},
        )
    if tool in _DELIVERY_MODE_TOOLS:
        delivery_mode = payload.get("delivery_mode", "background")
        if delivery_mode not in {"background", "foreground"}:
            return _failure(
                "invalid_selector",
                f"{tool} delivery_mode must be background or foreground.",
                available=True,
                details={"tool": tool, "delivery_mode": delivery_mode},
            )
    payload["operation_id"] = operation_id
    result = await _run_driver(
        ["call", tool],
        stdin_json=payload,
        timeout=timeout,
        operation_id=operation_id,
    )
    result.setdefault("operation_id", operation_id)
    if result.get("ok") is True and require_verified and result.get("verified") is not True:
        return _failure(
            "background_no_effect",
            f"cua-driver reported success for {tool!r} without verification.",
            available=True,
            stdout=result.get("stdout", ""),
            stderr=result.get("stderr", ""),
            details={"tool": tool, "driver_result": result, "operation_id": operation_id},
            target=result.get("target"),
            foreground=result.get("foreground"),
            operation_id=operation_id,
        )
    return _validate_tool_result(tool, payload, result)


async def _call_diagnostic_tool(
    tool: str,
    arguments: Optional[dict[str, Any]],
    timeout: int,
) -> dict[str, Any]:
    operation_id = uuid.uuid4().hex
    payload = dict(arguments or {})
    payload["operation_id"] = operation_id
    result = await _run_driver(
        ["call", tool],
        stdin_json=payload,
        timeout=timeout,
        operation_id=operation_id,
    )
    result.setdefault("operation_id", operation_id)
    combined_error = f"{result.get('stderr', '')}\n{result.get('stdout', '')}".lower()
    if not result.get("ok") and "unknown tool" in combined_error:
        result["error_code"] = "tool_unsupported"
    return result


def _positive_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _windows_from_result(data: dict[str, Any]) -> list[dict[str, Any]]:
    windows = _find_value(data, {"windows"})
    if windows is None and isinstance(data.get("result"), list):
        windows = data["result"]
    if not isinstance(windows, list):
        return []
    return [window for window in windows if isinstance(window, dict)]


def _window_identity(window: dict[str, Any]) -> tuple[Optional[int], Optional[int]]:
    pid = next(
        (_positive_int(window.get(key)) for key in ("pid", "process_id", "owner_pid") if key in window),
        None,
    )
    window_id = next(
        (_positive_int(window.get(key)) for key in ("window_id", "hwnd", "id") if key in window),
        None,
    )
    return pid, window_id


def _foreground_preserved(data: dict[str, Any]) -> Optional[bool]:
    for key in ("foreground_unchanged", "foreground_preserved", "foreground_restored"):
        if isinstance(data.get(key), bool):
            return data[key]

    foreground = data.get("foreground")
    if not isinstance(foreground, dict):
        return None
    for key in ("unchanged", "preserved", "restored"):
        if isinstance(foreground.get(key), bool):
            return foreground[key]
    before = next(
        (foreground.get(key) for key in ("before", "previous", "previous_window_id") if key in foreground),
        None,
    )
    after = next(
        (foreground.get(key) for key in ("after", "current", "current_window_id") if key in foreground),
        None,
    )
    if before is not None and after is not None:
        return before == after
    return None


def _result_target_identity(result: dict[str, Any]) -> tuple[Optional[int], Optional[int]]:
    target = result.get("target") if isinstance(result.get("target"), dict) else {}
    pid = _positive_int(
        result.get("target_pid")
        or result.get("pid")
        or target.get("pid")
        or target.get("process_id")
    )
    window_id = _positive_int(
        result.get("window_id")
        or target.get("window_id")
        or target.get("hwnd")
    )
    return pid, window_id


def _validate_tool_result(
    tool: str,
    arguments: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    if result.get("ok") is not True:
        return result

    requested_pid = _positive_int(arguments.get("pid"))
    requested_window_id = _positive_int(arguments.get("window_id"))
    returned_pid, returned_window_id = _result_target_identity(result)

    if tool in _TARGETED_PID_TOOLS:
        if requested_pid is None or returned_pid != requested_pid:
            return _result_failure(
                result,
                "target_mismatch",
                f"{tool} did not verify the requested target pid.",
                details={
                    "requested_pid": requested_pid,
                    "returned_pid": returned_pid,
                    "requested_window_id": requested_window_id,
                    "returned_window_id": returned_window_id,
                },
            )

    if tool in _TARGETED_WINDOW_TOOLS:
        if returned_window_id is None or (
            requested_window_id is not None and returned_window_id != requested_window_id
        ):
            return _result_failure(
                result,
                "target_mismatch",
                f"{tool} did not verify the requested target window.",
                details={
                    "requested_pid": requested_pid,
                    "returned_pid": returned_pid,
                    "requested_window_id": requested_window_id,
                    "returned_window_id": returned_window_id,
                },
            )

    delivery = arguments.get("delivery_mode", arguments.get("dispatch", "background"))
    should_preserve_foreground = (
        tool in _TARGETED_WINDOW_TOOLS
        and tool not in _FOREGROUND_EXEMPT_TOOLS
        and delivery != "foreground"
    )
    if should_preserve_foreground and _foreground_preserved(result) is not True:
        return _result_failure(
            result,
            "foreground_changed",
            f"{tool} did not verify that the foreground window stayed unchanged.",
            details={"delivery_mode": delivery},
        )
    return result


def _result_failure(
    result: dict[str, Any],
    error_code: str,
    message: str,
    *,
    details: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    merged_details = dict(details or {})
    merged_details["driver_result"] = result
    return _failure(
        error_code,
        message,
        available=result.get("available") is not False,
        stdout=str(result.get("stdout", "")),
        stderr=str(result.get("stderr", "")),
        details=merged_details,
        target=result.get("target"),
        foreground=result.get("foreground"),
        operation_id=result.get("operation_id"),
    )


async def _reconcile_launch(
    result: dict[str, Any],
    *,
    prelaunch_windows: list[dict[str, Any]],
    selector_name: str,
    selector_value: str,
    instance_policy: str,
    preserve_foreground: bool,
    timeout: int,
) -> dict[str, Any]:
    if not result.get("ok"):
        return result

    launched = result.get("launched")
    reused = result.get("reused")
    if not isinstance(launched, bool) or not isinstance(reused, bool) or launched == reused:
        return _result_failure(
            result,
            "target_mismatch",
            "launch_app did not unambiguously report whether the target was launched or reused.",
        )
    if instance_policy in {"new", "error"} and not launched:
        return _result_failure(
            result,
            "target_mismatch",
            f"instance_policy={instance_policy!r} cannot return a reused instance.",
        )
    if "launcher_pid" not in result:
        return _result_failure(
            result,
            "target_mismatch",
            "launch_app omitted launcher_pid, so process hand-off cannot be audited.",
        )
    launcher_pid = _positive_int(result.get("launcher_pid"))
    if launched and launcher_pid is None:
        return _result_failure(
            result,
            "target_mismatch",
            "A newly launched target must report a valid launcher_pid.",
        )

    target = result.get("target") if isinstance(result.get("target"), dict) else {}
    driver_selector = target.get("selector")
    driver_selector_value = target.get("value")
    selector_matches = driver_selector == selector_name and isinstance(driver_selector_value, str)
    if selector_name == "name" and selector_matches:
        selector_matches = driver_selector_value.casefold() == selector_value.casefold()
    elif selector_matches:
        selector_matches = driver_selector_value == selector_value
    if not selector_matches:
        return _result_failure(
            result,
            "target_mismatch",
            "launch_app did not preserve the exact requested selector identity.",
            details={
                "requested_selector": {"kind": selector_name, "value": selector_value},
                "driver_selector": {"kind": driver_selector, "value": driver_selector_value},
            },
        )
    target_pid = _positive_int(result.get("target_pid") or target.get("pid"))
    window_id = _positive_int(result.get("window_id") or target.get("window_id") or target.get("hwnd"))
    if target_pid is None or window_id is None:
        return _result_failure(
            result,
            "target_mismatch",
            "launch_app did not return a valid target_pid and window_id.",
        )

    windows_result = await _call_driver_tool(
        "list_windows",
        {"pid": target_pid},
        timeout,
        require_verified=False,
    )
    if not windows_result.get("ok"):
        return _result_failure(
            result,
            "target_mismatch",
            "The launched target could not be reconciled with list_windows.",
            details={"list_windows": windows_result},
        )
    exact_matches = [
        window
        for window in _windows_from_result(windows_result)
        if _window_identity(window) == (target_pid, window_id)
    ]
    if len(exact_matches) != 1:
        error_code = "target_ambiguous" if len(exact_matches) > 1 else "target_mismatch"
        return _result_failure(
            result,
            error_code,
            "The launch result did not resolve to exactly one window owned by target_pid.",
            details={"matches": exact_matches, "list_windows": windows_result},
        )

    prelaunch_ids = {
        identity
        for window in prelaunch_windows
        if (identity := _window_identity(window))[0] is not None and identity[1] is not None
    }
    existed_before = (target_pid, window_id) in prelaunch_ids
    if launched and existed_before:
        return _result_failure(
            result,
            "target_mismatch",
            "launch_app reported a newly launched target that existed before the launch.",
        )
    if reused and not existed_before:
        return _result_failure(
            result,
            "target_mismatch",
            "launch_app reported a reused target that was absent before the launch.",
        )

    foreground_ok = _foreground_preserved(result)
    if preserve_foreground and foreground_ok is not True:
        return _result_failure(
            result,
            "foreground_changed",
            "launch_app could not verify that the foreground window remained unchanged.",
            details={"foreground_preserved": foreground_ok},
        )

    reconciled_target = dict(target)
    reconciled_target.update({"pid": target_pid, "window_id": window_id})
    result.update(
        {
            "launcher_pid": launcher_pid,
            "target_pid": target_pid,
            "window_id": window_id,
            "launched": launched,
            "reused": reused,
            "target": reconciled_target,
        }
    )
    return result


def _png_dimensions(png: bytes) -> Optional[tuple[int, int]]:
    if png[:8] != b"\x89PNG\r\n\x1a\n":
        return None

    offset = 8
    dimensions: Optional[tuple[int, int]] = None
    saw_idat = False
    saw_iend = False
    while offset + 12 <= len(png):
        length = struct.unpack(">I", png[offset : offset + 4])[0]
        chunk_type = png[offset + 4 : offset + 8]
        data_start = offset + 8
        data_end = data_start + length
        crc_end = data_end + 4
        if crc_end > len(png):
            return None
        expected_crc = struct.unpack(">I", png[data_end:crc_end])[0]
        actual_crc = zlib.crc32(chunk_type + png[data_start:data_end]) & 0xFFFFFFFF
        if expected_crc != actual_crc:
            return None

        if offset == 8:
            if chunk_type != b"IHDR" or length != 13:
                return None
            width, height = struct.unpack(">II", png[data_start : data_start + 8])
            if width == 0 or height == 0:
                return None
            dimensions = (width, height)
        elif chunk_type == b"IDAT":
            saw_idat = True
        elif chunk_type == b"IEND":
            if length != 0 or crc_end != len(png):
                return None
            saw_iend = True
            break
        offset = crc_end

    return dimensions if dimensions and saw_idat and saw_iend else None


def _normalise_screenshot(
    result: dict[str, Any],
    *,
    expected_pid: int,
    expected_window_id: int,
) -> dict[str, Any]:
    if not result.get("ok"):
        return result
    image = result.get("screenshot") if isinstance(result.get("screenshot"), dict) else {}
    b64 = result.get("screenshot_png_b64") or result.get("png_base64") or image.get("data")
    mime = result.get("screenshot_mime_type") or result.get("mime_type") or image.get("mime_type")
    width = _positive_int(result.get("width") or result.get("screenshot_width") or image.get("width"))
    height = _positive_int(result.get("height") or result.get("screenshot_height") or image.get("height"))
    if not isinstance(b64, str) or not b64 or mime != "image/png" or width is None or height is None:
        return _result_failure(
            result,
            "background_no_effect",
            "screenshot returned an incomplete PNG payload.",
            details={"mime_type": mime, "width": width, "height": height},
        )
    returned_pid, returned_window_id = _result_target_identity(result)
    if returned_pid != expected_pid or returned_window_id != expected_window_id:
        return _result_failure(
            result,
            "target_mismatch",
            "screenshot identified a different target than requested.",
            details={
                "expected_pid": expected_pid,
                "returned_pid": returned_pid,
                "expected_window_id": expected_window_id,
                "returned_window_id": returned_window_id,
            },
        )

    try:
        png = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        return _result_failure(
            result,
            "background_no_effect",
            "screenshot returned invalid base64 data.",
        )
    png_dimensions = _png_dimensions(png)
    if png_dimensions is None:
        return _result_failure(
            result,
            "background_no_effect",
            "screenshot payload is not a complete PNG with valid chunk CRCs.",
        )
    png_width, png_height = png_dimensions
    if (png_width, png_height) != (width, height):
        return _result_failure(
            result,
            "background_no_effect",
            "screenshot dimensions do not match the PNG IHDR.",
            details={
                "reported_width": width,
                "reported_height": height,
                "png_width": png_width,
                "png_height": png_height,
            },
        )
    if _foreground_preserved(result) is not True:
        return _result_failure(
            result,
            "foreground_changed",
            "screenshot did not verify that the foreground window stayed unchanged.",
        )
    result.update(
        {
            "screenshot_png_b64": b64,
            "screenshot_mime_type": mime,
            "width": width,
            "height": height,
        }
    )
    return result


def _effective_text_timeout(text: str, delay_ms: int, requested_timeout: int) -> int:
    estimated = math.ceil(len(text) * delay_ms / 1000) + _TEXT_TIMEOUT_MARGIN_SECONDS
    return max(requested_timeout, estimated)


def _add_write_diagnostics(result: dict[str, Any], requested_count: int) -> dict[str, Any]:
    if result.get("ok"):
        return result
    details = dict(result.get("details") or {})
    progress = _extract_write_progress(result)
    confirmed = progress.get("confirmed_written")
    dispatched = progress.get("dispatched")

    def bounded_count(value: Any) -> int:
        if isinstance(value, bool):
            return 0
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return 0
        return max(0, min(requested_count, parsed))

    confirmed_count = bounded_count(confirmed)
    dispatched_count = max(confirmed_count, bounded_count(dispatched))
    details.update(
        {
            "requested_characters": requested_count,
            "confirmed_characters_written": confirmed_count,
            "dispatched_characters": dispatched_count,
        }
    )
    result["details"] = details
    result.update(progress)
    result["confirmed_characters_written"] = confirmed_count
    result["dispatched_characters"] = dispatched_count
    return result


def _content_with_optional_image(data: dict[str, Any]) -> Any:
    b64 = data.get("screenshot_png_b64")
    mime = data.get("screenshot_mime_type") or "image/png"
    if not b64:
        return data

    text_data = dict(data)
    text_data["screenshot_png_b64"] = "<returned as MCP image content>"
    return [
        types.TextContent(type="text", text=json.dumps(text_data, ensure_ascii=False, indent=2)),
        types.ImageContent(type="image", data=b64, mimeType=mime),
    ]


@MCP.tool(name="cua_driver_status", description="Check cua-driver availability and daemon status.")
async def cua_driver_status(timeout: int = Field(default=10, ge=1)):
    compatibility = await _driver_compatibility(timeout=timeout)
    status = compatibility["status"]
    compatible = bool(compatibility["compatible"])
    return {
        "ok": compatible,
        "available": bool(compatibility["available"]),
        "stdout": status.get("stdout", ""),
        "stderr": status.get("stderr", ""),
        "verified": compatible,
        "error_code": None if compatible else "driver_incompatible",
        "message": compatibility["message"],
        "details": {
            "lock_path": compatibility.get("lock_path"),
            "published": compatibility.get("published", False),
            "missing_capabilities": compatibility["missing_capabilities"],
        },
        "target": {},
        "foreground": {},
        "command": _driver_path() or _driver_command(),
        "platform": platform.system(),
        "background_default": "driver tools default to background dispatch when the driver supports it",
        "actual_version": compatibility["actual_version"],
        "expected_version": compatibility["expected_version"],
        "protocol_version": compatibility["protocol_version"],
        "expected_protocol": compatibility["expected_protocol"],
        "capabilities": compatibility["capabilities"],
        "missing_capabilities": compatibility["missing_capabilities"],
        "compatible": compatible,
        "status": status,
    }


@MCP.tool(name="cua_driver_doctor", description="Run cua-driver doctor diagnostics.")
async def cua_driver_doctor(
    json_output: bool = Field(default=True),
    timeout: int = Field(default=30, ge=1),
):
    args = ["doctor"]
    if json_output:
        args.append("--json")
    return await _run_driver(args, timeout=timeout)


@MCP.tool(name="cua_driver_check_permissions", description="Check cua-driver accessibility/input permissions.")
async def cua_driver_check_permissions(timeout: int = Field(default=20, ge=1)):
    return await _call_diagnostic_tool("check_permissions", {}, timeout)


@MCP.tool(name="cua_driver_list_tools", description="List tools exposed by the installed cua-driver.")
async def cua_driver_list_tools(timeout: int = Field(default=20, ge=1)):
    result = await _run_driver(["list-tools"], timeout=timeout)
    result["tools"] = sorted(_tool_names(result))
    return result


@MCP.tool(name="cua_driver_describe_tool", description="Describe a cua-driver tool schema.")
async def cua_driver_describe_tool(
    tool: str = Field(description="cua-driver tool name."),
    timeout: int = Field(default=20, ge=1),
):
    return await _run_driver(["describe", tool], timeout=timeout)


@MCP.tool(
    name="cua_driver_call",
    description="Call an audited cua-driver tool; unaudited state-changing tools fail closed.",
)
async def cua_driver_call(
    tool: str = Field(description="cua-driver tool name."),
    arguments: Optional[dict[str, Any]] = Field(default=None, description="JSON arguments for the driver tool."),
    timeout: int = Field(default=60, ge=1),
):
    payload = dict(arguments or {})
    if tool == "launch_app":
        allowed = {
            "name", "path", "bundle_id", "aumid", "launch_path", "urls",
            "additional_arguments", "start_minimized", "instance_policy", "preserve_foreground",
        }
        unexpected = sorted(payload.keys() - allowed)
        if unexpected:
            return _failure(
                "invalid_selector",
                "launch_app received unsupported arguments through cua_driver_call.",
                available=_driver_path() is not None,
                details={"unexpected_arguments": unexpected},
            )
        return await cua_driver_launch_app(
            name=payload.get("name"),
            path=payload.get("path"),
            bundle_id=payload.get("bundle_id"),
            aumid=payload.get("aumid"),
            launch_path=payload.get("launch_path"),
            urls=payload.get("urls"),
            additional_arguments=payload.get("additional_arguments"),
            start_minimized=payload.get("start_minimized", False),
            instance_policy=payload.get("instance_policy", "reuse"),
            preserve_foreground=payload.get("preserve_foreground", True),
            timeout=timeout,
        )
    if tool == "restore_without_activate":
        unexpected = sorted(payload.keys() - {"pid", "window_id"})
        if unexpected or "window_id" not in payload:
            return _failure(
                "invalid_selector",
                "restore_without_activate requires window_id and accepts only pid/window_id.",
                available=_driver_path() is not None,
                details={"unexpected_arguments": unexpected},
            )
        return await cua_driver_restore_without_activate(
            window_id=payload["window_id"],
            pid=payload.get("pid"),
            timeout=timeout,
        )
    if tool == "screenshot":
        unexpected = sorted(payload.keys() - {"pid", "window_id"})
        if unexpected:
            return _failure(
                "invalid_selector",
                "screenshot accepts only pid/window_id through cua_driver_call.",
                available=_driver_path() is not None,
                details={"unexpected_arguments": unexpected},
            )
        return await cua_driver_screenshot(
            pid=payload.get("pid"),
            window_id=payload.get("window_id"),
            timeout=timeout,
        )
    if tool in _SPECIALISED_TOOLS:
        return _failure(
            "invalid_selector",
            f"No safe cua_driver_call route is registered for {tool}.",
            available=_driver_path() is not None,
            details={"tool": tool},
        )
    if tool not in _GENERIC_CALL_TOOLS:
        return _failure(
            "tool_unsupported",
            f"cua_driver_call does not expose unaudited tool {tool!r}.",
            available=_driver_path() is not None,
            details={
                "tool": tool,
                "allowed_tools": sorted(_GENERIC_CALL_TOOLS | _SPECIALISED_TOOLS),
            },
        )

    require_verified = tool in _GENERIC_CALL_STRICT_TOOLS or tool in _TARGETED_WINDOW_TOOLS
    return _content_with_optional_image(
        await _call_driver_tool(
            tool,
            payload,
            timeout,
            require_verified=require_verified,
        )
    )


@MCP.tool(name="cua_driver_list_apps", description="List apps known to cua-driver.")
async def cua_driver_list_apps(timeout: int = Field(default=30, ge=1)):
    return await _call_driver_tool("list_apps", {}, timeout, require_verified=False)


@MCP.tool(name="cua_driver_launch_app", description="Launch an app without stealing focus when supported by cua-driver.")
async def cua_driver_launch_app(
    name: Optional[str] = Field(default=None, description="App display name or executable name."),
    path: Optional[str] = Field(default=None, description="Executable path."),
    bundle_id: Optional[str] = Field(default=None, description="macOS bundle id or Windows AUMID alias."),
    aumid: Optional[str] = Field(default=None, description="Windows packaged app AUMID."),
    launch_path: Optional[str] = Field(default=None, description="launch_path from list_apps."),
    urls: Optional[list[str]] = Field(default=None, description="URLs to open."),
    additional_arguments: Optional[list[str]] = Field(default=None),
    start_minimized: bool = Field(
        default=False,
        description=(
            "Windows: start minimized when true. The default is false so the target is "
            "materialized without stealing focus, which keeps UIA/background dispatch usable."
        ),
    ),
    instance_policy: str = Field(
        default="reuse",
        description="reuse, new, or error when a matching instance already exists.",
    ),
    preserve_foreground: bool = Field(
        default=True,
        description="Fail unless the driver verifies that the foreground window stayed unchanged.",
    ),
    timeout: int = Field(default=60, ge=1),
):
    selectors = {
        key: value.strip()
        for key, value in {
            "name": name,
            "path": path,
            "bundle_id": bundle_id,
            "aumid": aumid,
            "launch_path": launch_path,
        }.items()
        if isinstance(value, str) and value.strip()
    }
    if len(selectors) != 1:
        return _failure(
            "invalid_selector",
            "Exactly one non-empty app selector is required: name, path, bundle_id, aumid, or launch_path.",
            available=_driver_path() is not None,
            details={"provided_selectors": sorted(selectors)},
        )
    if instance_policy not in {"reuse", "new", "error"}:
        return _failure(
            "invalid_selector",
            "instance_policy must be one of: reuse, new, error.",
            available=_driver_path() is not None,
            details={"instance_policy": instance_policy},
        )

    selector_name, selector_value = next(iter(selectors.items()))
    arguments = {
        key: value
        for key, value in {
            selector_name: selector_value,
            "urls": urls,
            "additional_arguments": additional_arguments,
            "start_minimized": start_minimized,
            "instance_policy": instance_policy,
            "preserve_foreground": preserve_foreground,
        }.items()
        if value is not None
    }
    prelaunch_result = await _call_driver_tool(
        "list_windows", {}, timeout, require_verified=False
    )
    if not prelaunch_result.get("ok"):
        return _result_failure(
            prelaunch_result,
            "target_mismatch",
            "The pre-launch window snapshot could not be verified; launch was not attempted.",
        )
    result = await _call_driver_tool("launch_app", arguments, timeout)
    return await _reconcile_launch(
        result,
        prelaunch_windows=_windows_from_result(prelaunch_result),
        selector_name=selector_name,
        selector_value=selector_value,
        instance_policy=instance_policy,
        preserve_foreground=preserve_foreground,
        timeout=timeout,
    )


@MCP.tool(name="cua_driver_restore_without_activate", description="Restore/show a Windows window without stealing foreground focus.")
async def cua_driver_restore_without_activate(
    window_id: int = Field(description="Windows HWND / cua-driver window_id to restore."),
    pid: Optional[int] = Field(
        default=None,
        description="Owning pid. When omitted, it is resolved strictly from list_windows.",
    ),
    timeout: int = Field(default=20, ge=1),
):
    resolved_pid = _positive_int(pid)
    if pid is not None and resolved_pid is None:
        return _failure(
            "target_mismatch",
            "restore_without_activate requires a positive pid when pid is provided.",
            available=_driver_path() is not None,
            details={"pid": pid, "window_id": window_id},
        )

    if resolved_pid is None:
        windows_result = await _call_driver_tool(
            "list_windows",
            {},
            timeout,
            require_verified=False,
        )
        if not windows_result.get("ok"):
            return windows_result
        matches = [
            window
            for window in _windows_from_result(windows_result)
            if _window_identity(window)[1] == window_id
        ]
        if len(matches) != 1:
            error_code = "target_ambiguous" if len(matches) > 1 else "target_mismatch"
            return _result_failure(
                windows_result,
                error_code,
                "The requested window_id did not resolve to exactly one owned window.",
                details={"window_id": window_id, "matches": matches},
            )
        resolved_pid = _window_identity(matches[0])[0]
        if resolved_pid is None:
            return _result_failure(
                windows_result,
                "target_mismatch",
                "The requested window did not report a valid owning pid.",
                details={"window_id": window_id, "match": matches[0]},
            )

    arguments: dict[str, Any] = {"window_id": window_id, "pid": resolved_pid}
    result = await _call_driver_tool("restore_without_activate", arguments, timeout)
    if not result.get("ok"):
        return result
    returned_pid = _positive_int(
        result.get("pid")
        or result.get("target_pid")
        or (result.get("target") or {}).get("pid")
    )
    returned_window_id = _positive_int(
        result.get("window_id")
        or (result.get("target") or {}).get("window_id")
        or (result.get("target") or {}).get("hwnd")
    )
    if returned_pid != resolved_pid or returned_window_id != window_id:
        return _result_failure(
            result,
            "target_mismatch",
            "restore_without_activate verified a different pid/window than requested.",
            details={
                "requested_pid": resolved_pid,
                "requested_window_id": window_id,
                "returned_pid": returned_pid,
                "returned_window_id": returned_window_id,
            },
        )
    if _foreground_preserved(result) is not True:
        return _result_failure(
            result,
            "foreground_changed",
            "restore_without_activate could not verify that foreground stayed unchanged.",
        )
    return result


@MCP.tool(name="cua_driver_kill_app", description="Kill an app by pid using cua-driver.")
async def cua_driver_kill_app(
    pid: int,
    timeout: int = Field(default=30, ge=1),
):
    return await _call_driver_tool("kill_app", {"pid": pid}, timeout)


@MCP.tool(name="cua_driver_list_windows", description="List windows known to cua-driver.")
async def cua_driver_list_windows(
    pid: Optional[int] = Field(default=None),
    timeout: int = Field(default=30, ge=1),
):
    arguments = {} if pid is None else {"pid": pid}
    return await _call_driver_tool(
        "list_windows", arguments, timeout, require_verified=False
    )


@MCP.tool(name="cua_driver_get_window_state", description="Inspect a window tree and screenshot without foregrounding it.")
async def cua_driver_get_window_state(
    pid: int,
    window_id: int,
    capture_mode: str = Field(
        default="ax",
        description="Deprecated compatibility selector: ax or vision. The fork returns tree and screenshot together.",
    ),
    query: Optional[str] = Field(default=None),
    timeout: int = Field(default=60, ge=1),
):
    arguments: dict[str, Any] = {
        "pid": pid,
        "window_id": window_id,
        "capture_mode": capture_mode,
    }
    if query:
        arguments["query"] = query
    return _content_with_optional_image(await _call_driver_tool("get_window_state", arguments, timeout))


@MCP.tool(name="cua_driver_screenshot", description="Take a cua-driver screenshot. Window-scoped when pid/window_id are provided.")
async def cua_driver_screenshot(
    pid: Optional[int] = Field(default=None),
    window_id: Optional[int] = Field(default=None),
    timeout: int = Field(default=60, ge=1),
):
    if _positive_int(pid) is None or _positive_int(window_id) is None:
        return _failure(
            "invalid_selector",
            "screenshot requires an exact positive pid and window_id.",
            available=_driver_path() is not None,
            details={"pid": pid, "window_id": window_id},
        )
    arguments = {"pid": pid, "window_id": window_id}
    result = _normalise_screenshot(
        await _call_driver_tool("screenshot", arguments, timeout),
        expected_pid=pid,
        expected_window_id=window_id,
    )
    return _content_with_optional_image(result)


@MCP.tool(name="cua_driver_click", description="Click by element_index or window-local coordinates using cua-driver.")
async def cua_driver_click(
    pid: int,
    window_id: Optional[int] = Field(default=None),
    element_index: Optional[int] = Field(default=None),
    x: Optional[float] = Field(default=None),
    y: Optional[float] = Field(default=None),
    button: str = Field(default="left"),
    count: int = Field(default=1, ge=1, le=3),
    dispatch: str = Field(default="background", description="background or foreground."),
    from_zoom: bool = Field(default=False),
    timeout: int = Field(default=30, ge=1),
):
    arguments = {
        key: value
        for key, value in {
            "pid": pid,
            "window_id": window_id,
            "element_index": element_index,
            "x": x,
            "y": y,
            "button": button,
            "count": count,
            "delivery_mode": dispatch,
            "from_zoom": from_zoom,
        }.items()
        if value is not None
    }
    return await _call_driver_tool("click", arguments, timeout)


@MCP.tool(name="cua_driver_double_click", description="Double-click by element_index or window-local coordinates.")
async def cua_driver_double_click(
    pid: int,
    window_id: Optional[int] = Field(default=None),
    element_index: Optional[int] = Field(default=None),
    x: Optional[float] = Field(default=None),
    y: Optional[float] = Field(default=None),
    dispatch: str = Field(default="background"),
    from_zoom: bool = Field(default=False),
    timeout: int = Field(default=30, ge=1),
):
    arguments = {
        key: value
        for key, value in {
            "pid": pid,
            "window_id": window_id,
            "element_index": element_index,
            "x": x,
            "y": y,
            "delivery_mode": dispatch,
            "from_zoom": from_zoom,
        }.items()
        if value is not None
    }
    return await _call_driver_tool("double_click", arguments, timeout)


@MCP.tool(name="cua_driver_type_text", description="Type text into a target process using cua-driver.")
async def cua_driver_type_text(
    pid: int,
    text: str,
    window_id: Optional[int] = Field(default=None),
    element_index: Optional[int] = Field(default=None),
    delay_ms: int = Field(default=30, ge=0, le=200),
    dispatch: str = Field(default="background"),
    timeout: int = Field(default=30, ge=1),
):
    arguments = {
        key: value
        for key, value in {
            "pid": pid,
            "text": text,
            "window_id": window_id,
            "element_index": element_index,
            "delay_ms": delay_ms,
            "delivery_mode": dispatch,
        }.items()
        if value is not None
    }
    effective_timeout = _effective_text_timeout(text, delay_ms, timeout)
    result = await _call_driver_tool("type_text", arguments, effective_timeout)
    return _add_write_diagnostics(result, len(text))


@MCP.tool(name="cua_driver_press_key", description="Press a key in a target process using cua-driver.")
async def cua_driver_press_key(
    pid: int,
    key: str,
    window_id: Optional[int] = Field(default=None),
    element_index: Optional[int] = Field(default=None),
    modifiers: Optional[list[str]] = Field(default=None),
    dispatch: str = Field(default="background"),
    timeout: int = Field(default=30, ge=1),
):
    arguments = {
        key_name: value
        for key_name, value in {
            "pid": pid,
            "key": key,
            "window_id": window_id,
            "element_index": element_index,
            "modifiers": modifiers,
            "delivery_mode": dispatch,
        }.items()
        if value is not None
    }
    return await _call_driver_tool("press_key", arguments, timeout)


@MCP.tool(name="cua_driver_hotkey", description="Press a key combination in a target process using cua-driver.")
async def cua_driver_hotkey(
    pid: int,
    keys: list[str],
    window_id: Optional[int] = Field(default=None),
    dispatch: str = Field(default="background"),
    timeout: int = Field(default=30, ge=1),
):
    arguments = {"pid": pid, "keys": keys, "delivery_mode": dispatch}
    if window_id is not None:
        arguments["window_id"] = window_id
    return await _call_driver_tool("hotkey", arguments, timeout)


@MCP.tool(name="cua_driver_set_value", description="Set an accessibility element value using cua-driver.")
async def cua_driver_set_value(
    pid: int,
    window_id: int,
    element_index: int,
    value: str,
    timeout: int = Field(default=30, ge=1),
):
    return await _call_driver_tool(
        "set_value",
        {"pid": pid, "window_id": window_id, "element_index": element_index, "value": value},
        timeout,
    )


@MCP.tool(name="cua_driver_scroll", description="Scroll a target process/window using cua-driver.")
async def cua_driver_scroll(
    pid: int,
    window_id: Optional[int] = Field(default=None),
    element_index: Optional[int] = Field(default=None),
    direction: str = Field(default="down", description="up, down, left, or right."),
    by: str = Field(default="line", description="line or page."),
    amount: int = Field(default=3, ge=1, le=50),
    dispatch: str = Field(default="background"),
    timeout: int = Field(default=30, ge=1),
):
    arguments = {
        key: value
        for key, value in {
            "pid": pid,
            "window_id": window_id,
            "element_index": element_index,
            "direction": direction,
            "by": by,
            "amount": amount,
            "delivery_mode": dispatch,
        }.items()
        if value is not None
    }
    return await _call_driver_tool("scroll", arguments, timeout)


@MCP.tool(name="cua_driver_zoom", description="Zoom a window region and return an image from cua-driver.")
async def cua_driver_zoom(
    pid: int,
    window_id: int,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    timeout: int = Field(default=30, ge=1),
):
    return _content_with_optional_image(
        await _call_driver_tool(
            "zoom",
            {"pid": pid, "window_id": window_id, "x1": x1, "y1": y1, "x2": x2, "y2": y2},
            timeout,
        )
    )


@MCP.tool(name="cua_driver_bring_to_front", description="Explicitly foreground a window when background dispatch is unavailable.")
async def cua_driver_bring_to_front(
    pid: int,
    window_id: Optional[int] = Field(default=None),
    timeout: int = Field(default=20, ge=1),
):
    arguments = {"pid": pid}
    if window_id is not None:
        arguments["window_id"] = window_id
    return await _call_driver_tool("bring_to_front", arguments, timeout)


@MCP.tool(name="cua_driver_set_agent_cursor_enabled", description="Enable or disable cua-driver's visual agent cursor.")
async def cua_driver_set_agent_cursor_enabled(
    enabled: bool = Field(default=True),
    timeout: int = Field(default=20, ge=1),
):
    return await _call_driver_tool("set_agent_cursor_enabled", {"enabled": enabled}, timeout)


@MCP.tool(name="cua_driver_get_agent_cursor_state", description="Read cua-driver's visual agent cursor state.")
async def cua_driver_get_agent_cursor_state(timeout: int = Field(default=20, ge=1)):
    return await _call_driver_tool("get_agent_cursor_state", {}, timeout)
