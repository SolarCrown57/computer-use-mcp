import asyncio
import inspect

from mcp_server.tools import cua_driver


def test_launch_app_defaults_to_materialized_background(monkeypatch):
    captured = {}

    async def fake_call_driver_tool(tool, arguments, timeout):
        captured["tool"] = tool
        captured["arguments"] = arguments
        captured["timeout"] = timeout
        return {"ok": True}

    monkeypatch.setattr(cua_driver, "_call_driver_tool", fake_call_driver_tool)

    start_minimized_default = inspect.signature(cua_driver.cua_driver_launch_app).parameters[
        "start_minimized"
    ].default
    assert start_minimized_default.default is False

    result = asyncio.run(
        cua_driver.cua_driver_launch_app(
            name=None,
            path="C:\\Windows\\System32\\notepad.exe",
            bundle_id=None,
            aumid=None,
            launch_path=None,
            urls=None,
            additional_arguments=None,
            start_minimized=False,
            timeout=60,
        )
    )

    assert result == {"ok": True}
    assert captured["tool"] == "launch_app"
    assert captured["arguments"]["start_minimized"] is False


def test_restore_without_activate_is_windows_only(monkeypatch):
    monkeypatch.setattr(cua_driver.platform, "system", lambda: "Linux")

    result = asyncio.run(cua_driver.cua_driver_restore_without_activate(window_id=123, pid=None, timeout=20))

    assert result["ok"] is False
    assert "only implemented on Windows" in result["message"]
