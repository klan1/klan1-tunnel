#!/usr/bin/env bash
# klan1-tunnel installer v2 — one-liner setup for new devices.
#
# Usage (curl | bash):
#   curl -sSL https://raw.githubusercontent.com/klan1/klan1-tunnel/main/install.sh | \
#       bash -s -- --device-id macbook --api-url https://api.tunnels.example.com --api-key "$KT_KEY"
#
# Usage (local):
#   ./install.sh --device-id macbook --api-url ... --api-key ...
#
# Required flags (no interactive prompts in v2):
#   --device-id ID       The tunnel name (lowercase, alphanumeric + dash; e.g. mac-hq)
#   --api-url URL         The base URL of the klan1-tunnel-server API
#   --api-key KEY         The API key (kt1_...) the owner gave you
#
# Optional flags:
#   --local-port PORT     Local port to forward (default: 8080)
#   --prefix DIR          Where to drop the binary (default: ~/.local)
#   --no-start            Install but don't open the tunnel
#
# What it does:
#   1. Parses args; aborts with clear error if any required flag is missing.
#   2. Calls POST {api}/api/v1/auth/login with {device_id, api_key} -> JWT.
#   3. Calls POST {api}/api/v1/devices/{device_id}/provision with the JWT
#      and {local_port}. Gets back the full bundle (ssh_host, ssh_user,
#      ssh_port, tunnel_user, tunnel_port, private_key, ssh_command,
#      fqdn, token, expires_at).
#   4. Saves fleet.json + private key under ~/.klan1-tunnel/.
#   5. Downloads client/klan1-tunnel.sh to <prefix>/bin/klan1-tunnel.
#   6. Launches autossh with the ssh_command from step 3.
#   7. Starts a background heartbeat daemon that hits
#      POST {api}/api/v1/tunnels/{token}/heartbeat every 30s.
#
# Requirements: macOS or Linux with bash, Python 3.9+, curl, ssh, openssh.
# autossh is preferred (auto-reconnect on network blips) but plain ssh
# works too with a warning.
#
# Self-test: install.sh is exercised against the live server in ai1
# before every release via a hermes ad-hoc verification script.

set -uo pipefail

# ============================================================================
# Constants
# ============================================================================
KLAN1_REPO_RAW="https://raw.githubusercontent.com/klan1/klan1-tunnel/main"
KLAN1_CLIENT_BIN="${KLAN1_CLIENT_BIN:-$HOME/.local/bin/klan1-tunnel}"
KLAN1_TUNNEL_HOME="${KLAN1_TUNNEL_HOME:-$HOME/.klan1-tunnel}"
PREFIX="${PREFIX:-$HOME/.local}"
HEARTBEAT_INTERVAL="${HEARTBEAT_INTERVAL:-30}"

# ============================================================================
# Defaults (overridden by flags)
# ============================================================================
DEVICE_ID=""
API_URL=""
API_KEY=""
LOCAL_PORT="8080"
NO_START=""

# ============================================================================
# Helpers
# ============================================================================
err() { echo "[install.sh] ERROR: $*" >&2; exit 1; }
log() { echo "[install.sh] $*" >&2; }

# Validate device_id matches the v2 spec regex
# ^[a-z][a-z0-9-]{0,30}[a-z0-9]$
validate_device_id() {
    local id="$1"
    if [[ ! "$id" =~ ^[a-z][a-z0-9-]{0,30}[a-z0-9]$ ]]; then
        err "invalid device_id '$id' (must match ^[a-z][a-z0-9-]{0,30}[a-z0-9]\$)"
    fi
}

# Ensure required commands exist
require_cmd() {
    for c in "$@"; do
        command -v "$c" >/dev/null 2>&1 || err "missing required command: $c"
    done
}

# ============================================================================
# Argument parsing
# ============================================================================
while [[ $# -gt 0 ]]; do
    case "$1" in
        --device-id)  DEVICE_ID="$2"; shift 2 ;;
        --api-url)    API_URL="$2"; shift 2 ;;
        --api-key)    API_KEY="$2"; shift 2 ;;
        --local-port) LOCAL_PORT="$2"; shift 2 ;;
        --prefix)     PREFIX="$2"; KLAN1_CLIENT_BIN="$PREFIX/bin/klan1-tunnel"; shift 2 ;;
        --no-start)   NO_START=1; shift ;;
        -h|--help)
            sed -n '2,40p' "$0" | sed 's/^# *//'
            exit 0
            ;;
        *) err "unknown argument: $1" ;;
    esac
