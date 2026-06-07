from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import asdict, is_dataclass
from typing import Any, Optional

from mcp import types
from pydantic import Field

from mcp_server.tools import MCP
from mcp_server.tools.coordinates import (
    coordinate_space,
    cursor_input_position,
    input_point,
    is_localhost,
    screenshot_point,
)
from mcp_server.tools.cua_sessions import DEFAULT_SESSION_ID, get_cua_manager, _load_cua_sdk


def _normalise_keys(keys: str | list[str]) -> str | list[str]:
    if isinstance(keys, list):
        return keys
    parts = [part for part in re.split(r"[\s+]+", keys.strip()) if part]
    if len(parts) <= 1:
        return parts[0] if parts else keys
    return parts


def _entry_to_dict(entry: Any) -> dict[str, Any]:
    if is_dataclass(entry):
        return asdict(entry)
    if isinstance(entry, dict):
        return entry
    return {
        "name": getattr(entry, "name", ""),
        "path": getattr(entry, "path", ""),
        "is_dir": getattr(entry, "is_dir", False),
        "size": getattr(entry, "size", None),
    }


def _json_content(data: Any) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=json.dumps(data, ensure_ascii=False, indent=2))]


def _environment_name(environment: Any) -> str:
    if isinstance(environment, dict):
        candidates = [
            environment.get("os"),
            environment.get("os_type"),
            environment.get("platform"),
            environment.get("system"),
            environment.get("name"),
        ]
        for value in candidates:
            if value:
                return str(value).lower()
        return json.dumps(environment, ensure_ascii=False).lower()
    return str(environment or "").lower()


def _normalise_url(url: str) -> str:
    value = (url or "").strip()
    if not value:
        raise ValueError("url is required")
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", value):
        value = "https://" + value
    return value


async def _get_instance(session_id: Optional[str]) -> Any:
    if not isinstance(session_id, str):
        session_id = None
    return (await get_cua_manager().get_session(session_id)).instance


@MCP.tool(name="cua_open_session", description="Open a CUA Localhost or Sandbox session.")
async def cua_open_session(
    kind: str = Field(
        default="localhost",
        description="localhost, connect, create, or ephemeral.",
    ),
    session_id: Optional[str] = Field(default=None, description="Stable session id for later tool calls."),
    replace: bool = Field(default=False, description="Replace an existing session with the same id."),
    name: Optional[str] = Field(default=None, description="Sandbox name for create/connect."),
    local: bool = Field(default=True, description="Use local CUA runtime when creating or connecting."),
    os_type: str = Field(default="linux", description="linux, windows, macos, or android."),
    distro: Optional[str] = Field(default=None, description="Linux distro, default ubuntu."),
    version: Optional[str] = Field(default=None, description="OS version, for example 24.04 or 11."),
    image_kind: Optional[str] = Field(default=None, description="vm or container."),
    registry_ref: Optional[str] = Field(default=None, description="CUA registry image reference."),
    image_path: Optional[str] = Field(default=None, description="Local image/disk/ISO path or URL."),
    agent_type: Optional[str] = Field(default=None, description="Optional agent type for Image.from_file."),
    api_key: Optional[str] = Field(default=None, description="CUA cloud API key override."),
    ws_url: Optional[str] = Field(default=None, description="Existing computer-server websocket URL."),
    http_url: Optional[str] = Field(default=None, description="Existing computer-server HTTP URL."),
    container_name: Optional[str] = Field(default=None, description="Container name for remote HTTP auth."),
    cpu: Optional[int] = Field(default=None, description="Cloud/runtime CPU count."),
    memory_mb: Optional[int] = Field(default=None, description="Cloud/runtime memory in MB."),
    disk_gb: Optional[int] = Field(default=None, description="Cloud/runtime disk size in GB."),
    region: str = Field(default="us-east-1", description="CUA cloud region."),
    request_timeout: Optional[float] = Field(default=None, description="Per-request timeout for cloud sessions."),
    time_to_start: Optional[float] = Field(default=None, description="Startup timeout in seconds."),
    telemetry_enabled: bool = Field(default=True, description="Enable CUA SDK telemetry for this session."),
):
    session = await get_cua_manager().open_session(
        kind=kind,
        session_id=session_id,
        replace=replace,
        name=name,
        local=local,
        os_type=os_type,
        distro=distro,
        version=version,
        image_kind=image_kind,
        registry_ref=registry_ref,
        image_path=image_path,
        agent_type=agent_type,
        api_key=api_key,
        ws_url=ws_url,
        http_url=http_url,
        container_name=container_name,
        cpu=cpu,
        memory_mb=memory_mb,
        disk_gb=disk_gb,
        region=region,
        request_timeout=request_timeout,
        time_to_start=time_to_start,
        telemetry_enabled=telemetry_enabled,
    )
    return await session.info()


