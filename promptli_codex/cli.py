"""`promptli-codex`: set up and run OpenAI Codex as a Promptli Comms agent."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import typer
from promptli import AsyncPromptli, OAuthCredential, OAuthToken, PromptliError, browser_login
from promptli_gateway.config import DEFAULT_BASE_URL, GatewayConfig, home_dir
from promptli_gateway.core import Gateway
from promptli_gateway.setup.autostart import (
    autostart_status,
    install_autostart,
    label,
    uninstall_autostart,
)
from promptli_gateway.setup.bootstrap import BootstrapResult, bootstrap
from promptli_gateway.setup.doctor import doctor as run_doctor

from promptli_codex import __version__
from promptli_codex.adapter import RUNTIME, CodexAdapter
from promptli_codex.permissions import SANDBOXES, sandbox_for
from promptli_codex.prompts import STRANGER_WARNING

app = typer.Typer(
    help="Give OpenAI Codex its own email address and phone number (Promptli Comms).",
    no_args_is_help=True,
    add_completion=False,
)

BASE_URL_ENV = "PROMPTLI_BASE_URL"
HOSTED_MCP_URL = "https://api.comms.promptli.com/mcp"
# Environment the background service may inherit. Never API keys: Codex reads its own login from
# ~/.codex (CODEX_HOME), and Promptli credentials live in ~/.promptli/promptli-codex/credentials.
SERVICE_ENV = ("PROMPTLI_HOME", "PROMPTLI_BASE_URL", "CODEX_HOME", "HOME")


def _base_url(opt: str | None) -> str:
    return opt or os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL


def _print_json(data: Any) -> None:
    typer.echo(json.dumps(data, indent=2, default=str))


def _show_bootstrap(res: BootstrapResult, as_json: bool) -> None:
    if as_json:
        _print_json(res.as_dict())
    elif res.status == "error" and res.error:
        typer.secho(f"Setup failed: {res.error.get('code')}: {res.error.get('message')}", fg="red")
    else:
        typer.secho(f"Agent @{res.identity} is ready ({res.env} mode).", fg="green")
        if res.email_address:
            typer.echo(f"  email  {res.email_address}")
        typer.echo(f"  phone  {res.phone_number or 'none (run setup --number to add one)'}")
        for a in res.actions:
            typer.echo(f"  - {a}")
        if res.next_step:
            typer.secho(f"Next: {res.next_step}", fg="yellow")
    if res.status == "error":
        raise typer.Exit(1)


def codex_cli(cfg: GatewayConfig | None = None) -> str | None:
    """The Codex binary: config `extra.codex_bin`, else the SDK's pinned runtime, else PATH."""
    if cfg is not None and cfg.extra.get("codex_bin"):
        return str(cfg.extra["codex_bin"])
    with contextlib.suppress(Exception):
        from openai_codex import CodexConfig
        from openai_codex.client import _resolve_codex_bin  # pyright: ignore[reportPrivateUsage]

        return str(_resolve_codex_bin(CodexConfig()))
    return shutil.which("codex")


