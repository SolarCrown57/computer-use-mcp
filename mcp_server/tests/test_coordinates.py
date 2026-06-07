from __future__ import annotations

from mcp_server.tools import coordinates


class Localhost:
    __module__ = "cua_sandbox.localhost"


class Sandbox:
    __module__ = "cua_sandbox.sandbox"


def test_windows_localhost_maps_screenshot_pixels_to_input_pixels(monkeypatch):
    monkeypatch.setattr(coordinates.os, "name", "nt")
    monkeypatch.setattr(coordinates, "_windows_virtual_input_bounds", lambda: (0, 0, 2560, 1440))
    monkeypatch.setattr(coordinates, "_windows_dpi_scale", lambda: 1.25)
    monkeypatch.setattr(coordinates, "_windows_dpi_awareness", lambda: 2)

    space = coordinates._build_space(Localhost(), 2048, 1152)

    assert space.transforms_input is True
    assert space.scale_x == 1.25
    assert space.scale_y == 1.25
    assert space.to_input(1024, 576) == (1280, 720)
    assert space.to_input(2047, 1151) == (2559, 1439)
    assert space.to_input(2048, 1152) == (2559, 1439)
    assert space.to_screenshot(1280, 720) == (1024, 576)


def test_windows_localhost_accounts_for_virtual_screen_origin(monkeypatch):
    monkeypatch.setattr(coordinates.os, "name", "nt")
    monkeypatch.setattr(coordinates, "_windows_virtual_input_bounds", lambda: (-1920, 0, 4480, 1440))
    monkeypatch.setattr(coordinates, "_windows_dpi_scale", lambda: 1.0)
    monkeypatch.setattr(coordinates, "_windows_dpi_awareness", lambda: 2)

    space = coordinates._build_space(Localhost(), 4480, 1440)

    assert space.transforms_input is True
    assert space.to_input(0, 0) == (-1920, 0)
    assert space.to_input(1920, 0) == (0, 0)
    assert space.to_screenshot(0, 0) == (1920, 0)


def test_non_localhost_uses_native_coordinates(monkeypatch):
    monkeypatch.setattr(coordinates.os, "name", "nt")
    monkeypatch.setattr(coordinates, "_windows_virtual_input_bounds", lambda: (0, 0, 2560, 1440))

    space = coordinates._build_space(Sandbox(), 2048, 1152)

    assert space.transforms_input is False
    assert space.to_input(1024, 576) == (1024, 576)
    assert space.to_input(2048, 1152) == (2047, 1151)
    assert space.to_screenshot(1024, 576) == (1024, 576)
    assert space.to_screenshot(2048, 1152) == (2047, 1151)
