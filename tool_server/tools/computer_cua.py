import asyncio
import base64
import re

from .base import BaseResult
from .computer import (
    ChangePasswordRequest,
    ClickMouseRequest,
    DragMouseRequest,
    GetCursorPositionRequest,
    GetScreenSizeRequest,
    IComputerTool,
    MoveMouseRequest,
    PressKeyRequest,
    PressMouseRequest,
    ReleaseMouseRequest,
    ScrollRequest,
    TakeScreenshotRequest,
    TypeTextRequest,
    WaitRequest,
)
from .password import change_password


class CuaLocalhostComputerTool(IComputerTool):
    """Computer tool backed by the current trycua/cua Localhost API."""

    def __init__(self):
        self._host = None

    async def _ensure_host(self):
        if self._host is None:
            try:
                from cua import Localhost
            except ImportError:
                from cua_sandbox import Localhost

            self._host = await Localhost.connect()
        return self._host

    @staticmethod
    def _normalise_button(button: str | None) -> str:
        button = (button or "left").lower()
        if button in ("double_click", "double_left"):
            return "double_left"
        if button in ("right", "middle"):
            return button
        return "left"

    @staticmethod
    def _normalise_keys(keys: str) -> str | list[str]:
        parts = [part for part in re.split(r"[\s+]+", keys.strip()) if part]
        if len(parts) <= 1:
            return parts[0] if parts else keys
        return parts

    async def move_mouse(self, request: MoveMouseRequest):
        host = await self._ensure_host()
        await host.mouse.move(request.x, request.y)
        return BaseResult(output="", error="")

    async def click_mouse(self, request: ClickMouseRequest):
        host = await self._ensure_host()
        button = self._normalise_button(request.button)
        if request.press and not request.release:
            await host.mouse.mouse_down(
                request.x,
                request.y,
                "left" if button == "double_left" else button,
            )
        elif request.release and not request.press:
            await host.mouse.mouse_up(
                request.x,
                request.y,
                "left" if button == "double_left" else button,
            )
        elif button == "double_left":
            await host.mouse.double_click(request.x, request.y)
        else:
            await host.mouse.click(request.x, request.y, button)
        return BaseResult(output="", error="")

    async def press_mouse(self, request: PressMouseRequest):
        host = await self._ensure_host()
        await host.mouse.mouse_down(request.x, request.y, request.button)
        return BaseResult(output="", error="")

    async def release_mouse(self, request: ReleaseMouseRequest):
        host = await self._ensure_host()
        await host.mouse.mouse_up(request.x, request.y, request.button)
        return BaseResult(output="", error="")

    async def drag_mouse(self, request: DragMouseRequest):
        host = await self._ensure_host()
        await host.mouse.drag(
            request.source_x,
            request.source_y,
            request.target_x,
            request.target_y,
        )
        return BaseResult(output="", error="")

    async def scroll(self, request: ScrollRequest):
        host = await self._ensure_host()
        amount = int(request.scroll_amount)
        direction = request.scroll_direction
        scroll_x = 0
        scroll_y = 0
        if direction == "up":
            scroll_y = amount
        elif direction == "down":
            scroll_y = -amount
        elif direction == "left":
            scroll_x = -amount
        elif direction == "right":
            scroll_x = amount
        else:
            raise ValueError(f"Invalid scroll direction: {direction}")
        await host.mouse.scroll(request.x, request.y, scroll_x=scroll_x, scroll_y=scroll_y)
        return BaseResult(output="", error="")

    async def press_key(self, request: PressKeyRequest):
        host = await self._ensure_host()
        await host.keyboard.keypress(self._normalise_keys(request.key))
        return BaseResult(output="", error="")

    async def type_text(self, request: TypeTextRequest):
        host = await self._ensure_host()
        await host.keyboard.type(request.text)
        return BaseResult(output="", error="")

    async def wait(self, request: WaitRequest):
        await asyncio.sleep(int(request.duration) / 1000)
        return BaseResult(output="", error="")

    async def take_screenshot(self, request: TakeScreenshotRequest):
        host = await self._ensure_host()
        image = await host.screenshot()
        return {"Screenshot": base64.b64encode(image).decode("ascii")}

    async def get_cursor_position(self, request: GetCursorPositionRequest):
        import cua_auto.screen as screen

        x, y = await asyncio.to_thread(screen.cursor_position)
        return {"PositionX": x, "PositionY": y}

    async def get_screen_size(self, request: GetScreenSizeRequest):
        host = await self._ensure_host()
        width, height = await host.screen.size()
        return {"Width": width, "Height": height}

    async def change_password(self, req: ChangePasswordRequest):
        return await change_password(req.username, req.new_password)
