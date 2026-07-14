#!/usr/bin/env bash
# klan1-tunnel one-shot installer.
# Hosted at https://klan1.net/tunel-install.sh (Plesk on ws1).
#
# Usage:
#   curl -fsSL https://klan1.net/tunel-install.sh | bash
#
# What it does (idempotent — safe to re-run):
#   1. Picks an install dir (~/.local/bin by default).
#   2. Downloads the klan1-tunel CLI from the klan1-tunnel repo (main branch)
#      and writes it to <prefix>/bin/klan1-tunel.
#   3. Creates ~/.klan1-tunnel/ (state dir for config + keys + logs).
#   4. If config.json doesn't exist, prompts for device_id + api_key +
#      api_url and writes it.
#   5. Runs `klan1-tunel up` to provision the tunnel and start the SSH
#      reverse-tunnel + heartbeat.
#
# After this completes the user never needs to curl|bash again —
# they invoke `klan1-tunel` for everything (up / down / status / proxy / logs).

set -uo pipefail

REPO_RAW="${KLAN1_TUNNEL_REPO_RAW:-https://raw.githubusercontent.com/klan1/klan1-tunnel/main}"
PREFIX="${PREFIX:-$HOME/.local}"
BIN_DIR="$PREFIX/bin"
STATE_DIR="${KLAN1_TUNNEL_HOME:-$HOME/.klan1-tunnel}"
CLI="$BIN_DIR/klan1-tunel"

err()  { echo "[klan1-install] ERROR: $*" >&2; exit 1; }
log()  { echo "[klan1-install] $*" >&2; }

require_cmd() {
    for c in "$@"; do
        command -v "$c" >/dev/null 2>&1 || err "missing required command: $c"
    done
}

require_cmd curl bash python3 ssh
[[ "$(uname -s)" == "Darwin" || "$(uname -s)" == "Linux" ]] \
    || err "unsupported OS: $(uname -s) (need macOS or Linux)"

# ---------------------------------------------------------------------------
# Step 1: pick a writable prefix
# ---------------------------------------------------------------------------
# If ~/.local/bin is not on PATH, fall back to ~/.klan1-tunnel/bin and
# print a hint about adding it to PATH.
if ! echo "$PATH" | tr ':' '\n' | grep -qx "$BIN_DIR"; then
    if [[ -d "$HOME/.local/bin" ]] || mkdir -p "$HOME/.local/bin" 2>/dev/null; then
        BIN_DIR="$HOME/.local/bin"
        CLI="$BIN_DIR/klan1-tunel"
    else
        BIN_DIR="$STATE_DIR/bin"
        CLI="$BIN_DIR/klan1-tunel"
    fi
fi
mkdir -p "$BIN_DIR" "$STATE_DIR" || err "cannot create $BIN_DIR or $STATE_DIR"
log "prefix=$BIN_DIR state=$STATE_DIR"

# ---------------------------------------------------------------------------
# Step 2: download the klan1-tunel CLI from the repo
# ---------------------------------------------------------------------------
log "downloading klan1-tunel CLI from $REPO_RAW/client/klan1"
TMP="$(mktemp -t klan1-tunel-cli.XXXXXX)"
if ! curl -fsSL --max-time 20 "$REPO_RAW/client/klan1" -o "$TMP"; then
    rm -f "$TMP"
    err "failed to download klan1-tunel CLI from $REPO_RAW/client/klan1

Check your internet connection, or set KLAN1_TUNNEL_REPO_RAW to a mirror."
fi
chmod +x "$TMP"
mv "$TMP" "$CLI"
log "installed: $CLI"

# ---------------------------------------------------------------------------
# Step 3: ensure config.json exists (prompt if not)
# ---------------------------------------------------------------------------
CONFIG="$STATE_DIR/config.json"
if [[ ! -f "$CONFIG" ]]; then
    log "no config yet — first-time setup"
    echo
    echo "=== First-time setup ==="
    echo "We need a few values to talk to your klan1-tunnel server."
    echo "You should have received these from your server admin:"
    echo

    # Defaults
    DEFAULT_API_URL="https://api.tunnels.klan1.net"

    if [[ -t 0 ]]; then
        # interactive
        read -rp "  API URL  [$DEFAULT_API_URL]: " API_URL
        API_URL="${API_URL:-$DEFAULT_API_URL}"
        while true; do
            read -rp "  device_id (lowercase, e.g. macbook): " DEVICE_ID
            [[ "$DEVICE_ID" =~ ^[a-z][a-z0-9-]{0,30}[a-z0-9]$ ]] && break
            echo "  invalid (must match ^[a-z][a-z0-9-]{0,30}[a-z0-9]\$)"
        done
        read -rp "  api_key (kt1_...): " API_KEY
        read -rp "  local port [8080]: " LOCAL_PORT
        LOCAL_PORT="${LOCAL_PORT:-8080}"
    else
        # non-interactive (piped from curl) — read from env or error out
        : "${API_URL:=$DEFAULT_API_URL}"
        : "${DEVICE_ID:?set DEVICE_ID env var in non-interactive mode}"
        : "${API_KEY:?set API_KEY env var in non-interactive mode}"
        : "${LOCAL_PORT:=8080}"
    fi

    umask 077
    cat > "$CONFIG" <<EOF
{
  "api_url": "$API_URL",
  "device_id": "$DEVICE_ID",
  "api_key": "$API_KEY",
  "local_port": $LOCAL_PORT
}
EOF
    chmod 600 "$CONFIG"
    log "wrote $CONFIG (mode 0600)"
fi

# ---------------------------------------------------------------------------
# Step 4: ensure prefix/bin is on PATH for this session + future shells
# ---------------------------------------------------------------------------
if ! echo "$PATH" | tr ':' '\n' | grep -qx "$BIN_DIR"; then
    # Detect shell rc file
    case "${SHELL:-/bin/bash}" in
        */zsh)  RC="$HOME/.zshrc" ;;
        */bash) RC="$HOME/.bashrc" ;;
        *)      RC="$HOME/.profile" ;;
    esac
    if [[ -f "$RC" ]] && ! grep -q "$BIN_DIR" "$RC" 2>/dev/null; then
        echo "" >> "$RC"
        echo "# klan1-tunnel CLI" >> "$RC"
        echo "export PATH=\"$BIN_DIR:\$PATH\"" >> "$RC"
        log "added $BIN_DIR to PATH in $RC (restart shell or: export PATH=$BIN_DIR:\$PATH)"
    fi
    export PATH="$BIN_DIR:$PATH"
fi

# ---------------------------------------------------------------------------
# Step 5: kick the tunnel up
# ---------------------------------------------------------------------------
echo
log "running: klan1-tunel up"
echo
exec klan1-tunel up
