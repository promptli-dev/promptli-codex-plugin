<img src="assets/icon.png" alt="Promptli Comms" width="96" align="right">

# Promptli Comms for Codex

Give OpenAI Codex its own email address and phone number. People email, text or call your agent, and
Codex answers in your project. It can also send email and texts and place calls itself. Works with the
Codex CLI, the IDE extension and the Codex app.

```
person ──email/SMS/call──▶ Promptli Comms ──Agent Link (WebSocket)──▶ promptli-codex ──▶ codex app-server
                                                                     (your Mac/Linux)    (one thread per person)
```

There are two parts, and you can use either one alone:

1. **Tools in Codex** (the plugin). Adds the hosted Promptli MCP server
   (`https://api.comms.promptli.com/mcp`, OAuth, nothing to paste) and skills that teach Codex how to email,
   text and call.
2. **Inbound** (the `promptli-codex` service). A background service that lets people reach your agent:
   - **No tunnels or webhooks.** It keeps an outbound WebSocket to Promptli and resumes from where it left
     off, so nothing is lost while your computer sleeps.
   - **One Codex thread per person.** The same person by email and then by text lands in the same
     conversation. Threads survive restarts (thread ids are persisted and resumed).
   - **Streaming.** Replies stream from Codex as they are written, so live phone calls work.
   - **Driven the supported way.** It uses OpenAI's [Python SDK](https://github.com/openai/codex/tree/main/sdk/python)
     (`openai-codex`), which runs `codex app-server` with a pinned Codex runtime and your existing Codex login.

> **Security:** anyone who learns the agent's address or number can message it. Each person gets an
> isolated thread, and inbound content is passed to Codex as an *external message* (tool-level authority,
> below your instructions). Threads run in Codex's **read-only sandbox** by default with approval policy
> **never**: anything that needs approval is declined, because nobody is at the keyboard. Promptli's own
> tools are always allowed. Keep the project you point it at, and the sandbox you choose, tight.

## Install (macOS and Linux)

```bash
curl -fsSL https://comms.promptli.com/install/codex.sh | sh
codex mcp login promptli     # tools for interactive Codex
promptli-codex setup         # let people email, text or call the agent
```

The installer puts `promptli-codex` in its own virtualenv and adds this repo as a Codex plugin
marketplace, then installs the plugin (MCP server + skills). To add the plugin by hand:

```bash
codex plugin marketplace add promptli-dev/promptli-codex-plugin
codex plugin add promptli-comms@promptli
codex mcp login promptli
```

Or just the tools, without the plugin:

```bash
codex mcp add promptli --url https://api.comms.promptli.com/mcp
codex mcp login promptli
```

`codex mcp login` opens your browser; you pick or create the agent there (OAuth with dynamic client
registration and PKCE). Use `--no-browser` over SSH.

`promptli-codex setup` shows the security warning, then **logs you in with your browser**. There you pick
an existing agent or create one (add `--number` to get it a phone number, `--test` for test mode where
nothing is really sent). Use `--no-browser` over SSH, or `--api-key` to paste a key instead. Then it starts
the background service: **launchd** on macOS, **systemd --user** on Linux (run
`loginctl enable-linger $USER` to keep it running after you log out). The login refreshes itself and shows
up under **API keys → Connected apps**, where you can revoke it. `promptli-codex logout` revokes it from
this machine. Credentials live in `~/.promptli/promptli-codex/credentials` (mode 600). Codex itself uses
your normal Codex login (`codex login`).

In Codex, the skills are `promptli-comms` (using the tools), `promptli-setup` (onboarding) and
`promptli-status` (status and doctor).

## Commands

