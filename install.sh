#!/bin/sh
# Promptli Comms for OpenAI Codex: installer.
#
#   curl -fsSL https://comms.promptli.com/install/codex.sh | sh
#
# Installs `promptli-codex` into its own virtualenv (~/.local/share/promptli-codex/venv), links it to
# ~/.local/bin, and adds the Codex plugin (hosted Promptli MCP tools + skills) from the Promptli
# marketplace. It never asks for or stores a key: run `promptli-codex setup` afterwards.
#
# Environment overrides:
#   PROMPTLI_CODEX_SOURCE   pip requirement or local path for the plugin   (default: GitHub main)
#   PROMPTLI_CODEX_MARKETPLACE  marketplace source for `codex plugin marketplace add`
#                               (default: promptli-dev/promptli-codex-plugin)
#   PROMPTLI_COMMS_REF      install the SDK/gateway from this git ref instead of PyPI (development)
#   PROMPTLI_COMMS_SOURCE   git URL used with PROMPTLI_COMMS_REF           (default: GitHub)
#   PROMPTLI_SKIP_PLUGIN=1  do not register the Codex plugin
set -eu

COMMS_SOURCE="${PROMPTLI_COMMS_SOURCE:-https://github.com/promptli-dev/promptli-comms}"
COMMS_REF="${PROMPTLI_COMMS_REF:-}"
PLUGIN_SOURCE="${PROMPTLI_CODEX_SOURCE:-git+https://github.com/promptli-dev/promptli-codex-plugin@main}"
MARKETPLACE="${PROMPTLI_CODEX_MARKETPLACE:-promptli-dev/promptli-codex-plugin}"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/promptli-codex"
VENV="$DATA_DIR/venv"
BIN_DIR="$HOME/.local/bin"

say() { printf '\033[1;38;5;202m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

case "$(uname -s)" in
  Darwin) SERVICE="launchd" ;;
  Linux) SERVICE="systemd --user" ;;
  *) die "unsupported OS: $(uname -s) (macOS and Linux are supported)" ;;
esac

if ! command -v uv >/dev/null 2>&1; then
  if [ -x "$HOME/.local/bin/uv" ]; then
    PATH="$HOME/.local/bin:$PATH"
  else
    say "Installing uv (Python package manager from astral.sh)"
    curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
    PATH="$HOME/.local/bin:$PATH"
  fi
fi
command -v uv >/dev/null 2>&1 || die "uv is required: https://docs.astral.sh/uv/"

say "Creating $VENV"
mkdir -p "$DATA_DIR" "$BIN_DIR"
uv venv --quiet --allow-existing --python ">=3.11" "$VENV"

say "Installing promptli-codex (with the Codex runtime pinned by the openai-codex SDK)"
if [ -n "$COMMS_REF" ]; then  # development: SDK + gateway from a git ref
  set -- "promptli[cli] @ git+$COMMS_SOURCE@$COMMS_REF#subdirectory=sdk/python" \
    "promptli-gateway @ git+$COMMS_SOURCE@$COMMS_REF#subdirectory=gateway"
else  # released packages from PyPI
  set -- "promptli[cli]>=0.2.2,<0.3" "promptli-gateway>=0.2.2,<0.3"
fi
uv pip install --quiet --no-sources --python "$VENV/bin/python" "$@" "$PLUGIN_SOURCE"

ln -sf "$VENV/bin/promptli-codex" "$BIN_DIR/promptli-codex"
"$BIN_DIR/promptli-codex" version >/dev/null || die "install failed: promptli-codex does not run"

if [ "${PROMPTLI_SKIP_PLUGIN:-0}" != "1" ]; then
  if command -v codex >/dev/null 2>&1; then
    say "Adding the Codex plugin (marketplace: promptli)"
    codex plugin marketplace add "$MARKETPLACE" >/dev/null 2>&1 \
      || codex plugin marketplace upgrade promptli >/dev/null 2>&1 || warn "could not add the marketplace"
    codex plugin add promptli-comms@promptli >/dev/null 2>&1 \
      || warn "could not install the plugin; run: codex plugin add promptli-comms@promptli"
  else
    warn "Codex CLI not found; install it (npm i -g @openai/codex), then run:"
    warn "  codex plugin marketplace add $MARKETPLACE && codex plugin add promptli-comms@promptli"
  fi
fi

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) warn "$BIN_DIR is not on your PATH. Add it: echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.profile" ;;
esac

say "Installed $("$BIN_DIR/promptli-codex" version) (background service: $SERVICE)"
cat <<'EOF'

Next:
  codex mcp login promptli   log in to the Promptli tools for interactive Codex (browser, nothing to paste)
  promptli-codex setup       let people email, text or call your agent (starts the background service)
  promptli-codex doctor      check everything is working

Anyone who learns your agent's address or number can message it. Inbound sessions run in Codex's
read-only sandbox by default and never get approval to escalate.
EOF
