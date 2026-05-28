from __future__ import annotations

import asyncio
import base64
import json
import re
from typing import Optional

from mcp import types
from pydantic import Field

from mcp_server.common.errors import handle_error
from mcp_server.common.logs import LOG
from mcp_server.tools import MCP
from mcp_server.tools.cua_sessions import get_cua_manager


def _ok():
    return [types.TextContent(type="text", text="Operation successful")]


def _normalise_keys(keys: str) -> str | list[str]:
    parts = [part for part in re.split(r"[\s+]+", (keys or "").strip()) if part]
    if len(parts) <= 1:
        return parts[0] if parts else keys
    return parts


async def _host(session_id: Optional[str] = None):
    if not isinstance(session_id, str):
        session_id = None
    return (await get_cua_manager().get_session(session_id)).instance


@MCP.tool(name="move_mouse", description="Move the mouse pointer to the target position")
async def move_mouse(
    x: int = Field(default=50, description="X coordinate of the mouse pointer to the target position"),
    y: int = Field(default=50, description="Y coordinate of the mouse pointer to the target position"),
    endpoint: Optional[str] = Field(default=None, description="Deprecated. Kept for compatibility."),
):
    del endpoint
    LOG.info(f"Call move_mouse via CUA, x: {x}, y: {y}")
    try:
        await (await _host()).mouse.move(x, y)
        return _ok()
    except Exception as e:
        return handle_error("move_mouse", e)


@MCP.tool(name="click_mouse", description="Click the mouse pointer at the target position")
async def click_mouse(
    x: int = Field(default=50, description="X coordinate of the mouse pointer to the target position"),
    y: int = Field(default=50, description="Y coordinate of the mouse pointer to the target position"),
    button: str = Field(
        default="left",
        description="Mouse button: Left, Right, Middle, or DoubleLeft",
    ),
    endpoint: Optional[str] = Field(default=None, description="Deprecated. Kept for compatibility."),
):
    del endpoint
    LOG.info(f"Call click_mouse via CUA, x: {x}, y: {y}, button: {button}")
    try:
        mouse = (await _host()).mouse
        normalized = (button or "left").lower()
        if normalized in ("doubleleft", "double_left", "double_click"):
            await mouse.double_click(x, y)
        elif normalized == "right":
            await mouse.right_click(x, y)
        else:
            await mouse.click(x, y, "middle" if normalized == "middle" else "left")
        return _ok()
    except Exception as e:
        return handle_error("click_mouse", e)


@MCP.tool(name="drag_mouse", description="Drag the mouse pointer from the start position to the target position")
async def drag_mouse(
    source_x: int = Field(default=50, description="X coordinate of the mouse pointer at the start position"),
    source_y: int = Field(default=50, description="Y coordinate of the mouse pointer at the start position"),
    target_x: int = Field(default=50, description="X coordinate of the mouse pointer at the target position"),
    target_y: int = Field(default=50, description="Y coordinate of the mouse pointer at the target position"),
    endpoint: Optional[str] = Field(default=None, description="Deprecated. Kept for compatibility."),
):
    del endpoint
    LOG.info(
        "Call drag_mouse via CUA, source_x: %s, source_y: %s, target_x: %s, target_y: %s",
        source_x,
        source_y,
        target_x,
        target_y,
    )
    try:
        await (await _host()).mouse.drag(source_x, source_y, target_x, target_y)
        return _ok()
    except Exception as e:
        return handle_error("drag_mouse", e)


@MCP.tool(name="scroll", description="Scroll the mouse wheel")
async def scroll(
    x: int = Field(default=50, description="X coordinate of the mouse pointer"),
    y: int = Field(default=50, description="Y coordinate of the mouse pointer"),
    direction: Optional[str] = Field(default="Down", description="Up, Down, Left, or Right"),
    amount: int = Field(default=3, description="Scroll amount", ge=0, le=50),
    endpoint: Optional[str] = Field(default=None, description="Deprecated. Kept for compatibility."),
):
    del endpoint
    LOG.info(f"Call scroll via CUA, x: {x}, y: {y}, direction: {direction}, amount: {amount}")
    try:
        scroll_x = 0
        scroll_y = 0
        normalized = (direction or "down").lower()
        if normalized == "up":
            scroll_y = amount
        elif normalized == "down":
            scroll_y = -amount
        elif normalized == "left":
            scroll_x = -amount
        elif normalized == "right":
            scroll_x = amount
        else:
            raise ValueError(f"Invalid scroll direction: {direction}")
        await (await _host()).mouse.scroll(x, y, scroll_x=scroll_x, scroll_y=scroll_y)
        return _ok()
    except Exception as e:
        return handle_error("scroll", e)


@MCP.tool(name="press_key", description="Press the specified key")
async def press_key(
    key: str = Field(default="", description="Specified key or key combination"),
    endpoint: Optional[str] = Field(default=None, description="Deprecated. Kept for compatibility."),
):
    del endpoint
    LOG.info(f"Call press_key via CUA, key: {key}")
    try:
        await (await _host()).keyboard.keypress(_normalise_keys(key))
        return _ok()
    except Exception as e:
        return handle_error("press_key", e)


@MCP.tool(name="type_text", description="Type the specified text")
async def type_text(
    text: str = Field(default="", description="Text to type"),
    endpoint: Optional[str] = Field(default=None, description="Deprecated. Kept for compatibility."),
):
    del endpoint
    LOG.info("Call type_text via CUA")
    try:
        await (await _host()).keyboard.type(text)
        return _ok()
    except Exception as e:
        return handle_error("type_text", e)


@MCP.tool(name="get_cursor_position", description="Get the current cursor position")
async def get_cursor_position(
    endpoint: Optional[str] = Field(default=None, description="Deprecated. Kept for compatibility."),
):
    del endpoint
    LOG.info("Call get_cursor_position via cua-auto")
    try:
        import cua_auto.screen as screen

        x, y = await asyncio.to_thread(screen.cursor_position)
        return [types.TextContent(type="text", text=json.dumps({"x": x, "y": y}))]
    except Exception as e:
        return handle_error("get_cursor_position", e)


@MCP.tool(name="screenshot", description="Take a screenshot of the current screen")
async def screenshot(
    endpoint: Optional[str] = Field(default=None, description="Deprecated. Kept for compatibility."),
):
    del endpoint
    LOG.info("Call screenshot via CUA")
    try:
        host = await _host()
        image = await host.screen.screenshot()
        width, height = await host.get_dimensions()
        return [
            types.TextContent(type="text", text=json.dumps({"width": width, "height": height})),
            types.ImageContent(
                type="image",
                data=base64.b64encode(image).decode("ascii"),
                mimeType="image/png",
            ),
        ]
    except Exception as e:
        return handle_error("screenshot", e)