@MCP.tool(name="cua_list_sessions", description="List active CUA sessions held by this MCP process.")
async def cua_list_sessions():
    return await get_cua_manager().list_sessions()


@MCP.tool(name="cua_close_session", description="Close a CUA session. Persistent sandboxes keep running unless destroy=true.")
async def cua_close_session(
    session_id: str = Field(default=DEFAULT_SESSION_ID),
    destroy: bool = Field(default=False, description="Destroy/delete the backing sandbox when supported."),
):
    return await get_cua_manager().close_session(session_id, destroy=destroy)


@MCP.tool(name="cua_session_info", description="Return environment, dimensions, and lifecycle info for a CUA session.")
async def cua_session_info(session_id: Optional[str] = Field(default=None)):
    return await (await get_cua_manager().get_session(session_id)).info()


@MCP.tool(name="cua_list_sandboxes", description="List CUA sandboxes known to the SDK.")
async def cua_list_sandboxes(
    local: bool = Field(default=True),
    api_key: Optional[str] = Field(default=None),
):
    _, _, Sandbox = _load_cua_sdk()
    sandboxes = await Sandbox.list(local=local, api_key=api_key)
    return [_entry_to_dict(item) for item in sandboxes]


@MCP.tool(name="cua_delete_sandbox", description="Delete a persistent CUA sandbox by name.")
async def cua_delete_sandbox(
    name: str = Field(description="Sandbox name."),
    local: bool = Field(default=True),
    api_key: Optional[str] = Field(default=None),
):
    _, _, Sandbox = _load_cua_sdk()
    await Sandbox.delete(name, local=local, api_key=api_key)
    return {"deleted": True, "name": name, "local": local}


@MCP.tool(name="cua_resume_sandbox", description="Resume a suspended CUA sandbox and attach it as a session.")
async def cua_resume_sandbox(
    name: str = Field(description="Sandbox name."),
    session_id: Optional[str] = Field(default=None),
    local: bool = Field(default=True),
    api_key: Optional[str] = Field(default=None),
):
    _, _, Sandbox = _load_cua_sdk()
    instance = await Sandbox.resume(name, local=local, api_key=api_key)
    manager = get_cua_manager()
    resolved_id = session_id or name
    if resolved_id in manager._sessions:
        await manager.close_session(resolved_id, destroy=False)
    now = __import__("time").time()
    from mcp_server.tools.cua_sessions import CuaSession

    manager._sessions[resolved_id] = CuaSession(
        session_id=resolved_id,
        kind="connect",
        target=name,
        instance=instance,
        persistent=True,
        created_at=now,
        last_used_at=now,
    )
    return await manager._sessions[resolved_id].info()


@MCP.tool(name="cua_suspend_sandbox", description="Suspend a persistent CUA sandbox by name.")
async def cua_suspend_sandbox(
    name: str = Field(description="Sandbox name."),
    local: bool = Field(default=True),
    api_key: Optional[str] = Field(default=None),
):
    _, _, Sandbox = _load_cua_sdk()
    await Sandbox.suspend(name, local=local, api_key=api_key)
    return {"suspended": True, "name": name, "local": local}


@MCP.tool(name="cua_get_environment", description="Get the OS environment for a CUA session.")
async def cua_get_environment(session_id: Optional[str] = Field(default=None)):
    instance = await _get_instance(session_id)
    return {"environment": await instance.get_environment()}


