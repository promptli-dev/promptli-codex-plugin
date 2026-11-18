from __future__ import annotations

import json
import stat
import time
from pathlib import Path
from typing import Any

import pytest
from promptli import OAuthCredential, OAuthToken
from promptli_gateway.config import GatewayConfig
from promptli_gateway.setup.bootstrap import BootstrapResult
from typer.testing import CliRunner

from promptli_codex import cli

runner = CliRunner()


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROMPTLI_HOME", str(tmp_path))
    monkeypatch.delenv("PROMPTLI_API_KEY", raising=False)


def test_version() -> None:
    r = runner.invoke(cli.app, ["version"])
    assert r.exit_code == 0 and "promptli-codex 0.1.0" in r.output


def test_unconfigured_status_doctor_and_run() -> None:
    r = runner.invoke(cli.app, ["status", "--json"])
    assert r.exit_code == 0 and json.loads(r.output)["configured"] is False
    assert runner.invoke(cli.app, ["doctor"]).exit_code == 1
    assert runner.invoke(cli.app, ["run"]).exit_code == 2


def test_bootstrap_needs_a_key() -> None:
    r = runner.invoke(cli.app, ["bootstrap", "--json"])
    assert r.exit_code == 2 and "PROMPTLI_API_KEY" in r.output


def test_setup_requires_accepting_the_warning() -> None:
    r = runner.invoke(cli.app, ["setup"], input="n\n")
    assert r.exit_code == 1
    assert "Anyone who knows your email address" in r.output


def test_setup_refuses_full_access() -> None:
    r = runner.invoke(cli.app, ["setup", "--yes", "--sandbox", "danger-full-access"])
    assert r.exit_code == 2 and "full access is not allowed" in r.output


def test_status_shows_local_config_even_offline(tmp_path: Path) -> None:
    GatewayConfig(runtime="promptli-codex", identity="glow", base_url="http://127.0.0.1:9").save(
        api_key="psk_test_x"
    )
    r = runner.invoke(cli.app, ["status", "--json"])
    data = json.loads(r.output)
    assert data["identity"] == "glow" and data["sandbox"] == "read-only" and "channels" not in data
    assert (tmp_path / "promptli-codex" / "credentials").stat().st_mode & 0o777 == 0o600


def test_run_rejects_a_bad_sandbox_in_the_config() -> None:
    GatewayConfig(runtime="promptli-codex", identity="glow", extra={"sandbox": "yolo"}).save(
        api_key="psk_test_x"
    )
    r = runner.invoke(cli.app, ["run"])
    assert r.exit_code == 2 and "Bad config" in r.output


def test_uninstall_purge_removes_local_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "uninstall_autostart", lambda _r: False)  # never touch the real service
    GatewayConfig(runtime="promptli-codex", identity="glow").save(api_key="psk_test_x")
    r = runner.invoke(cli.app, ["uninstall", "--purge", "--yes"])
    assert r.exit_code == 0 and not (tmp_path / "promptli-codex").exists()


def _fake_codex(tmp_path: Path, stdout: str, code: int) -> str:
    p = tmp_path / "codex"
    p.write_text(f"#!/bin/sh\necho '{stdout}'\nexit {code}\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


async def test_codex_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "codex_cli", lambda _cfg=None: None)
    ok, detail = await cli.codex_check()
    assert not ok and "not found" in detail
    monkeypatch.setattr(cli, "codex_cli", lambda _cfg=None: _fake_codex(tmp_path, "Not logged in", 1))
    ok, detail = await cli.codex_check()
    assert not ok and "codex login" in detail
    monkeypatch.setattr(
        cli, "codex_cli", lambda _cfg=None: _fake_codex(tmp_path, "Logged in using ChatGPT", 0)
    )
    ok, detail = await cli.codex_check()
    assert ok and "ChatGPT" in detail


def test_codex_cli_prefers_the_configured_binary_then_the_sdk_runtime() -> None:
    cfg = GatewayConfig(runtime="promptli-codex", identity="glow", extra={"codex_bin": "/opt/codex"})
    assert cli.codex_cli(cfg) == "/opt/codex"
    found = cli.codex_cli(None)
    assert found is not None and Path(found).name.startswith("codex")


def _login() -> OAuthCredential:
    return OAuthCredential(
        client_id="mcl_1",
        refresh_token="r1",
        base_url="http://127.0.0.1:9",
        access_token="psk_test_acc",
        expires_at=time.time() + 3000,
        identity="maya",
        env="test",
    )


def test_setup_logs_in_with_the_browser_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def fake_login(url: str, **kw: Any) -> OAuthCredential:
        seen.update(kw, url=url)
        kw["on_url"]("https://comms.promptli.com/oauth/authorize?x=1")
        return _login()

    async def fake_bootstrap(**kw: Any) -> BootstrapResult:
        seen["bootstrap"] = kw
        GatewayConfig(runtime="promptli-codex", identity="maya").save(oauth=kw["credential"])
        return BootstrapResult(status="ok", identity="maya", env="test")

    monkeypatch.setattr(cli, "browser_login", fake_login)
    monkeypatch.setattr(cli, "bootstrap", fake_bootstrap)
    args = ["setup", "--yes", "--no-start", "--identity", "maya", "--number", "--sandbox", "workspace-write"]
    r = runner.invoke(cli.app, args, input="\n")
    assert r.exit_code == 0, r.output
    assert "Log in with your browser (recommended)" in r.output and "oauth/authorize" in r.output
    assert seen["login_hint"] == "maya" and seen["want_number"] is True and seen["open_browser"] is True
    assert seen["client_name"].startswith("Codex on ")
    assert "api_key" not in seen["bootstrap"] and seen["bootstrap"]["credential"].identity == "maya"
    assert seen["bootstrap"]["extra"] == {"sandbox": "workspace-write"}


def test_logout_revokes_the_browser_login_and_deletes_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "uninstall_autostart", lambda _r: False)
    revoked: list[str] = []

    async def fake_revoke(self: OAuthToken) -> None:
        revoked.append(self.cred.refresh_token)

    monkeypatch.setattr(OAuthToken, "revoke", fake_revoke)
    cfg = GatewayConfig(runtime="promptli-codex", identity="maya")
    cfg.save(oauth=_login())
    r = runner.invoke(cli.app, ["logout", "--yes"])
    assert r.exit_code == 0 and "revoked the browser login" in r.output
    assert revoked == ["r1"] and not cfg.credentials_path.exists()
    assert "Not logged in" in runner.invoke(cli.app, ["logout", "--yes"]).output


def test_bootstrap_reuses_a_stored_browser_login(monkeypatch: pytest.MonkeyPatch) -> None:
    GatewayConfig(runtime="promptli-codex", identity="maya").save(oauth=_login())
    seen: dict[str, Any] = {}

    async def fake_bootstrap(**kw: Any) -> BootstrapResult:
        seen.update(kw)
        return BootstrapResult(status="ok", identity="maya")

    monkeypatch.setattr(cli, "bootstrap", fake_bootstrap)
    r = runner.invoke(cli.app, ["bootstrap", "--json"])
    assert r.exit_code == 0, r.output
    assert seen["credential"].refresh_token == "r1" and "api_key" not in seen