async def codex_check() -> tuple[bool, str]:
    cli = codex_cli(GatewayConfig.load(RUNTIME))
    if cli is None:
        return False, "Codex not found (reinstall promptli-codex, or set extra.codex_bin in the config)"
    try:
        out = await asyncio.to_thread(
            subprocess.run,
            [cli, "login", "status"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"could not run Codex: {e}"
    text = " ".join((out.stdout + " " + out.stderr).split())
    if out.returncode != 0 or "not logged in" in text.lower():
        return False, "Codex is not logged in: run `codex login` (ChatGPT sign-in or API key)"
    detail = next((ln for ln in out.stdout.splitlines() if ln.strip()), "logged in")
    return True, f"Codex {detail.strip()}"


# ---- setup ---------------------------------------------------------------------------------------


@app.command()
def setup(
    identity: str | None = typer.Option(None, help="Agent handle, e.g. 'my-agent'"),
    project_dir: str | None = typer.Option(None, help="Directory Codex works in (default: here)"),
    sandbox: str = typer.Option(
        "read-only", help=f"Codex sandbox for inbound sessions: {', '.join(SANDBOXES)}"
    ),
    number: bool = typer.Option(False, "--number", help="Also get a phone number for SMS and calls"),
    region: str | None = typer.Option(None, help="Phone number region/area code"),
    base_url: str | None = typer.Option(None, help="Promptli API base URL"),
    start: bool = typer.Option(True, help="Start the background service when done"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Accept the security warning"),
    api_key: bool = typer.Option(False, "--api-key", help="Paste an API key instead of logging in"),
    no_browser: bool = typer.Option(
        False, "--no-browser", help="Print the login link and paste the code (SSH, headless)"
    ),
    test: bool = typer.Option(False, "--test", help="Start in Test mode (simulated, nothing is sent)"),
) -> None:
    """Interactive setup: log in with your browser (or use a key, or sign up) and start the service."""
    try:
        sandbox_for(sandbox)
    except ValueError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(2) from e
    extra = {"sandbox": sandbox}
    typer.secho("Promptli Comms for Codex", bold=True)
    typer.secho("\nBefore you continue:", fg="yellow", bold=True)
    typer.echo(STRANGER_WARNING)
    if not yes and not typer.confirm("\nI understand. Continue?", default=False):
        raise typer.Exit(1)
    url = _base_url(base_url)
    proj = str(Path(project_dir or os.getcwd()).resolve())
    key = os.environ.get("PROMPTLI_API_KEY")
    if not key:
        how = "2" if api_key else "1" if no_browser else _ask_how()
        if how == "1":
            cred = _browser_login(url, identity=identity, number=number, test=test, no_browser=no_browser)
            res = asyncio.run(bootstrap(runtime=RUNTIME, credential=cred, project_dir=proj, extra=extra))
            _show_bootstrap(res, as_json=False)
            if start:
                _start()
            return
        if how == "2":
            key = typer.prompt("Paste the key (input hidden)", hide_input=True).strip()
        else:
            human = typer.prompt("Your email (we send you a confirmation code)")
            handle: str = identity or typer.prompt("Pick a handle for the agent")
            identity = handle
            created = asyncio.run(AsyncPromptli.create_signup(human, handle, test_mode=test, base_url=url))
            key = created.api_key
            typer.echo(f"Signed up. {created.next_step}")

    def run_bootstrap(handle: str | None) -> BootstrapResult:
        return asyncio.run(
            bootstrap(
                runtime=RUNTIME,
                api_key=key,
                identity=handle,
                base_url=url,
                project_dir=proj,
                phone_region=region,
                want_number=number,
                extra=extra,
            )
        )

    res = run_bootstrap(identity)
    if res.status == "error" and res.error and res.error.get("code") == "identity_required":
        res = run_bootstrap(typer.prompt("This is an organization key. Agent handle (existing or new)"))
    _show_bootstrap(res, as_json=False)
    if res.status == "requires_human":
        code = typer.prompt("6-digit code from the email (empty to do it later)", default="")
        if code:
            verify(code)
    if start:
        _start()


def _ask_how() -> str:
    typer.echo("\nHow do you want to connect?")
    typer.echo("  1) Log in with your browser (recommended): pick or create an agent, nothing to paste")
    typer.echo("  2) Paste an API key")
    typer.echo("  3) Sign up with email (no Promptli account yet)")
    while (how := str(typer.prompt("Choose 1, 2 or 3", default="1")).strip()) not in ("1", "2", "3"):
        typer.echo("Please type 1, 2 or 3.")
    return how


def _browser_login(
    url: str, *, identity: str | None, number: bool, test: bool, no_browser: bool
) -> OAuthCredential:
    def show(link: str) -> None:
        if no_browser:
            typer.echo(f"\nOpen this link on any device and approve:\n  {link}")
        else:
            typer.echo(f"\nOpening your browser to log in. If it doesn't open, use:\n  {link}")

    async def paste() -> str:
        return await asyncio.to_thread(typer.prompt, "Paste the code shown after you approve")

    host = socket.gethostname().split(".")[0]
    try:
        return asyncio.run(
            browser_login(
                url,
                client_name=f"Codex on {host}",
                open_browser=not no_browser,
                login_hint=identity,
                want_number=number,
                env="test" if test else None,
                on_url=show,
                paste_code=paste if no_browser else None,
            )
        )
    except PromptliError as e:
        typer.secho(f"Login failed: {e.message} ({e.code})", fg="red", err=True)
        raise typer.Exit(1) from e


@app.command()
def logout(yes: bool = typer.Option(False, "--yes", "-y")) -> None:
    """Revoke this machine's browser login (or forget the API key) and stop the service. The agent is kept."""
    cfg = GatewayConfig.load(RUNTIME)
    if cfg is None or not cfg.credentials_path.exists():
        typer.echo("Not logged in.")
        return
    if not yes and not typer.confirm(f"Log out @{cfg.identity} on this machine?", default=True):
        raise typer.Exit(1)
    uninstall_autostart(RUNTIME)
    src = None
    with contextlib.suppress(Exception):
        src = cfg.auth()
    if isinstance(src, OAuthToken):
        asyncio.run(src.revoke())
    cfg.credentials_path.unlink(missing_ok=True)
    typer.echo("Logged out" + (" and revoked the browser login." if isinstance(src, OAuthToken) else "."))
    typer.echo("Run `promptli-codex setup` to connect again.")


@app.command("bootstrap")
def bootstrap_cmd(
    identity: str | None = typer.Option(None, help="Agent handle (required for organization keys)"),
    project_dir: str | None = typer.Option(None, help="Directory Codex works in"),
    number: bool = typer.Option(False, "--number", help="Ensure the agent has a phone number"),
    region: str | None = typer.Option(None),
    base_url: str | None = typer.Option(None),
    api_key_stdin: bool = typer.Option(False, help="Read the key from stdin instead of PROMPTLI_API_KEY"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Non-interactive, idempotent setup (for agents and scripts). The key comes from PROMPTLI_API_KEY."""
    key = sys.stdin.readline().strip() if api_key_stdin else os.environ.get("PROMPTLI_API_KEY", "")
    proj = str(Path(project_dir).resolve()) if project_dir else None
    if not key:
        cfg = GatewayConfig.load(RUNTIME)
        if cfg is None:
            typer.secho("Set PROMPTLI_API_KEY (or use --api-key-stdin).", fg="red", err=True)
            raise typer.Exit(2)
        src = cfg.auth()
        if isinstance(src, OAuthToken):
            res = asyncio.run(bootstrap(runtime=RUNTIME, credential=src.cred, project_dir=proj))
            _show_bootstrap(res, as_json)
            return
        key = cfg.api_key()
    res = asyncio.run(
        bootstrap(
            runtime=RUNTIME,
            api_key=key,
            identity=identity,
            base_url=_base_url(base_url),
            project_dir=proj,
            phone_region=region,
            want_number=number,
        )
    )
    _show_bootstrap(res, as_json)


@app.command()
def signup(
    email: str = typer.Option(..., help="Your (the human owner's) email address"),
    handle: str = typer.Option(..., help="Handle for the new agent"),
    test: bool = typer.Option(False, "--test", help="Sign up in test mode (sandbox)"),
    project_dir: str | None = typer.Option(None),
    base_url: str | None = typer.Option(None),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Self-signup without a key: creates the agent; the human confirms with an emailed code."""
    url = _base_url(base_url)
    try:
        created = asyncio.run(AsyncPromptli.create_signup(email, handle, test_mode=test, base_url=url))
    except PromptliError as e:
        typer.secho(f"Signup failed: {e.code}: {e.message}", fg="red", err=True)
        raise typer.Exit(1) from e
    res = asyncio.run(
        bootstrap(
            runtime=RUNTIME,
            api_key=created.api_key,
            identity=None,
            base_url=url,
            project_dir=str(Path(project_dir).resolve()) if project_dir else None,
        )
    )
    _show_bootstrap(res, as_json)


@app.command()
def verify(code: str = typer.Argument(..., help="The 6-digit code emailed to the human")) -> None:
    """Confirm a self-signup with the code the human received."""
    cfg = _require_config()

    async def go() -> bool:
        async with AsyncPromptli(cfg.auth(), base_url=cfg.base_url) as p:
            return await p.signup.verify(code)

    try:
        ok = asyncio.run(go())
    except PromptliError as e:
        typer.secho(f"{e.code}: {e.message}", fg="red", err=True)
        raise typer.Exit(1) from e
    msg = "Verified: the agent is fully active." if ok else "Code not accepted."
    typer.secho(msg, fg="green" if ok else "red")
    if not ok:
        raise typer.Exit(1)


# ---- run / service ---------------------------------------------------------------------------------


def _require_config() -> GatewayConfig:
    cfg = GatewayConfig.load(RUNTIME)
    if cfg is None:
        typer.secho("Not set up yet: run `promptli-codex setup`.", fg="red", err=True)
        raise typer.Exit(2)
    return cfg


def make_adapter(cfg: GatewayConfig) -> CodexAdapter:
    return CodexAdapter(
        model=cfg.extra.get("model"),
        voice_model=cfg.extra.get("voice_model"),
        sandbox=cfg.extra.get("sandbox"),
        codex_bin=cfg.extra.get("codex_bin"),
    )


@app.command()
def run(verbose: bool = typer.Option(False, "-v", "--verbose")) -> None:
    """Run the gateway in the foreground (this is what the background service runs)."""
    cfg = _require_config()
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for noisy in ("httpx", "mcp", "uvicorn"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        adapter = make_adapter(cfg)
    except ValueError as e:
        typer.secho(f"Bad config: {e}", fg="red", err=True)
        raise typer.Exit(2) from e

    async def main_loop() -> None:
        gw = Gateway(cfg, adapter)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, gw.stop)
        await gw.run()

    asyncio.run(main_loop())


def service_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k in SERVICE_ENV}


def _start() -> None:
    cfg = _require_config()
    path = install_autostart(RUNTIME, [sys.executable, "-m", "promptli_codex", "run"], env=service_env())
    kind = "systemd --user" if sys.platform.startswith("linux") else "launchd"
    typer.secho(f"Background service started for @{cfg.identity} ({kind}: {path}).", fg="green")
    typer.echo("Logs: promptli-codex logs -f")
    if sys.platform.startswith("linux"):
        typer.echo("To keep it running after you log out: loginctl enable-linger $USER")


@app.command()
def start() -> None:
    """Install and start the background service (launchd on macOS, systemd --user on Linux)."""
    _start()


@app.command()
def stop() -> None:
    """Stop the background service and remove it from login items."""
    typer.echo("Stopped." if uninstall_autostart(RUNTIME) else "Service was not installed.")


@app.command()
def restart() -> None:
    """Restart the background service."""
    uninstall_autostart(RUNTIME)
    _start()


@app.command()
def status(as_json: bool = typer.Option(False, "--json")) -> None:
    """Show the agent, its channels and whether the service is running."""
    cfg = GatewayConfig.load(RUNTIME)
    running, detail = autostart_status(RUNTIME)
    data: dict[str, Any] = {
        "version": __version__,
        "configured": cfg is not None,
        "service": {"running": running, "detail": detail},
    }
    if cfg is not None:
        data.update(
            identity=cfg.identity,
            base_url=cfg.base_url,
            project_dir=cfg.project_dir,
            sandbox=cfg.extra.get("sandbox", "read-only"),
        )
        with contextlib.suppress(Exception):

            async def channels() -> dict[str, Any]:
                async with AsyncPromptli(cfg.auth(), base_url=cfg.base_url) as p:
                    st = await p.identity(cfg.identity).channel_status()
                    return {"email": st.email.address, "sms": st.sms.number, "sms_status": st.sms.status}

            data["channels"] = asyncio.run(channels())
    if as_json:
        _print_json(data)
        return
    if cfg is None:
        typer.echo("Not set up: run `promptli-codex setup`.")
        return
    ch = data.get("channels", {})
    typer.echo(f"Agent      @{cfg.identity}")
    typer.echo(f"Email      {ch.get('email') or '-'}")
    typer.echo(f"Phone      {ch.get('sms') or '-'}")
    typer.echo(f"Project    {cfg.project_dir or home_dir(RUNTIME) / 'workspace'}")
    typer.echo(f"Sandbox    {data['sandbox']}")
    typer.echo(f"Service    {'running' if running else detail}")


@app.command()
def doctor(as_json: bool = typer.Option(False, "--json")) -> None:
    """Check the login, channels, Agent Link, clock, service and Codex login."""
    rep = asyncio.run(run_doctor(RUNTIME, runtime_check=codex_check))
    if as_json:
        _print_json(rep.as_dict())
    else:
        for c in rep.checks:
            mark = typer.style("ok  ", fg="green") if c.ok else typer.style("FAIL", fg="red")
            typer.echo(f"{mark} {c.name:<14} {c.detail}")
    if not rep.ok:
        raise typer.Exit(1)


def log_command(follow: bool, lines: int) -> list[str] | None:
    """journalctl for a systemd --user service (Linux); None means read the launchd log files."""
    if not sys.platform.startswith("linux") or shutil.which("journalctl") is None:
        return None
    cmd = ["journalctl", "--user", "-u", f"{label(RUNTIME)}.service", "-n", str(lines), "--no-pager"]
    return [*cmd, "-f"] if follow else cmd


@app.command()
def logs(follow: bool = typer.Option(False, "-f", "--follow"), lines: int = typer.Option(50, "-n")) -> None:
    """Show the background service's logs."""
    cmd = log_command(follow, lines)
    if cmd is not None:
        os.execvp(cmd[0], cmd)  # noqa: S606
    d = home_dir(RUNTIME) / "logs"
    files = [p for p in (d / "gateway.err.log", d / "gateway.log") if p.exists()]
    if not files:
        typer.echo(f"No logs yet in {d}.")
        return
    if follow:
        os.execvp("tail", ["tail", "-n", str(lines), "-F", *map(str, files)])  # noqa: S606, S607
    for f in files:
        for line in f.read_text(errors="replace").splitlines()[-lines:]:
            typer.echo(line)


@app.command()
def mcp() -> None:
    """Serve the Promptli tools over MCP stdio with this machine's login (alternative to the hosted server).

    Add it to Codex with: codex mcp add promptli-local -- promptli-codex mcp
    """
    from promptli_gateway.mcp_stdio import serve

    asyncio.run(serve(RUNTIME))


@app.command("mcp-login")
def mcp_login(
    no_browser: bool = typer.Option(False, "--no-browser", help="Print the URL instead of opening a browser"),
) -> None:
    """Add the hosted Promptli MCP server to Codex (if needed) and log in to it with OAuth."""
    cli = shutil.which("codex") or codex_cli()
    if cli is None:
        typer.secho("Codex not found: install it first (npm i -g @openai/codex).", fg="red", err=True)
        raise typer.Exit(2)
    have = subprocess.run([cli, "mcp", "get", "promptli"], capture_output=True, text=True, check=False)  # noqa: S603
    if have.returncode != 0:
        subprocess.run([cli, "mcp", "add", "promptli", "--url", HOSTED_MCP_URL], check=True)  # noqa: S603
    cmd = [cli, "mcp", "login", "promptli", *(["--no-browser"] if no_browser else [])]
    raise typer.Exit(subprocess.run(cmd, check=False).returncode)  # noqa: S603


@app.command()
def uninstall(
    purge: bool = typer.Option(False, help="Also delete local config, key and state"),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Stop the service; with --purge also remove ~/.promptli/promptli-codex (the agent itself is kept)."""
    uninstall_autostart(RUNTIME)
    if purge:
        d = home_dir(RUNTIME)
        if yes or typer.confirm(f"Delete {d}?", default=False):
            shutil.rmtree(d, ignore_errors=True)
    typer.echo("Uninstalled. The agent and its history stay in your Promptli account.")


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(f"promptli-codex {__version__}")


def main() -> None:
    app()
