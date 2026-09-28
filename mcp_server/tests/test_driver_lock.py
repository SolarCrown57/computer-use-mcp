import json
import os
import subprocess
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
LOCK_PATH = REPOSITORY_ROOT / "cua-driver.lock.json"
E2E_WORKFLOW_PATH = REPOSITORY_ROOT / ".github" / "workflows" / "windows-interactive-e2e.yml"


def test_driver_lock_pins_expected_fork_and_protocol():
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))

    assert lock["schema_version"] == 1
    assert lock["repository"] == "SolarCrown57/cua"
    assert lock["tag"] == "cua-driver-rs-v0.7.1-sc.1"
    assert lock["version"] == "0.7.1-sc.1"
    assert lock["protocol"] == "sc.background.v1"
    assert len(lock["required_tools"]) == len(set(lock["required_tools"]))
    assert {"launch_app", "screenshot", "restore_without_activate", "cancel_operation"} <= set(
        lock["required_tools"]
    )


def test_driver_asset_is_release_bound_and_fail_closed_until_published():
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    asset = lock["assets"]["windows-x86_64"]
    expected_url = (
        f"https://github.com/{lock['repository']}/releases/download/"
        f"{lock['tag']}/{asset['name']}"
    )

    assert asset["url"] == expected_url
    assert asset["name"] == f"cua-driver-rs-{lock['version']}-windows-x86_64.zip"
    if lock["published"]:
        assert len(asset["sha256"]) == 64
        int(asset["sha256"], 16)
    else:
        assert asset["sha256"] is None


def test_published_lock_changes_automatically_gate_interactive_e2e():
    workflow = E2E_WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "push:\n    branches:\n      - main\n    paths:\n      - \"cua-driver.lock.json\"" in workflow
    assert (
        "pull_request:\n    branches:\n      - main\n    paths:\n      - \"cua-driver.lock.json\""
        in workflow
    )
    assert "published: ${{ steps.lock.outputs.published }}" in workflow
    assert "if: needs.lock-state.outputs.published == 'true'" in workflow
    assert "A published driver lock must contain a valid Windows x86_64 SHA256." in workflow


@pytest.mark.skipif(os.name != "nt", reason="PowerShell installer is Windows-only")
def test_installer_refuses_unpublished_release_before_download():
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(REPOSITORY_ROOT / "scripts" / "install-cua-driver.ps1"),
        ],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert completed.returncode != 0
    assert "not published" in (completed.stdout + completed.stderr)