@MCP.tool(name="cua_get_screen_size", description="Get CUA session screen dimensions.")
async def cua_get_screen_size(session_id: Optional[str] = Field(default=None)):
    instance = await _get_instance(session_id)
    width, height = await instance.get_dimensions()
    space = await coordinate_space(instance, screenshot_size=(width, height), refresh=True)
    return {"width": width, "height": height, **space.diagnostics()}


@MCP.tool(name="cua_screenshot", description="Take a screenshot from a CUA session.")
async def cua_screenshot(
    session_id: Optional[str] = Field(default=None),
    format: str = Field(default="png", description="png or jpeg."),
    quality: int = Field(default=95, ge=1, le=95),
):
    instance = await _get_instance(session_id)
    image = await instance.screen.screenshot(format=format, quality=quality)
    width, height = await instance.get_dimensions()
    space = await coordinate_space(instance, screenshot_size=(width, height), refresh=True)
    mime = "image/jpeg" if format.lower() in ("jpeg", "jpg") else "image/png"
    return [
        types.TextContent(
            type="text",
            text=json.dumps({"width": width, "height": height, "format": format, **space.diagnostics()}, ensure_ascii=False),
        ),
        types.ImageContent(type="image", data=__import__("base64").b64encode(image).decode("ascii"), mimeType=mime),
    ]


@MCP.tool(name="cua_get_cursor_position", description="Get the localhost cursor position in screenshot pixels.")
async def cua_get_cursor_position(session_id: Optional[str] = Field(default=None)):
    instance = await _get_instance(session_id)
    if not is_localhost(instance):
        raise RuntimeError("cua_get_cursor_position is only available for Localhost sessions.")

    input_x, input_y = await cursor_input_position()
    x, y = await screenshot_point(instance, input_x, input_y)
    return {"x": x, "y": y}


@MCP.tool(name="cua_move", description="Move the mouse in a CUA session using screenshot-pixel coordinates.")
async def cua_move(
    x: int = Field(description="X coordinate in the latest screenshot pixel space."),
    y: int = Field(description="Y coordinate in the latest screenshot pixel space."),
    session_id: Optional[str] = Field(default=None),
):
    instance = await _get_instance(session_id)
    input_x, input_y = await input_point(instance, x, y)
    await instance.mouse.move(input_x, input_y)
    return {"ok": True}


@MCP.tool(name="cua_click", description="Click in a CUA session using screenshot-pixel coordinates.")
async def cua_click(
    x: int = Field(description="X coordinate in the latest screenshot pixel space."),
    y: int = Field(description="Y coordinate in the latest screenshot pixel space."),
    button: str = Field(default="left", description="left, right, or middle."),
    session_id: Optional[str] = Field(default=None),
):
    instance = await _get_instance(session_id)
    input_x, input_y = await input_point(instance, x, y)
    mouse = instance.mouse
    if button == "right":
        await mouse.right_click(input_x, input_y)
    else:
        await mouse.click(input_x, input_y, button=button)
    return {"ok": True}


@MCP.tool(name="cua_double_click", description="Double-click in a CUA session using screenshot-pixel coordinates.")
async def cua_double_click(
    x: int = Field(description="X coordinate in the latest screenshot pixel space."),
    y: int = Field(description="Y coordinate in the latest screenshot pixel space."),
    session_id: Optional[str] = Field(default=None),
):
    instance = await _get_instance(session_id)
    input_x, input_y = await input_point(instance, x, y)
    await instance.mouse.double_click(input_x, input_y)
    return {"ok": True}


@MCP.tool(name="cua_mouse_down", description="Press a mouse button in a CUA session using screenshot-pixel coordinates.")
async def cua_mouse_down(
    x: int,
    y: int,
    button: str = Field(default="left"),
    session_id: Optional[str] = Field(default=None),
):
    instance = await _get_instance(session_id)
    input_x, input_y = await input_point(instance, x, y)
    await instance.mouse.mouse_down(input_x, input_y, button=button)
    return {"ok": True}


