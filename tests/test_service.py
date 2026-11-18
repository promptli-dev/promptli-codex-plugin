"""The background service on Linux (systemd --user) and macOS (launchd), with the system tools mocked."""

from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from promptli_gateway.config import GatewayConfig
from promptli_gateway.setup import autostart
from typer.testing import CliRunner

from promptli_codex import cli

runner = CliRunner()
UNIT = "ai.promptli.promptli-codex.service"


@pytest.fixture
def calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    monkeypatch.setenv("PROMPTLI_HOME", str(tmp_path / "promptli"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-never-written")
    monkeypatch.setenv("PROMPTLI_API_KEY", "psk_test_never_written")
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    seen: list[list[str]] = []

    def fake_run(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        seen.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="active\n", stderr="")

    monkeypatch.setattr(autostart.subprocess, "run", fake_run)
    GatewayConfig(runtime="promptli-codex", identity="glow").save(api_key="psk_test_x")
    monkeypatch.delenv("PROMPTLI_API_KEY")
    return seen


def test_linux_installs_a_systemd_user_unit(
    calls: list[list[str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    r = runner.invoke(cli.app, ["start"])
    assert r.exit_code == 0, r.output
    unit = tmp_path / "config" / "systemd" / "user" / UNIT
    text = unit.read_text()
    assert f"ExecStart={sys.executable} -m promptli_codex run" in text
    assert "Restart=on-failure" in text and "WantedBy=default.target" in text
    assert "sk-test" not in text and "psk_" not in text  # keys never land in the unit file
    assert ["systemctl", "--user", "enable", "--now", UNIT] in calls
    assert "systemd --user" in r.output and "enable-linger" in r.output

    st = runner.invoke(cli.app, ["status", "--json"])
    assert '"running": true' in st.output

    assert runner.invoke(cli.app, ["stop"]).output.strip() == "Stopped."
    assert not unit.exists() and ["systemctl", "--user", "disable", "--now", UNIT] in calls


def test_macos_installs_a_launch_agent(
    calls: list[list[str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    r = runner.invoke(cli.app, ["start"])
    assert r.exit_code == 0, r.output
    plist = tmp_path / "home" / "Library" / "LaunchAgents" / "ai.promptli.promptli-codex.plist"
    data = plistlib.loads(plist.read_bytes())
    assert data["ProgramArguments"] == [sys.executable, "-m", "promptli_codex", "run"]
    assert data["KeepAlive"] == {"SuccessfulExit": False}
    assert "OPENAI_API_KEY" not in data["EnvironmentVariables"]
    assert "PROMPTLI_API_KEY" not in data["EnvironmentVariables"]
    assert any(c[:2] == ["launchctl", "bootstrap"] for c in calls)
    assert "launchd" in r.output


def test_service_env_never_includes_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("CODEX_API_KEY", "sk-y")
    monkeypatch.setenv("CODEX_HOME", "/srv/codex-home")
    env = cli.service_env()
    assert env.get("CODEX_HOME") == "/srv/codex-home"
    assert not any("KEY" in k for k in env)


def test_logs_use_journalctl_on_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(cli.shutil, "which", lambda _n: "/usr/bin/journalctl")
    assert cli.log_command(True, 20) == [
        "journalctl",
        "--user",
        "-u",
        UNIT,
        "-n",
        "20",
        "--no-pager",
        "-f",
    ]
    monkeypatch.setattr(sys, "platform", "darwin")
    assert cli.log_command(True, 20) is None
