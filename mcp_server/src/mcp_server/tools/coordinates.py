from __future__ import annotations

import asyncio
import ctypes
import os
import time
from dataclasses import asdict, dataclass
from typing import Any, Optional


_CACHE_TTL_SECONDS = 5.0
_SPACE_CACHE: dict[int, tuple[float, "CoordinateSpace"]] = {}


@dataclass(frozen=True)
class CoordinateSpace:
    coordinate_space: str
    screenshot_width: int
    screenshot_height: int
    input_origin_x: int = 0
    input_origin_y: int = 0
    input_width: int = 0
    input_height: int = 0
    scale_x: float = 1.0
    scale_y: float = 1.0
    transforms_input: bool = False
    platform: str = ""
    dpi_scale: Optional[float] = None
    dpi_awareness: Optional[int] = None

    def to_input(self, x: int, y: int) -> tuple[int, int]:
        if not self.transforms_input:
            input_x = int(x)
            input_y = int(y)
        else:
            input_x = self.input_origin_x + round(int(x) * self.scale_x)
            input_y = self.input_origin_y + round(int(y) * self.scale_y)

        min_x = self.input_origin_x
        min_y = self.input_origin_y
        max_x = self.input_origin_x + max(0, self.input_width - 1)
        max_y = self.input_origin_y + max(0, self.input_height - 1)
        return (
            max(min_x, min(max_x, input_x)),
            max(min_y, min(max_y, input_y)),
        )

    def to_screenshot(self, x: int, y: int) -> tuple[int, int]:
        if not self.transforms_input:
            sx = int(x)
            sy = int(y)
        else:
            sx = round((int(x) - self.input_origin_x) / self.scale_x)
            sy = round((int(y) - self.input_origin_y) / self.scale_y)
        return (
            max(0, min(self.screenshot_width - 1, sx)),
            max(0, min(self.screenshot_height - 1, sy)),
        )

    def diagnostics(self) -> dict[str, Any]:
        return asdict(self)


def _is_localhost(instance: Any) -> bool:
    cls = instance.__class__
    return cls.__name__ == "Localhost" and cls.__module__ == "cua_sandbox.localhost"


def is_localhost(instance: Any) -> bool:
    return _is_localhost(instance)


def _windows_dpi_awareness() -> Optional[int]:
    try:
        awareness = ctypes.c_int()
        result = ctypes.windll.shcore.GetProcessDpiAwareness(0, ctypes.byref(awareness))
        if result == 0:
            return int(awareness.value)
    except Exception:
        return None
    return None


def _windows_dpi_scale() -> Optional[float]:
    try:
        return float(ctypes.windll.shcore.GetScaleFactorForDevice(0)) / 100.0
    except Exception:
        return None


def _windows_virtual_input_bounds() -> Optional[tuple[int, int, int, int]]:
    try:
        user32 = ctypes.windll.user32
        return (
            int(user32.GetSystemMetrics(76)),  # SM_XVIRTUALSCREEN
            int(user32.GetSystemMetrics(77)),  # SM_YVIRTUALSCREEN
            int(user32.GetSystemMetrics(78)),  # SM_CXVIRTUALSCREEN
            int(user32.GetSystemMetrics(79)),  # SM_CYVIRTUALSCREEN
        )
    except Exception:
        return None


def _build_space(
    instance: Any,
    screenshot_width: int,
    screenshot_height: int,
) -> CoordinateSpace:
    platform = os.name
    if not _is_localhost(instance) or os.name != "nt":
        return CoordinateSpace(
            coordinate_space="native",
            screenshot_width=screenshot_width,
            screenshot_height=screenshot_height,
            input_width=screenshot_width,
            input_height=screenshot_height,
            platform=platform,
        )

    bounds = _windows_virtual_input_bounds()
    if not bounds:
        return CoordinateSpace(
            coordinate_space="screenshot_pixels",
            screenshot_width=screenshot_width,
            screenshot_height=screenshot_height,
            input_width=screenshot_width,
            input_height=screenshot_height,
            platform=platform,
            dpi_scale=_windows_dpi_scale(),
            dpi_awareness=_windows_dpi_awareness(),
        )

    origin_x, origin_y, input_width, input_height = bounds
    scale_x = input_width / screenshot_width if screenshot_width else 1.0
    scale_y = input_height / screenshot_height if screenshot_height else 1.0
    transforms_input = abs(scale_x - 1.0) > 0.001 or abs(scale_y - 1.0) > 0.001 or origin_x != 0 or origin_y != 0
    return CoordinateSpace(
        coordinate_space="screenshot_pixels",
        screenshot_width=screenshot_width,
        screenshot_height=screenshot_height,
        input_origin_x=origin_x,
        input_origin_y=origin_y,
        input_width=input_width,
        input_height=input_height,
        scale_x=scale_x,
        scale_y=scale_y,
        transforms_input=transforms_input,
        platform=platform,
        dpi_scale=_windows_dpi_scale(),
        dpi_awareness=_windows_dpi_awareness(),
    )


async def coordinate_space(
    instance: Any,
    *,
    screenshot_size: Optional[tuple[int, int]] = None,
    refresh: bool = False,
) -> CoordinateSpace:
    cache_key = id(instance)
    now = time.monotonic()
    if screenshot_size is None and not refresh:
        cached = _SPACE_CACHE.get(cache_key)
        if cached and now - cached[0] < _CACHE_TTL_SECONDS:
            return cached[1]

    if screenshot_size is None:
        screenshot_size = await instance.get_dimensions()

    space = _build_space(instance, int(screenshot_size[0]), int(screenshot_size[1]))
    _SPACE_CACHE[cache_key] = (now, space)
    return space


async def input_point(instance: Any, x: int, y: int) -> tuple[int, int]:
    space = await coordinate_space(instance)
    return space.to_input(x, y)


async def screenshot_point(instance: Any, x: int, y: int) -> tuple[int, int]:
    space = await coordinate_space(instance)
    return space.to_screenshot(x, y)


async def cursor_input_position() -> tuple[int, int]:
    import cua_auto.screen as screen

    return await asyncio.to_thread(screen.cursor_position)