@MCP.tool(name="cua_mouse_up", description="Release a mouse button in a CUA session using screenshot-pixel coordinates.")
async def cua_mouse_up(
    x: int,
    y: int,
    button: str = Field(default="left"),
    session_id: Optional[str] = Field(default=None),
):
    instance = await _get_instance(session_id)
    input_x, input_y = await input_point(instance, x, y)
    await instance.mouse.mouse_up(input_x, input_y, button=button)
    return {"ok": True}


@MCP.tool(name="cua_drag", description="Drag between two screenshot-pixel points in a CUA session.")
async def cua_drag(
    start_x: int,
    start_y: int,
    end_x: int,
    end_y: int,
    button: str = Field(default="left"),
    session_id: Optional[str] = Field(default=None),
):
    instance = await _get_instance(session_id)
    input_start_x, input_start_y = await input_point(instance, start_x, start_y)
    input_end_x, input_end_y = await input_point(instance, end_x, end_y)
    await instance.mouse.drag(input_start_x, input_start_y, input_end_x, input_end_y, button=button)
    return {"ok": True}


@MCP.tool(name="cua_scroll", description="Scroll in a CUA session from a screenshot-pixel coordinate.")
async def cua_scroll(
    x: int,
    y: int,
    scroll_x: int = Field(default=0),
    scroll_y: int = Field(default=3),
    session_id: Optional[str] = Field(default=None),
):
    instance = await _get_instance(session_id)
    input_x, input_y = await input_point(instance, x, y)
    await instance.mouse.scroll(input_x, input_y, scroll_x=scroll_x, scroll_y=scroll_y)
    return {"ok": True}


@MCP.tool(name="cua_type_text", description="Type text in a CUA session.")
async def cua_type_text(
    text: str = Field(description="Text to type."),
    session_id: Optional[str] = Field(default=None),
):
    await (await _get_instance(session_id)).keyboard.type(text)
    return {"ok": True}


@MCP.tool(name="cua_keypress", description="Press a key or key combination in a CUA session.")
async def cua_keypress(
    keys: str | list[str] = Field(description="Key or combo, e.g. enter, ctrl+c, or ['ctrl','c']."),
    session_id: Optional[str] = Field(default=None),
):
    await (await _get_instance(session_id)).keyboard.keypress(_normalise_keys(keys))
    return {"ok": True}


@MCP.tool(name="cua_key_down", description="Press and hold a key in a CUA session.")
async def cua_key_down(
    key: str,
    session_id: Optional[str] = Field(default=None),
):
    await (await _get_instance(session_id)).keyboard.key_down(key)
    return {"ok": True}


@MCP.tool(name="cua_key_up", description="Release a held key in a CUA session.")
async def cua_key_up(
    key: str,
    session_id: Optional[str] = Field(default=None),
):
    await (await _get_instance(session_id)).keyboard.key_up(key)
    return {"ok": True}


@MCP.tool(name="cua_wait", description="Wait without changing the CUA session state.")
async def cua_wait(ms: int = Field(default=1000, ge=0)):
    await asyncio.sleep(ms / 1000)
    return {"ok": True, "waited_ms": ms}


@MCP.tool(name="cua_clipboard_get", description="Read clipboard text from a CUA session.")
async def cua_clipboard_get(session_id: Optional[str] = Field(default=None)):
    return {"text": await (await _get_instance(session_id)).clipboard.get()}


@MCP.tool(name="cua_clipboard_set", description="Set clipboard text in a CUA session.")
async def cua_clipboard_set(
    text: str,
    session_id: Optional[str] = Field(default=None),
):
    await (await _get_instance(session_id)).clipboard.set(text)
    return {"ok": True}


