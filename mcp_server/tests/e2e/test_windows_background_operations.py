from __future__ import annotations

import asyncio
import ctypes
import json
import os
import queue
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from ctypes import wintypes

import pytest
from mcp import types

from mcp_server.tools import cua_driver


pytestmark = [
    pytest.mark.windows_e2e,
    pytest.mark.skipif(
        os.name != "nt" or os.environ.get("CUA_E2E_INTERACTIVE") != "1",
        reason="requires the dedicated interactive Windows E2E runner",
    ),
]

USER32 = ctypes.windll.user32 if os.name == "nt" else None
SW_SHOW = 5
WM_CLOSE = 0x0010


def _driver_command() -> str:
    command = os.environ.get("CUA_DRIVER_COMMAND")
    assert command and Path(command).is_file(), "CUA_DRIVER_COMMAND must be an installed driver"
    return command


def _status(command: str) -> dict[str, Any]:
    completed = subprocess.run(
        [command, "status", "--json"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    return json.loads(completed.stdout)


@pytest.fixture(scope="module", autouse=True)
def compatible_daemon() -> None:
    command = _driver_command()
    subprocess.run([command, "stop"], check=False, capture_output=True, timeout=10)
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    daemon = subprocess.Popen(
        [command, "serve"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creation_flags,
    )
    try:
        deadline = time.monotonic() + 15
        status: dict[str, Any] = {}
        while time.monotonic() < deadline:
            try:
                status = _status(command)
            except (AssertionError, json.JSONDecodeError, subprocess.SubprocessError):
                time.sleep(0.1)
                continue
            if status.get("daemon_running") is True:
                break
            time.sleep(0.1)
        assert status.get("compatible") is True, status
        assert status.get("daemon_running") is True, status
        cua_driver._reset_driver_cache()
        yield
    finally:
        subprocess.run([command, "stop"], check=False, capture_output=True, timeout=10)
        try:
            daemon.wait(timeout=5)
        except subprocess.TimeoutExpired:
            daemon.kill()
            daemon.wait(timeout=5)
        cua_driver._reset_driver_cache()


class _TestDesktop:
    def __init__(self) -> None:
        self._commands: queue.Queue[tuple[Callable[[], Any], queue.Queue[Any]]] = queue.Queue()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="cua-e2e-tk", daemon=True)
        self._thread.start()
        assert self._ready.wait(10), "test desktop did not initialize"

    def _run(self) -> None:
        import tkinter as tk

        self.root = tk.Tk()
        self.root.title("CUA E2E Foreground Sentinel")
        self.root.geometry("520x220+40+40")
        self.sentinel = tk.Text(self.root, name="sentinel")
        self.sentinel.pack(fill="both", expand=True)
        self.sentinel.insert("1.0", "sentinel-unchanged")

        self.target = tk.Toplevel(self.root)
        self.target.title("CUA E2E Win32 Target")
        self.target.geometry("520x220+620+40")
        self.editor = tk.Text(self.target, name="targeteditor")
        self.editor.pack(fill="both", expand=True)
        self.root.update_idletasks()
        self.sentinel_hwnd = int(self.root.winfo_id())
        self.target_hwnd = int(self.target.winfo_id())
        self._ready.set()

        def drain() -> None:
            while True:
                try:
                    callback, result = self._commands.get_nowait()
                except queue.Empty:
                    break
                try:
                    result.put((True, callback()))
                except BaseException as exc:  # pragma: no cover - E2E diagnostics
                    result.put((False, exc))
            self.root.after(5, drain)

        self.root.after(0, drain)
        self.root.mainloop()

    def invoke(self, callback: Callable[[], Any]) -> Any:
        result: queue.Queue[Any] = queue.Queue(maxsize=1)
        self._commands.put((callback, result))
        ok, value = result.get(timeout=10)
        if not ok:
            raise value
        return value

    def focus_sentinel(self) -> None:
        def focus() -> None:
            self.root.deiconify()
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.update()
            self.root.attributes("-topmost", False)
            self.sentinel.focus_force()

        self.invoke(focus)
        USER32.ShowWindow(self.sentinel_hwnd, SW_SHOW)
        USER32.SetForegroundWindow(self.sentinel_hwnd)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if int(USER32.GetForegroundWindow()) == self.sentinel_hwnd:
                return
            time.sleep(0.02)
            USER32.SetForegroundWindow(self.sentinel_hwnd)
        raise AssertionError("foreground sentinel could not acquire foreground")

    def sentinel_text(self) -> str:
        return self.invoke(lambda: self.sentinel.get("1.0", "end-1c"))

    def target_text(self) -> str:
        return self.invoke(lambda: self.editor.get("1.0", "end-1c"))

    def clear_target(self) -> None:
        self.invoke(lambda: self.editor.delete("1.0", "end"))

    def minimize_target(self) -> None:
        self.invoke(self.target.iconify)

    def close(self) -> None:
        self.invoke(self.root.quit)
        self._thread.join(timeout=5)


@pytest.fixture(scope="module")
def desktop() -> _TestDesktop:
    value = _TestDesktop()
    try:
        yield value
    finally:
        value.close()


class ForegroundGuard:
    def __init__(self, desktop: _TestDesktop) -> None:
        self.desktop = desktop
        self.changed: list[int] = []
        self._stop = threading.Event()

    @staticmethod
    def _cursor() -> tuple[int, int]:
        point = wintypes.POINT()
        assert USER32.GetCursorPos(ctypes.byref(point))
        return point.x, point.y

    def __enter__(self) -> "ForegroundGuard":
        self.desktop.focus_sentinel()
        self.before_cursor = self._cursor()
        self.before_text = self.desktop.sentinel_text()

        def sample() -> None:
            while not self._stop.is_set():
                current = int(USER32.GetForegroundWindow())
                if current != self.desktop.sentinel_hwnd:
                    self.changed.append(current)
                time.sleep(0.002)

        self._thread = threading.Thread(target=sample, name="cua-focus-guard", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        assert self.changed == [], f"foreground changed transiently: {self.changed[:10]}"
        assert self._cursor() == self.before_cursor, "background operation moved the pointer"
        assert self.desktop.sentinel_text() == self.before_text, "input leaked into the sentinel"


def _window_identities() -> dict[tuple[int, int], dict[str, Any]]:
    result = asyncio.run(cua_driver.cua_driver_list_windows(timeout=15))
    return {
        identity: window
        for window in cua_driver._windows_from_result(result)
        if (identity := cua_driver._window_identity(window))[0] is not None
        and identity[1] is not None
    }


def _close_verified_window(pid: int, window_id: int) -> None:
    """Close only the exact HWND whose current owner still matches pid."""
    hwnd = wintypes.HWND(window_id)
    if not USER32.IsWindow(hwnd):
        return

    owner_pid = wintypes.DWORD()
    thread_id = USER32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
    assert thread_id and owner_pid.value == pid, (
        f"refusing to close HWND {window_id}: expected pid {pid}, "
        f"current owner is {owner_pid.value}"
    )
    assert USER32.PostMessageW(hwnd, WM_CLOSE, 0, 0), (
        f"failed to post WM_CLOSE to verified HWND {window_id}"
    )

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not USER32.IsWindow(hwnd):
            return
        time.sleep(0.05)
    raise AssertionError(f"verified test HWND {window_id} did not close")


def test_window_screenshot_is_target_bound_and_focus_safe(desktop: _TestDesktop) -> None:
    with ForegroundGuard(desktop):
        result = asyncio.run(
            cua_driver.cua_driver_screenshot(
                pid=os.getpid(), window_id=desktop.target_hwnd, timeout=30
            )
        )

    assert isinstance(result, list), result
    assert any(isinstance(item, types.ImageContent) for item in result)


def test_minimized_win32_restore_never_activates(desktop: _TestDesktop) -> None:
    desktop.minimize_target()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not USER32.IsIconic(desktop.target_hwnd):
        time.sleep(0.02)
    assert USER32.IsIconic(desktop.target_hwnd)

    with ForegroundGuard(desktop):
        result = asyncio.run(
            cua_driver.cua_driver_restore_without_activate(
                pid=os.getpid(), window_id=desktop.target_hwnd, timeout=20
            )
        )

    assert result["ok"] is True, result
    assert not USER32.IsIconic(desktop.target_hwnd)
    assert USER32.IsWindowVisible(desktop.target_hwnd)


def test_calculator_packaged_launch_is_rejected_before_activation(desktop: _TestDesktop) -> None:
    with ForegroundGuard(desktop):
        result = asyncio.run(
            cua_driver.cua_driver_launch_app(
                name=None,
                path=None,
                bundle_id=None,
                aumid="Microsoft.WindowsCalculator_8wekyb3d8bbwe!App",
                launch_path=None,
                urls=None,
                additional_arguments=None,
                start_minimized=False,
                instance_policy="reuse",
                preserve_foreground=True,
                timeout=30,
            )
        )

    assert result["ok"] is False, result
    assert result["error_code"] == "background_unavailable", result


def test_modern_notepad_background_keys_refuse_and_new_instance_is_audited(
    desktop: _TestDesktop, tmp_path: Path
) -> None:
    document = tmp_path / f"cua-e2e-notepad-target-{uuid.uuid4().hex}.txt"
    document.write_text("abcdef", encoding="utf-8")
    notepad_path = Path(os.environ["WINDIR"]) / "System32" / "notepad.exe"
    before_launch = _window_identities()
    launcher = subprocess.Popen(
        [str(notepad_path), str(document)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    target: dict[str, Any] | None = None
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        matches = [
            window
            for identity, window in _window_identities().items()
            if identity not in before_launch
            and document.name.casefold() in str(window.get("title", "")).casefold()
        ]
        assert len(matches) <= 1, f"Notepad target is ambiguous: {matches}"
        if matches:
            target = matches[0]
            break
        time.sleep(0.1)
    assert target is not None, "the uniquely titled Notepad window did not appear"
    pid, window_id = cua_driver._window_identity(target)
    assert pid and window_id

    cleanup_windows: list[tuple[int, int]] = [(pid, window_id)]
    try:
        target_title = str(target.get("title", ""))
        identities_before_driver_launch = set(_window_identities())
        with ForegroundGuard(desktop):
            home = asyncio.run(
                cua_driver.cua_driver_press_key(
                    pid=pid,
                    key="home",
                    window_id=window_id,
                    element_index=None,
                    modifiers=None,
                    dispatch="background",
                    timeout=15,
                )
            )
            delete = asyncio.run(
                cua_driver.cua_driver_press_key(
                    pid=pid,
                    key="delete",
                    window_id=window_id,
                    element_index=None,
                    modifiers=None,
                    dispatch="background",
                    timeout=15,
                )
            )
            launch = asyncio.run(
                cua_driver.cua_driver_launch_app(
                    name=None,
                    path=str(notepad_path),
                    bundle_id=None,
                    aumid=None,
                    launch_path=None,
                    urls=None,
                    additional_arguments=None,
                    start_minimized=False,
                    instance_policy="new",
                    preserve_foreground=True,
                    timeout=30,
                )
            )

        assert home["error_code"] == "background_unavailable", home
        assert delete["error_code"] == "background_unavailable", delete
        assert home["ok"] is False and delete["ok"] is False
        current_target = _window_identities().get((pid, window_id))
        assert current_target is not None, "the rejected key calls closed the target"
        assert str(current_target.get("title", "")) == target_title, (
            "the rejected key calls changed the Notepad window state"
        )
        assert document.read_text(encoding="utf-8") == "abcdef"

        if launch["ok"] is True:
            launched_target_pid = int(launch["target_pid"])
            launched_window_id = int(launch["window_id"])
            assert launch["launched"] is True and launch["reused"] is False
            assert (launched_target_pid, launched_window_id) != (pid, window_id)
            assert (launched_target_pid, launched_window_id) not in identities_before_driver_launch
            cleanup_windows.append((launched_target_pid, launched_window_id))
        else:
            assert launch["error_code"] == "background_unavailable", launch
            assert set(_window_identities()) == identities_before_driver_launch, (
                "a rejected launch changed the desktop window set"
            )
    finally:
        for cleanup_pid, cleanup_window_id in reversed(cleanup_windows):
            _close_verified_window(cleanup_pid, cleanup_window_id)
        try:
            launcher.wait(timeout=2)
        except subprocess.TimeoutExpired:
            # Never terminate by PID here: modern Notepad may broker the new
            # document into a pre-existing process that owns user windows.
            pass


def test_mid_request_cancellation_stops_further_text(desktop: _TestDesktop) -> None:
    desktop.clear_target()

    async def exercise() -> None:
        task = asyncio.create_task(
            cua_driver.cua_driver_type_text(
                pid=os.getpid(),
                text="x" * 1000,
                window_id=desktop.target_hwnd,
                element_index=None,
                delay_ms=30,
                dispatch="background",
                timeout=40,
            )
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if await asyncio.to_thread(desktop.target_text):
                break
            if task.done():
                pytest.fail(f"type_text finished before cancellation: {task.result()}")
            await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    with ForegroundGuard(desktop):
        asyncio.run(exercise())
        after_cancel = desktop.target_text()
        time.sleep(0.5)
        assert desktop.target_text() == after_cancel, "text continued after cancellation"
