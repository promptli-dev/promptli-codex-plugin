# AGENTS.md: promptli-codex-plugin

OpenAI Codex adapter for Promptli Comms. Keep this repo thin: transport, dedupe, sessions-by-contact,
framing, setup and doctor belong in `promptli-gateway` (promptli-comms repo), not here.

- `promptli_codex/adapter.py`: CodexAdapter (one `codex app-server` via the `openai-codex` SDK, one thread per contact_key, delta streaming, resume, interrupt; inbound text is an `ExternalMessage`).
- `promptli_codex/toolserver.py`: gateway ToolSpecs → loopback Streamable HTTP MCP server with a random bearer token (passed to Codex only through the env var `PROMPTLI_CODEX_MCP_TOKEN`).
- `promptli_codex/permissions.py`: approval policy `never`, read-only or workspace-write sandbox, decline every approval request.
- `promptli_codex/prompts.py`: developer instructions + the stranger/prompt-injection warning shown at setup.
- `promptli_codex/cli.py`: `promptli-codex` Typer CLI (runtime id / config dir: `promptli-codex`).
- `plugin.json`, `mcp.json`, `skills/`, `.agents/plugins/marketplace.json`: the Codex plugin + marketplace (validate with `sh scripts/validate-plugin.sh`).
- `install.sh`: curl installer (own venv under ~/.local/share/promptli-codex, shim in ~/.local/bin).

Rules:
- Never print, log or commit API keys or tokens. Keys are read from hidden input, env or stdin, never argv.
- Never use `Sandbox.full_access` or `ApprovalMode.auto_review` for inbound threads, and never accept approvals.
- `mcp.json` may only use fields Codex's Agent Plugins parser accepts (it denies unknown fields).
- Keep `plugin.json` and `pyproject.toml` versions equal.
- Checks: `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest && sh scripts/validate-plugin.sh`.