| Command | What it does |
| --- | --- |
| `promptli-codex setup` | Interactive setup (browser login, key or signup; project directory; `--sandbox`; phone number with `--number`) |
| `promptli-codex logout` | Revoke this machine's login and stop the service (the agent is kept) |
| `promptli-codex bootstrap --identity <handle> --json` | Non-interactive and idempotent; uses the stored login, or a key from `PROMPTLI_API_KEY` |
| `promptli-codex signup --email you@x.com --handle my-agent` | Self-signup without a key |
| `promptli-codex verify <code>` | Confirm a self-signup with the emailed code |
| `promptli-codex start / stop / restart` | Manage the background service (launchd or systemd --user) |
| `promptli-codex status` | Agent, address, number, sandbox, service state |
| `promptli-codex doctor` | Login, channels, Agent Link, clock, service, Codex login |
| `promptli-codex logs -f` | Follow the service logs (journalctl on Linux, log files on macOS) |
| `promptli-codex run -v` | Run in the foreground (debugging) |
| `promptli-codex mcp` | Promptli tools over MCP stdio with this machine's login (`codex mcp add promptli-local -- promptli-codex mcp`) |
| `promptli-codex mcp-login` | Add the hosted MCP server to Codex if needed and run `codex mcp login promptli` |
| `promptli-codex uninstall [--purge]` | Stop the service; `--purge` deletes local config and state |

How replies work:
- Codex's final answer is sent back on the channel the person used (a threaded email reply, or SMS
  without markdown). On a live call it is spoken as it streams.
- If Codex already replied with a tool, or answers `NO_REPLY`, nothing extra is sent.
- On calls the agent says it is an AI, and it only calls Indian numbers within TRAI calling hours.

## Configuration

`~/.promptli/promptli-codex/config.json` (written by setup):
- `project_dir`: where Codex runs. Your `AGENTS.md` and project config apply there.
- `auto_reply`: send the final answer back automatically.
- `batch_window_s`: texts arriving within this window become one turn.
- `extra.sandbox`: `read-only` (default) or `workspace-write`. Full access is refused.
- `extra.model` / `extra.voice_model`: Codex model for threads, and an optional faster one for live calls
  (set both if you set `voice_model`).
- `extra.codex_bin`: use this Codex binary instead of the runtime pinned by the SDK.

Under the hood, the service serves the Promptli tools to Codex on a loopback MCP server
(`127.0.0.1`, random port, random bearer token passed only in the Codex process environment), so tool
sends are seen by the service and never answered twice. In those threads the plugin's hosted `promptli`
server is switched off with a config override; the skills stay on.

## Development

```bash
uv sync
uv run pytest                     # unit tests (fake Codex app-server, real gateway core and MCP server)
uv run ruff check . && uv run ruff format --check . && uv run pyright
sh scripts/validate-plugin.sh     # install the plugin into a throwaway CODEX_HOME with the real Codex CLI
# live: a logged-in Codex; tools are local fakes, nothing is sent
uv run pytest -m live tests/live
```

Install from a local checkout: `PROMPTLI_CODEX_SOURCE=$PWD PROMPTLI_CODEX_MARKETPLACE=$PWD sh install.sh`.

The runtime-independent pieces live in `promptli-gateway` (the [promptli-comms](https://github.com/promptli-dev/promptli-comms) repo):
- Agent Link, dedupe and acks.
- Contact-keyed queues, framing and auto-reply.
- Bootstrap, doctor, launchd/systemd, and the MCP stdio server.

This repo only adds the Codex adapter (`promptli_codex/adapter.py`), the local tool server, the plugin
files and the CLI.

## Codex docs used

- MCP servers and OAuth: <https://developers.openai.com/codex/mcp>
- Plugins and marketplaces: <https://developers.openai.com/codex/plugins/build>
- Skills: <https://developers.openai.com/codex/skills>
- AGENTS.md: <https://developers.openai.com/codex/guides/agents-md>
- Non-interactive mode (`codex exec`): <https://developers.openai.com/codex/noninteractive>
- Python SDK (app-server): <https://github.com/openai/codex/tree/main/sdk/python>
- Config reference: <https://developers.openai.com/codex/config-reference>