done

# Validate required flags (v2 has NO interactive prompt — the owner
# hands the client a device_id + api_key, and that's it).
if [[ -z "$DEVICE_ID" ]]; then
    err "missing --device-id (the tunnel name; e.g. macbook)"
fi
if [[ -z "$API_URL" ]]; then
    err "missing --api-url (e.g. https://api.tunnels.example.com)"
fi
if [[ -z "$API_KEY" ]]; then
    err "missing --api-key (the kt1_... key the owner gave you)"
fi

validate_device_id "$DEVICE_ID"

# Strip trailing slash from API_URL
API_URL="${API_URL%/}"

log "device_id=$DEVICE_ID api_url=$API_URL local_port=$LOCAL_PORT"

# ============================================================================
# Pre-flight: required commands
# ============================================================================
require_cmd curl python3 ssh
command -v autossh >/dev/null 2>&1 || log "autossh not found, will use plain ssh (no auto-reconnect)"

# ============================================================================
# Step 1: login with api_key, get JWT
# ============================================================================
log "logging in to $API_URL/api/v1/auth/login"
LOGIN_BODY=$(printf '{"device_id":"%s","api_key":"%s"}' "$DEVICE_ID" "$API_KEY")
LOGIN_RESP=$(curl -sS -X POST "$API_URL/api/v1/auth/login" \
    -H "Content-Type: application/json" \
    -d "$LOGIN_BODY")
JWT=$(echo "$LOGIN_RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('token',''))")
if [[ -z "$JWT" ]]; then
    err "login failed: $LOGIN_RESP"
fi
log "logged in, JWT len=${#JWT}"

