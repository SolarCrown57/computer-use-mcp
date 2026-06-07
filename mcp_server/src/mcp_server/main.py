# Copyright (c) 2025 Bytedance Ltd. and/or its affiliates
# Licensed under the 【火山方舟】原型应用软件自用许可协议
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at 
#     https://www.volcengine.com/docs/82379/1433703
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
MCP Computer Use Server.

This server provides MCP tools to interact with Computer Use Agent.
"""

import argparse
import ctypes
import os
import signal
import threading
from ctypes import wintypes


def _set_windows_dpi_awareness() -> None:
    if os.name != "nt":
        return

    try:
        user32 = ctypes.windll.user32
        per_monitor_v2 = ctypes.c_void_p(-4)
        if user32.SetProcessDpiAwarenessContext(per_monitor_v2):
            return
    except Exception:
        pass

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass

    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


_set_windows_dpi_awareness()

from mcp_server.common.logs import LOG
from mcp_server.tools import computer
from mcp_server.tools import cua
from mcp_server.tools import cua_driver
from mcp_server.tools import MCP


def _process_exists(pid: int) -> bool:
    if pid <= 1:
        return False
    if os.name == "nt":
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class _WindowsProcessEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(wintypes.ULONG)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def _windows_parent_map() -> dict[int, int]:
    kernel32 = ctypes.windll.kernel32
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        return {}

    parents: dict[int, int] = {}
    entry = _WindowsProcessEntry()
    entry.dwSize = ctypes.sizeof(_WindowsProcessEntry)
    try:
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return parents


def _watch_pids() -> list[int]:
    if os.name != "nt":
        return [os.getppid()]

    parents = _windows_parent_map()
    current = os.getpid()
    watched: list[int] = []
    seen = {current}
    while True:
        parent = parents.get(current)
        if not parent or parent in seen or parent <= 4:
            break
        watched.append(parent)
        seen.add(parent)
        current = parent
    return watched or [os.getppid()]


def _start_parent_watchdog() -> None:
    if os.getenv("MCP_SERVER_DISABLE_PARENT_WATCHDOG"):
        return

    watched = [pid for pid in _watch_pids() if _process_exists(pid)]
    if not watched:
        return

    def watch() -> None:
        while all(_process_exists(pid) for pid in watched):
            threading.Event().wait(2)
        LOG.warning("Parent process chain %s changed; stopping MCP server.", watched)
        os._exit(0)

    thread = threading.Thread(target=watch, name="parent-watchdog", daemon=True)
    thread.start()


def _install_signal_handlers() -> None:
    def handler(signum, _frame):
        LOG.info("Received signal %s; stopping MCP server.", signum)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def main():
    parser = argparse.ArgumentParser(
        description="Run the Computer Use MCP Server")
    parser.add_argument(
        "--transport",
        "-t",
        choices=["sse", "stdio"],
        default="sse",
        help="Transport protocol to use (sse or stdio)",
    )

    args = parser.parse_args()

    # Run the MCP server
    LOG.info(
        f"Starting Computer Use MCP Server with {args.transport} transport")

    _install_signal_handlers()
    if args.transport == "stdio":
        _start_parent_watchdog()

    MCP.run(transport=args.transport)


if __name__ == "__main__":
    main()
