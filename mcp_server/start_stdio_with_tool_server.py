from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


MCP_SERVER_DIR = Path(__file__).resolve().parent
PROJECT_DIR = MCP_SERVER_DIR.parent
TOOL_SERVER_DIR = PROJECT_DIR / "tool_server"
RUNTIME_DIR = MCP_SERVER_DIR / ".runtime"
TOOL_SERVER_URL = "http://127.0.0.1:8102/config"


def tool_server_ready() -> bool:
    try:
        with urllib.request.urlopen(TOOL_SERVER_URL, timeout=1) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError):
        return False


def ensure_tool_server() -> None:
    if tool_server_ready():
        return

    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    stdout_log = (RUNTIME_DIR / "tool_server.stdout.log").open("ab")
    stderr_log = (RUNTIME_DIR / "tool_server.stderr.log").open("ab")

    process = subprocess.Popen(
        ["uv", "run", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "8102"],
        cwd=TOOL_SERVER_DIR,
        stdout=stdout_log,
        stderr=stderr_log,
    )

    for _ in range(30):
        time.sleep(0.5)
        if tool_server_ready():
            return

    if process.poll() is None:
        process.kill()
    raise RuntimeError(
        f"tool_server did not become ready. Check {RUNTIME_DIR / 'tool_server.stdout.log'} "
        f"and {RUNTIME_DIR / 'tool_server.stderr.log'}."
    )


def main() -> int:
    ensure_tool_server()
    return subprocess.run(
        ["uv", "run", "mcp-server", "-t", "stdio"],
        cwd=MCP_SERVER_DIR,
        check=False,
    ).returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pragma: no cover - startup failures only
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