@MCP.tool(name="cua_open_url", description="Open a URL through the desktop UI without running shell commands.")
async def cua_open_url(
    url: str = Field(description="URL or domain to open. Missing schemes default to https://."),
    session_id: Optional[str] = Field(default=None),
    wait_ms: int = Field(default=1000, ge=0, le=30000),
    restore_clipboard: bool = Field(default=True, description="Restore previous clipboard text after launching."),
):
    instance = await _get_instance(session_id)
    normalized_url = _normalise_url(url)
    environment = None
    try:
        environment = await instance.get_environment()
    except Exception:
        pass
    environment_text = _environment_name(environment)

    old_clipboard = None
    clipboard_available = False
    if restore_clipboard:
        try:
            old_clipboard = await instance.clipboard.get()
            clipboard_available = True
        except Exception:
            clipboard_available = False

    keyboard = instance.keyboard
    if "windows" in environment_text or environment_text in ("", "win32"):
        method = "windows_run_dialog"
        paste_keys: str | list[str] = ["ctrl", "v"]
        await keyboard.keypress(["win", "r"])
    elif "darwin" in environment_text or "mac" in environment_text:
        method = "macos_spotlight"
        paste_keys = ["cmd", "v"]
        await keyboard.keypress(["cmd", "space"])
    else:
        method = "desktop_launcher"
        paste_keys = ["ctrl", "v"]
        await keyboard.keypress(["alt", "f2"])

    await asyncio.sleep(0.4)
    await instance.clipboard.set(normalized_url)
    await keyboard.keypress(paste_keys)
    await keyboard.keypress("enter")
    if wait_ms:
        await asyncio.sleep(wait_ms / 1000)

    if restore_clipboard and clipboard_available:
        try:
            await instance.clipboard.set(old_clipboard or "")
        except Exception:
            clipboard_available = False

    return {
        "ok": True,
        "url": normalized_url,
        "method": method,
        "clipboard_restored": bool(restore_clipboard and clipboard_available),
    }


@MCP.tool(name="cua_shell", description="Run a shell command inside a CUA session.")
async def cua_shell(
    command: str,
    timeout: int = Field(default=30, ge=1),
    background: bool = Field(default=False, description="Return immediately with pid when supported."),
    session_id: Optional[str] = Field(default=None),
):
    result = await (await _get_instance(session_id)).shell.run(
        command,
        timeout=timeout,
        background=background,
    )
    return {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "returncode": result.returncode,
        "success": result.success,
    }


@MCP.tool(name="cua_terminal_create", description="Create a PTY terminal session in CUA.")
async def cua_terminal_create(
    command: Optional[str] = Field(default=None),
    cols: int = Field(default=80),
    rows: int = Field(default=24),
    session_id: Optional[str] = Field(default=None),
):
    return await (await _get_instance(session_id)).terminal.create(command=command, cols=cols, rows=rows)


@MCP.tool(name="cua_terminal_send", description="Send input to a CUA PTY terminal session.")
async def cua_terminal_send(
    pid: int,
    data: str,
    session_id: Optional[str] = Field(default=None),
):
    await (await _get_instance(session_id)).terminal.send_input(pid, data)
    return {"ok": True}


@MCP.tool(name="cua_terminal_info", description="Get CUA PTY terminal info.")
async def cua_terminal_info(
    pid: int,
    session_id: Optional[str] = Field(default=None),
):
    return await (await _get_instance(session_id)).terminal.info(pid)


@MCP.tool(name="cua_terminal_close", description="Close a CUA PTY terminal session.")
async def cua_terminal_close(
    pid: int,
    session_id: Optional[str] = Field(default=None),
):
    return {"closed": await (await _get_instance(session_id)).terminal.close(pid)}


@MCP.tool(name="cua_window_title", description="Get the active window title in a CUA session.")
async def cua_window_title(session_id: Optional[str] = Field(default=None)):
    return {"title": await (await _get_instance(session_id)).window.get_active_title()}


@MCP.tool(name="cua_display_url", description="Get a display URL for a sandbox session.")
async def cua_display_url(
    session_id: Optional[str] = Field(default=None),
    share: bool = Field(default=False),
):
    instance = await _get_instance(session_id)
    if not hasattr(instance, "get_display_url"):
        raise RuntimeError("Display URLs are available for sandbox sessions, not Localhost.")
    return {"url": await instance.get_display_url(share=share)}


@MCP.tool(name="cua_snapshot", description="Snapshot a cloud CUA sandbox session.")
async def cua_snapshot(
    session_id: Optional[str] = Field(default=None),
    name: Optional[str] = Field(default=None),
    stateful: bool = Field(default=False),
):
    image = await (await _get_instance(session_id)).snapshot(name=name, stateful=stateful)
    return image.to_dict()


