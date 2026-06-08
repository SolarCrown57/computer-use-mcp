from __future__ import annotations

import asyncio
import ctypes
import json
import os
import platform
import shutil
import time
from ctypes import wintypes
from typing import Any, Optional

from mcp import types
from pydantic import Field

from mcp_server.common.config import cua_driver_config
from mcp_server.tools import MCP


def _driver_command() -> str:
    return os.getenv("CUA_DRIVER_COMMAND") or cua_driver_config.get("command", "cua-driver")


def _driver_path() -> Optional[str]:
    command = _driver_command()
    if os.path.isabs(command) or os.sep in command:
        return command if os.path.exists(command) else None
    return shutil.which(command)


def _install_hint() -> str:
    if platform.system() == "Windows":
        return "irm https://raw.githubusercontent.com/trycua/cua/main/libs/cua-driver/scripts/install.ps1 | iex"
    return '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/trycua/cua/main/libs/cua-driver/scripts/install.sh)"'


def _unavailable() -> dict[str, Any]:
    return {
        "ok": False,
        "available": False,
        "command": _driver_command(),
        "message": "cua-driver is not installed or not on PATH.",
        "install": _install_hint(),
    }


def _parse_stdout(stdout: str) -> Any:
    text = stdout.strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"text": text}


async def _run_driver(
    args: list[str],
    *,
    stdin_json: Optional[dict[str, Any]] = None,
    timeout: int = 60,
) -> dict[str, Any]:
    command = _driver_path()
    if not command:
        return _unavailable()

    stdin = asyncio.subprocess.PIPE if stdin_json is not None else None
    process = await asyncio.create_subprocess_exec(
        command,
        *args,
        stdin=stdin,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    payload = None if stdin_json is None else json.dumps(stdin_json).encode("utf-8")
    try:
        stdout_b, stderr_b = await asyncio.wait_for(process.communicate(payload), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return {
            "ok": False,
            "available": True,
            "exit_code": None,
            "stdout": "",
            "stderr": f"cua-driver timed out after {timeout}s",
        }

    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    parsed = _parse_stdout(stdout)
    if process.returncode == 0:
        if isinstance(parsed, dict):
            return {"ok": True, "available": True, **parsed, "stderr": stderr.strip()}
        return {"ok": True, "available": True, "result": parsed, "stderr": stderr.strip()}
    return {
        "ok": False,
        "available": True,
        "exit_code": process.returncode,
        "stdout": stdout.strip(),
        "stderr": stderr.strip(),
    }


async def _call_driver_tool(tool: str, arguments: Optional[dict[str, Any]], timeout: int) -> dict[str, Any]:
    return await _run_driver(["call", tool], stdin_json=arguments or {}, timeout=timeout)


def _set_foreground_window(user32: Any, window_id: int) -> dict[str, Any]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.SetActiveWindow.argtypes = [wintypes.HWND]
    user32.SetActiveWindow.restype = wintypes.HWND
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    hwnd = wintypes.HWND(window_id)
    foreground = user32.GetForegroundWindow()
    foreground_id = int(foreground or 0)
    current_thread_id = kernel32.GetCurrentThreadId()
    foreground_thread_id = (
        user32.GetWindowThreadProcessId(wintypes.HWND(foreground_id), None) if foreground_id else 0
    )
    target_thread_id = user32.GetWindowThreadProcessId(hwnd, None)
    attached_thread_ids: list[int] = []

    for thread_id in {foreground_thread_id, target_thread_id}:
        if thread_id and thread_id != current_thread_id:
            if user32.AttachThreadInput(current_thread_id, thread_id, True):
                attached_thread_ids.append(thread_id)

    try:
        set_foreground = bool(user32.SetForegroundWindow(hwnd))
        bring_to_top = bool(user32.BringWindowToTop(hwnd))
        active_window = user32.SetActiveWindow(hwnd)
    finally:
        for thread_id in attached_thread_ids:
            user32.AttachThreadInput(current_thread_id, thread_id, False)

    return {
        "set_foreground": set_foreground,
        "bring_to_top": bring_to_top,
        "active_window": int(active_window or 0),
        "attached_thread_count": len(attached_thread_ids),
    }


def _restore_window_without_activate(window_id: int) -> dict[str, Any]:
    if platform.system() != "Windows":
        return {
            "ok": False,
            "available": True,
            "message": "restore_without_activate is only implemented on Windows.",
        }

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    hwnd = wintypes.HWND(window_id)
    if not user32.IsWindow(hwnd):
        return {
            "ok": False,
            "available": True,
            "window_id": window_id,
            "message": "window_id is not a valid HWND.",
        }

    sw_shownoactivate = 4
    sw_restore = 9
    swp_nosize = 0x0001
    swp_nomove = 0x0002
    swp_nozorder = 0x0004
    swp_noactivate = 0x0010
    swp_showwindow = 0x0040

    was_iconic = bool(user32.IsIconic(hwnd))
    was_visible = bool(user32.IsWindowVisible(hwnd))
    previous_foreground = user32.GetForegroundWindow()
    previous_foreground_id = int(previous_foreground or 0)
    hwnd_id = int(hwnd.value or 0)
    used_restore_fallback = False
    foreground_restore: dict[str, Any] = {}

    user32.ShowWindowAsync(hwnd, sw_shownoactivate)
    user32.SetWindowPos(
        hwnd,
        None,
        0,
        0,
        0,
        0,
        swp_nomove | swp_nosize | swp_nozorder | swp_noactivate | swp_showwindow,
    )

    if user32.IsIconic(hwnd):
        used_restore_fallback = True
        for _ in range(2):
            user32.ShowWindow(hwnd, sw_restore)
            time.sleep(0.2)
            if not user32.IsIconic(hwnd):
                break
        if previous_foreground_id and previous_foreground_id != hwnd_id:
            foreground_restore = _set_foreground_window(user32, previous_foreground_id)
        user32.SetWindowPos(
            hwnd,
            None,
            0,
            0,
            0,
            0,
            swp_nomove | swp_nosize | swp_nozorder | swp_noactivate | swp_showwindow,
        )

    is_iconic = bool(user32.IsIconic(hwnd))
    is_visible = bool(user32.IsWindowVisible(hwnd))
    foreground = user32.GetForegroundWindow()
    foreground_id = int(foreground or 0)
    return {
        "ok": is_visible and not is_iconic,
        "available": True,
        "window_id": window_id,
        "was_minimized": was_iconic,
        "was_visible": was_visible,
        "is_minimized": is_iconic,
        "is_visible": is_visible,
        "used_restore_fallback": used_restore_fallback,
        "foreground_restore": foreground_restore,
        "previous_foreground": previous_foreground_id,
        "foreground": foreground_id,
        "foreground_restored": not previous_foreground_id or foreground_id == previous_foreground_id,
        "activated": foreground_id == hwnd_id,
    }


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
    path = _driver_path()
    if not path:
        return _unavailable()
    status = await _run_driver(["status"], timeout=timeout)
    return {
        "ok": status.get("ok", False),
        "available": True,
        "command": path,
        "platform": platform.system(),
        "background_default": "driver tools default to background dispatch when the driver supports it",
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
    return await _call_driver_tool("check_permissions", {}, timeout)


@MCP.tool(name="cua_driver_list_tools", description="List tools exposed by the installed cua-driver.")
async def cua_driver_list_tools(timeout: int = Field(default=20, ge=1)):
    result = await _run_driver(["list-tools"], timeout=timeout)
    text = result.get("text")
    if text:
        result["tools"] = [line.strip() for line in text.splitlines() if line.strip()]
    return result


@MCP.tool(name="cua_driver_describe_tool", description="Describe a cua-driver tool schema.")
async def cua_driver_describe_tool(
    tool: str = Field(description="cua-driver tool name."),
    timeout: int = Field(default=20, ge=1),
):
    return await _run_driver(["describe", tool], timeout=timeout)


@MCP.tool(name="cua_driver_call", description="Call any cua-driver tool. Use this for newly added driver capabilities.")
async def cua_driver_call(
    tool: str = Field(description="cua-driver tool name."),
    arguments: Optional[dict[str, Any]] = Field(default=None, description="JSON arguments for the driver tool."),
    timeout: int = Field(default=60, ge=1),
):
    return _content_with_optional_image(await _call_driver_tool(tool, arguments, timeout))


@MCP.tool(name="cua_driver_list_apps", description="List apps known to cua-driver.")
async def cua_driver_list_apps(timeout: int = Field(default=30, ge=1)):
    return await _call_driver_tool("list_apps", {}, timeout)


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
    timeout: int = Field(default=60, ge=1),
):
    arguments = {
        key: value
        for key, value in {
            "name": name,
            "path": path,
            "bundle_id": bundle_id,
            "aumid": aumid,
            "launch_path": launch_path,
            "urls": urls,
            "additional_arguments": additional_arguments,
            "start_minimized": start_minimized,
        }.items()
        if value is not None
    }
    return await _call_driver_tool("launch_app", arguments, timeout)


@MCP.tool(name="cua_driver_restore_without_activate", description="Restore/show a Windows window without stealing foreground focus.")
async def cua_driver_restore_without_activate(
    window_id: int = Field(description="Windows HWND / cua-driver window_id to restore."),
    pid: Optional[int] = Field(default=None, description="Optional pid used only to include refreshed window diagnostics."),
    timeout: int = Field(default=20, ge=1),
):
    result = _restore_window_without_activate(window_id)
    if pid is not None:
        result["windows"] = await _call_driver_tool("list_windows", {"pid": pid}, timeout)
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
    return await _call_driver_tool("list_windows", arguments, timeout)


@MCP.tool(name="cua_driver_get_window_state", description="Inspect a window tree and screenshot without foregrounding it.")
async def cua_driver_get_window_state(
    pid: int,
    window_id: int,
    capture_mode: str = Field(default="som", description="som, vision, or ax."),
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
    arguments = {key: value for key, value in {"pid": pid, "window_id": window_id}.items() if value is not None}
    return _content_with_optional_image(await _call_driver_tool("screenshot", arguments, timeout))


@MCP.tool(name="cua_driver_click", description="Click by element_index or window-local coordinates using cua-driver.")
async def cua_driver_click(
    pid: int,
    window_id: Optional[int] = Field(default=None),
    element_index: Optional[int] = Field(default=None),
    x: Optional[float] = Field(default=None),
    y: Optional[float] = Field(default=None),
    button: str = Field(default="left"),
    count: int = Field(default=1, ge=1, le=3),
    dispatch: str = Field(default="background", description="background, foreground, or auto."),
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
            "dispatch": dispatch,
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
            "dispatch": dispatch,
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
            "dispatch": dispatch,
        }.items()
        if value is not None
    }
    return await _call_driver_tool("type_text", arguments, timeout)


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
            "dispatch": dispatch,
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
    arguments = {"pid": pid, "keys": keys, "dispatch": dispatch}
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
            "dispatch": dispatch,
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