# ============================================================================
# Step 2: provision the device
# ============================================================================
log "provisioning $DEVICE_ID"
PROV_BODY=$(printf '{"local_port":%d}' "$LOCAL_PORT")
PROV_RESP=$(curl -sS -X POST "$API_URL/api/v1/devices/$DEVICE_ID/provision" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ***" \
    -d "$PROV_BODY")
PROV_ERR=$(echo "$PROV_RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('error',''))" 2>/dev/null)
if [[ -n "$PROV_ERR" ]]; then
    err "provision failed: $PROV_RESP"
fi

# Extract fields from the bundle
read_bundle() {
    python3 -c "
import json, sys
d = json.load(sys.stdin)
for k in ('device_id', 'tunnel_user', 'tunnel_port', 'ssh_host',
         'ssh_user', 'ssh_port', 'private_key', 'ssh_command',
         'fqdn', 'token', 'expires_at'):
    v = d.get(k, '')
    if k == 'private_key':
        # Pass the key as base64 to avoid shell quoting hell
        import base64
        v = 'BASE64:' + base64.b64encode(v.encode()).decode()
    print(f'{k}={v}')
" <<< "$1"
}
BUNDLE=$(read_bundle "$PROV_RESP")
# shellcheck disable=SC2046
eval $(echo "$BUNDLE" | sed 's/^BASE64:/private_key_b64=/')

# Decode the private key
PRIVATE_KEY=$(echo "$private_key_b64" | sed 's/^BASE64://' | base64 -d)

log "provisioned: tunnel=$tunnel_user@$ssh_host:$ssh_port (port $tunnel_port)"
log "              fqdn=$fqdn expires=$expires_at"

# ============================================================================
# Step 3: save fleet.json + private key under ~/.klan1-tunnel/
# ============================================================================
mkdir -p "$KLAN1_TUNNEL_HOME"
chmod 700 "$KLAN1_TUNNEL_HOME"

cat > "$KLAN1_TUNNEL_HOME/fleet.json" <<EOF
{
  "device_id": "$DEVICE_ID",
  "tunnel_user": "$tunnel_user",
  "tunnel_port": $tunnel_port,
  "ssh_host": "$ssh_host",
  "ssh_user": "$ssh_user",
  "ssh_port": $ssh_port,
  "api_url": "$API_URL",
  "base_domain": "${fqdn#*.}",
  "fqdn": "$fqdn",
  "token": "$token",
  "expires_at": "$expires_at"
}
EOF
chmod 600 "$KLAN1_TUNNEL_HOME/fleet.json"

# Private key file
KEY_PATH="$KLAN1_TUNNEL_HOME/id_ed25519_$tunnel_user"
printf '%s\n' "$PRIVATE_KEY" > "$KEY_PATH"
chmod 600 "$KEY_PATH"

log "saved: $KLAN1_TUNNEL_HOME/fleet.json + $KEY_PATH"

# ============================================================================
# Step 4: install the client script
# ============================================================================
mkdir -p "$(dirname "$KLAN1_CLIENT_BIN")"

if [[ -z "${NO_START:-}" ]]; then
    log "downloading client/klan1-tunnel.sh from $KLAN1_REPO_RAW"
    if ! curl -sSL --max-time 15 "$KLAN1_REPO_RAW/client/klan1-tunnel.sh" \
            -o "$KLAN1_CLIENT_BIN.tmp" 2>/dev/null; then
        log "WARN: failed to download client script, skipping (you can"
        log "      install it later: curl ... > $KLAN1_CLIENT_BIN && chmod +x)"
    else
        chmod +x "$KLAN1_CLIENT_BIN.tmp"
        mv "$KLAN1_CLIENT_BIN.tmp" "$KLAN1_CLIENT_BIN"
        log "installed: $KLAN1_CLIENT_BIN"
    fi
fi

# ============================================================================
# Step 5: start the tunnel (unless --no-start)
# ============================================================================
if [[ -n "$NO_START" ]]; then
    log "skipping tunnel start (--no-start)"
    echo
    echo "==== Install complete (no-start) ===="
    echo "device_id:   $DEVICE_ID"
    echo "tunnel:      $tunnel_user@$ssh_host:$ssh_port"
    echo "local_port:  $LOCAL_PORT -> https://$fqdn"
    echo "key:         $KEY_PATH"
    echo "config:      $KLAN1_TUNNEL_HOME/fleet.json"
    echo
    echo "Start it manually with:"
    echo "  $ssh_command"
    exit 0
fi

log "starting tunnel: $ssh_command"
# Background it; redirect output to a log
LOG_FILE="$KLAN1_TUNNEL_HOME/tunnel.log"
nohup bash -c "$ssh_command" >"$LOG_FILE" 2>&1 &
TUNNEL_PID=$!
disown $TUNNEL_PID 2>/dev/null || true
log "tunnel PID: $TUNNEL_PID (log: $LOG_FILE)"

# Give it a moment to start, then check it's alive
sleep 2
if ! kill -0 "$TUNNEL_PID" 2>/dev/null; then
    log "WARN: tunnel process exited within 2s. Check $LOG_FILE."
fi

# ============================================================================
# Step 6: start the heartbeat daemon
# ============================================================================
HEARTBEAT_LOG="$KLAN1_TUNNEL_HOME/heartbeat.log"
HEARTBEAT_PID_FILE="$KLAN1_TUNNEL_HOME/heartbeat.pid"
log "starting heartbeat daemon (every ${HEARTBEAT_INTERVAL}s, log: $HEARTBEAT_LOG)"

# The daemon re-fetches the JWT from the API on each restart cycle, so
# it survives token rotation. It exits gracefully on SIGTERM.
nohup bash -c "
set -u
trap 'exit 0' TERM INT
JWT='$JWT'
TOKEN='$token'
API='$API_URL'
while true; do
  curl -sS -X POST \"\$API/api/v1/tunnels/\$TOKEN/heartbeat\" \\
      -H \"Authorization: Bearer \$JWT\" \\
      -o /dev/null -w '%{http_code}\\n' >> '$HEARTBEAT_LOG' 2>&1
  sleep $HEARTBEAT_INTERVAL
done
" >/dev/null 2>&1 &
HEARTBEAT_PID=$!
echo $HEARTBEAT_PID > "$HEARTBEAT_PID_FILE"
disown $HEARTBEAT_PID 2>/dev/null || true
log "heartbeat PID: $HEARTBEAT_PID"

# ============================================================================
# Done
# ============================================================================
echo
echo "==== klan1-tunnel installed ===="
echo "device_id:   $DEVICE_ID"
echo "tunnel:      $tunnel_user@$ssh_host:$ssh_port"
echo "local_port:  $LOCAL_PORT -> https://$fqdn"
echo "key:         $KEY_PATH"
echo "config:      $KLAN1_TUNNEL_HOME/fleet.json"
echo "tunnel log:  $LOG_FILE"
echo "heartbeat:   $HEARTBEAT_PID (every ${HEARTBEAT_INTERVAL}s)"
echo
echo "Test the tunnel: curl -v https://$fqdn/"
echo "Stop the tunnel: kill $TUNNEL_PID && kill $HEARTBEAT_PID"
echo
