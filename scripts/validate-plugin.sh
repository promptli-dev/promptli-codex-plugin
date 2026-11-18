#!/bin/sh
# Install this checkout as a Codex plugin into a throwaway CODEX_HOME with the real Codex CLI, and check
# that Codex loads its MCP server and skills. Nothing is sent anywhere (a dummy API key is only used so
# `codex mcp list` exposes plugin servers; no model call is made).
#
#   sh scripts/validate-plugin.sh [path/to/codex]
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
CODEX="${1:-$(uv run --project "$ROOT" python -c 'from promptli_codex.cli import codex_cli; print(codex_cli())')}"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
export CODEX_HOME="$WORK/home"
mkdir -p "$CODEX_HOME" "$WORK/src"

# Copy only tracked/unignored files (no .venv), the way a Git marketplace checkout looks.
(cd "$ROOT" && git ls-files -co --exclude-standard -z | xargs -0 tar cf -) | (cd "$WORK/src" && tar xf -)

"$CODEX" --version
"$CODEX" plugin marketplace add "$WORK/src" 2>&1 | grep -v "PATH aliases"
"$CODEX" plugin add promptli-comms@promptli --json 2>&1 | grep -v "PATH aliases" | tee "$WORK/add.json"
grep -q '"pluginId": "promptli-comms@promptli"' "$WORK/add.json"

CODEX_API_KEY=sk-dummy "$CODEX" mcp list --json 2>/dev/null > "$WORK/mcp.json"
python3 - "$WORK/mcp.json" <<'PY'
import json, sys
servers = {s["name"]: s for s in json.load(open(sys.argv[1]))}
s = servers.get("promptli")
assert s, f"promptli MCP server not loaded: {list(servers)}"
assert s["transport"]["type"] == "streamable_http", s
assert s["transport"]["url"] == "https://api.comms.promptli.com/mcp", s
print("ok: plugin MCP server", s["transport"]["url"], "auth:", s.get("auth_status"))
PY

PLUGIN_DIR=$(find "$CODEX_HOME/plugins/cache/promptli/promptli-comms" -mindepth 1 -maxdepth 1 -type d | head -1)
for skill in promptli-comms promptli-setup promptli-status; do
  test -f "$PLUGIN_DIR/skills/$skill/SKILL.md" || { echo "missing skill $skill" >&2; exit 1; }
done
echo "ok: skills installed in $PLUGIN_DIR"
