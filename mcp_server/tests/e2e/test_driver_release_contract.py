import json
import os
import subprocess
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
LOCK = json.loads((REPOSITORY_ROOT / "cua-driver.lock.json").read_text(encoding="utf-8"))

pytestmark = [
    pytest.mark.windows_e2e,
    pytest.mark.skipif(
        os.environ.get("CUA_E2E_INTERACTIVE") != "1",
        reason="requires the dedicated interactive Windows E2E runner",
    ),
]


def _run_driver(*arguments: str) -> str:
    command = os.environ.get("CUA_DRIVER_COMMAND")
    assert command, "CUA_DRIVER_COMMAND must point to the installer-verified driver"
    completed = subprocess.run(
        [command, *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    return completed.stdout


def test_pinned_driver_reports_locked_version_protocol_and_tools():
    assert LOCK["published"] is True
    assert int(os.environ["CUA_E2E_INITIAL_FOREGROUND"]) > 0

    version_output = _run_driver("--version")
    assert LOCK["version"] in version_output

    status = json.loads(_run_driver("status", "--json"))
    assert status["ok"] is True
    assert status["compatible"] is True
    assert status["actual_version"] == LOCK["version"]
    assert status["protocol_version"] == LOCK["protocol"]
    assert set(LOCK["required_tools"]).issubset(status["capabilities"])

    tools_output = _run_driver("list-tools")
    for tool in LOCK["required_tools"]:
        assert tool in tools_output, f"locked driver does not expose required tool: {tool}"