@MCP.tool(name="cua_file_list", description="List files inside a CUA sandbox session.")
async def cua_file_list(
    path: str,
    session_id: Optional[str] = Field(default=None),
):
    files = getattr(await _get_instance(session_id), "files", None)
    if files is None:
        raise RuntimeError("File tools are available for sandbox sessions, not Localhost.")
    return [_entry_to_dict(item) for item in await files.list(path)]


@MCP.tool(name="cua_file_read_text", description="Read a text file inside a CUA sandbox session.")
async def cua_file_read_text(
    path: str,
    session_id: Optional[str] = Field(default=None),
):
    files = getattr(await _get_instance(session_id), "files", None)
    if files is None:
        raise RuntimeError("File tools are available for sandbox sessions, not Localhost.")
    return {"path": path, "content": await files.read_text(path)}


@MCP.tool(name="cua_file_write_text", description="Write a text file inside a CUA sandbox session.")
async def cua_file_write_text(
    path: str,
    content: str,
    session_id: Optional[str] = Field(default=None),
):
    files = getattr(await _get_instance(session_id), "files", None)
    if files is None:
        raise RuntimeError("File tools are available for sandbox sessions, not Localhost.")
    await files.write_text(path, content)
    return {"ok": True, "path": path}


@MCP.tool(name="cua_mobile_tap", description="Tap an Android CUA session.")
async def cua_mobile_tap(
    x: int,
    y: int,
    session_id: Optional[str] = Field(default=None),
):
    await (await _get_instance(session_id)).mobile.tap(x, y)
    return {"ok": True}


@MCP.tool(name="cua_mobile_swipe", description="Swipe an Android CUA session.")
async def cua_mobile_swipe(
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    duration_ms: int = Field(default=300),
    session_id: Optional[str] = Field(default=None),
):
    await (await _get_instance(session_id)).mobile.swipe(x1, y1, x2, y2, duration_ms=duration_ms)
    return {"ok": True}


@MCP.tool(name="cua_mobile_key", description="Press an Android hardware key code.")
async def cua_mobile_key(
    keycode: int,
    session_id: Optional[str] = Field(default=None),
):
    await (await _get_instance(session_id)).mobile.key(keycode)
    return {"ok": True}


@MCP.tool(name="cua_run_task", description="Run a CUA ComputerAgent task against an existing session.")
async def cua_run_task(
    task: str = Field(description="Task instruction for ComputerAgent."),
    model: str = Field(default="anthropic/claude-sonnet-4-5-20250929"),
    session_id: Optional[str] = Field(default=None),
    only_n_most_recent_images: int = Field(default=3, ge=1),
    telemetry_enabled: bool = Field(default=True),
):
    try:
        from cua_agent import ComputerAgent
        from cua_sandbox.agent import LocalhostHandler, SandboxHandler
        from cua_sandbox.localhost import Localhost
    except ImportError as exc:
        raise RuntimeError("cua_run_task requires cua-agent. Install this project with the updated lock file.") from exc

    session = await get_cua_manager().get_session(session_id)
    handler = LocalhostHandler(session.instance) if isinstance(session.instance, Localhost) else SandboxHandler(session.instance)
    agent = ComputerAgent(
        model=model,
        tools=[handler],
        only_n_most_recent_images=only_n_most_recent_images,
        verbosity=logging.INFO,
        telemetry_enabled=telemetry_enabled,
    )

    messages = [{"role": "user", "content": task}]
    texts: list[str] = []
    async for result in agent.run(messages):
        for output in result.get("output", []):
            if output.get("type") == "message":
                content = output.get("content", [])
                if isinstance(content, str):
                    texts.append(content)
                else:
                    for part in content:
                        if isinstance(part, dict) and part.get("text"):
                            texts.append(str(part["text"]))

    screenshot = await session.instance.screen.screenshot()
    return [
        types.TextContent(type="text", text="\n".join(texts).strip() or "Task completed."),
        types.ImageContent(
            type="image",
            data=__import__("base64").b64encode(screenshot).decode("ascii"),
            mimeType="image/png",
        ),
    ]
