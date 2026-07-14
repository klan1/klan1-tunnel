#!/usr/bin/env bash
# klan1-tunnel proxy — one-shot deploy of a forward proxy on a public URL.
#
# Provision a tunnel AND launch a local pproxy server, so the user can
# route traffic through the tunnel as either a SOCKS5 or HTTP CONNECT
# proxy. Both protocols share a single port (pproxy combines them
# natively: `http+socks5://...`).
#
# Usage (curl | bash):
#   curl -sSL https://raw.githubusercontent.com/klan1/klan1-tunnel/main/proxy.sh | \
#       bash -s -- --device-id macbook --api-url https://api.tunnels.example.com --api-key "$KT_KEY"
#
# Subcommands:
#   start  (default)   Provision (if needed) + ensure tunnel up + launch pproxy
#   stop               Kill pproxy + the tunnel + heartbeat
#   status             Show pproxy + tunnel + heartbeat state
#
# Required flags (same as install.sh):
#   --device-id ID       The tunnel name (e.g. macbook)
#   --api-url URL         The base URL of the klan1-tunnel-server API
#   --api-key KEY         The API key (kt1_...) the owner gave you
#
# Optional flags:
#   --local-port PORT     Local port the tunnel forwards to (default: 8080).
#                         pproxy will listen here. Caddy reverse-proxies the
#                         public URL -> 127.0.0.1:<remote_port> -> tunnel -> :LOCAL_PORT.
#   --proxy-port PORT     Local port for pproxy (default: same as --local-port).
#                         Only used if you want pproxy on a different port than
#                         what the tunnel forwards.
#   --no-launch           Provision + set up the tunnel but don't start pproxy.
#                         (Useful for CI / scripting.)
#
# What it does (start):
#   1. Parses args (same as install.sh).
#   2. If a tunnel already exists for this device_id in fleet.json AND the
#      SSH tunnel is alive, reuses it. Otherwise calls the same provision
#      endpoints as install.sh (login + POST /devices/<id>/provision) to
#      create it.
#   3. Installs pproxy via `pip install --user pproxy` if not on PATH.
#   4. Launches pproxy on 127.0.0.1:<port> as `http+socks5://...` (one port
#      serves both HTTP CONNECT and SOCKS5).
#   5. Starts a heartbeat daemon (same as install.sh) if not already running.
#   6. Prints:
#        - local proxy URL  (socks5://127.0.0.1:<port>  and  http://127.0.0.1:<port>)
#        - public URL       (https://<device_id>.<base_domain>) — once Caddy cert
#                            is issued (~1-3 min) you can also use this as the proxy
#                            by sending -x https://<url>:443 http://target
#
# Idempotency:
#   - Re-running `start` with a live tunnel + pproxy is a no-op.
#   - `stop` cleans up pproxy + tunnel + heartbeat (does NOT release the
#     tunnel on the server; use `install.sh --no-start` or the server's
#     DELETE /api/v1/tunnels/<token> to release).

set -uo pipefail

# ============================================================================
# Constants
# ============================================================================
KLAN1_TUNNEL_HOME="${KLAN1_TUNNEL_HOME:-$HOME/.klan1-tunnel}"
HEARTBEAT_INTERVAL="${HEARTBEAT_INTERVAL:-30}"
PPROXY_INSTALL_CMD="${PPROXY_INSTALL_CMD:-python3 -m pip install --user pproxy}"

# ============================================================================
# Defaults
# ============================================================================
DEVICE_ID=""
API_URL=""
API_KEY=""
LOCAL_PORT="8080"
PROXY_PORT=""
NO_LAUNCH=""

# ============================================================================
# Helpers
# ============================================================================
err() { echo "[proxy.sh] ERROR: $*" >&2; exit 1; }
log() { echo "[proxy.sh] $*" >&2; }

validate_device_id() {
    local id="$1"
    if [[ ! "$id" =~ ^[a-z][a-z0-9-]{0,30}[a-z0-9]$ ]]; then
        err "invalid device_id '$id' (must match ^[a-z][a-z0-9-]{0,30}[a-z0-9]\$)"
    fi
}

require_cmd() {
    for c in "$@"; do
        command -v "$c" >/dev/null 2>&1 || err "missing required command: $c"
    done
}

# ============================================================================
# Argument parsing
# ============================================================================
SUBCMD="start"
while [[ $# -gt 0 ]]; do
    case "$1" in
        start|stop|status) SUBCMD="$1"; shift ;;
        --device-id)   DEVICE_ID="$2"; shift 2 ;;
        --api-url)     API_URL="$2"; shift 2 ;;
        --api-key)     API_KEY="$2"; shift 2 ;;
        --local-port)  LOCAL_PORT="$2"; shift 2 ;;
        --proxy-port)  PROXY_PORT="$2"; shift 2 ;;
        --no-launch)   NO_LAUNCH=1; shift ;;
        -h|--help)
            sed -n '2,40p' "$0" | sed 's/^# *//'
            exit 0
            ;;
        *) err "unknown argument: $1" ;;
    esac
done

# If PROXY_PORT is empty, mirror LOCAL_PORT (so the tunnel and pproxy
# use the same local port — the simplest case).
[[ -z "$PROXY_PORT" ]] && PROXY_PORT="$LOCAL_PORT"

# ============================================================================
# Subcommand: status
# ============================================================================
if [[ "$SUBCMD" == "status" ]]; then
    echo "==== klan1-proxy status ===="
    echo
    echo "fleet.json:    ${KLAN1_TUNNEL_HOME}/fleet.json"
    if [[ -f "${KLAN1_TUNNEL_HOME}/fleet.json" ]]; then
        cat "${KLAN1_TUNNEL_HOME}/fleet.json" | python3 -m json.tool 2>/dev/null | sed 's/^/  /'
    else
        echo "  (no tunnel provisioned)"
    fi
    echo
    echo "pproxy:"
    PPROXY_PID=$(pgrep -f "pproxy.*-l.*:${PROXY_PORT}" 2>/dev/null | head -1)
    if [[ -n "$PPROXY_PID" ]]; then
        echo "  PID=$PPROXY_PID listening on 127.0.0.1:${PROXY_PORT}"
    else
        echo "  NOT running on port ${PROXY_PORT}"
    fi
    echo
    echo "tunnel (ssh):"
    if [[ -f "${KLAN1_TUNNEL_HOME}/fleet.json" ]]; then
        TUNNEL_USER=$(python3 -c "import json; print(json.load(open('${KLAN1_TUNNEL_HOME}/fleet.json')).get('tunnel_user',''))" 2>/dev/null)
        TUNNEL_PID=$(pgrep -f "ssh.*${TUNNEL_USER}@" 2>/dev/null | head -1)
        if [[ -n "$TUNNEL_PID" ]]; then
            echo "  PID=$TUNNEL_PID for ${TUNNEL_USER}"
        else
            echo "  NOT running"
        fi
    fi
    echo
    echo "heartbeat:"
    if [[ -f "${KLAN1_TUNNEL_HOME}/heartbeat.pid" ]]; then
        HB_PID=$(cat "${KLAN1_TUNNEL_HOME}/heartbeat.pid" 2>/dev/null)
        if kill -0 "$HB_PID" 2>/dev/null; then
            echo "  PID=$HB_PID (every ${HEARTBEAT_INTERVAL}s)"
        else
            echo "  PID $HB_PID in pid file but not running"
        fi
    else
        echo "  no pid file"
    fi
    exit 0
fi

# ============================================================================
# Subcommand: stop
# ============================================================================
if [[ "$SUBCMD" == "stop" ]]; then
    log "stopping pproxy"
    pkill -f "pproxy.*-l.*:${PROXY_PORT:-.*}" 2>/dev/null || true
    log "stopping tunnel (ssh)"
    if [[ -f "${KLAN1_TUNNEL_HOME}/fleet.json" ]]; then
        TUNNEL_USER=$(python3 -c "import json; print(json.load(open('${KLAN1_TUNNEL_HOME}/fleet.json')).get('tunnel_user',''))" 2>/dev/null)
        pkill -f "ssh.*${TUNNEL_USER}@" 2>/dev/null || true
    fi
    log "stopping heartbeat"
    if [[ -f "${KLAN1_TUNNEL_HOME}/heartbeat.pid" ]]; then
        HB_PID=$(cat "${KLAN1_TUNNEL_HOME}/heartbeat.pid" 2>/dev/null)
        kill "$HB_PID" 2>/dev/null || true
        rm -f "${KLAN1_TUNNEL_HOME}/heartbeat.pid"
    fi
    pkill -f "while true.*heartbeat" 2>/dev/null || true
    log "done"
    exit 0
fi

# ============================================================================
# Subcommand: start (the default)
# ============================================================================
# Validate
if [[ -z "$DEVICE_ID" ]]; then err "missing --device-id"; fi
if [[ -z "$API_URL" ]]; then err "missing --api-url"; fi
if [[ -z "$API_KEY" ]]; then err "missing --api-key"; fi
validate_device_id "$DEVICE_ID"
API_URL="${API_URL%/}"
log "device_id=$DEVICE_ID api_url=$API_URL local_port=$LOCAL_PORT proxy_port=$PROXY_PORT"

require_cmd curl python3 ssh

# ============================================================================
# Step 1: ensure the tunnel exists
# ============================================================================
# If we already have a fleet.json for this device AND the SSH process is
# alive, reuse it. Otherwise provision a new one.
TUNNEL_ALIVE=0
if [[ -f "${KLAN1_TUNNEL_HOME}/fleet.json" ]]; then
    FLEET_DEVICE=$(python3 -c "import json; print(json.load(open('${KLAN1_TUNNEL_HOME}/fleet.json')).get('device_id',''))" 2>/dev/null || echo "")
    FLEET_USER=$(python3 -c "import json; print(json.load(open('${KLAN1_TUNNEL_HOME}/fleet.json')).get('tunnel_user',''))" 2>/dev/null || echo "")
    if [[ "$FLEET_DEVICE" == "$DEVICE_ID" ]] && pgrep -f "ssh.*${FLEET_USER}@" >/dev/null 2>&1; then
        log "reusing existing tunnel for $DEVICE_ID (user=$FLEET_USER)"
        TUNNEL_ALIVE=1
    fi
fi

if [[ $TUNNEL_ALIVE -eq 0 ]]; then
    log "no live tunnel for $DEVICE_ID; provisioning"
    # Login
    LOGIN_BODY=$(printf '{"device_id":"%s","api_key":"%s"}' "$DEVICE_ID" "$API_KEY")
    LOGIN_RESP=$(curl -sS -X POST "$API_URL/api/v1/auth/login" \
        -H "Content-Type: application/json" \
        -d "$LOGIN_BODY")
    JWT=$(echo "$LOGIN_RESP" | python3 -c "import json,sys; print(json.load(sys.stdin).get('token',''))" 2>/dev/null)
    [[ -z "$JWT" ]] && err "login failed: $LOGIN_RESP"
    log "logged in, JWT len=${#JWT}"

    # Provision
    PROV_BODY=$(printf '{"local_port":%d}' "$LOCAL_PORT")
    PROV_RESP=$(curl -sS -X POST "$API_URL/api/v1/devices/$DEVICE_ID/provision" \
        -H "Content-Type: application/json" \
        -H "Authorization: Bearer $JWT" \
        -d "$PROV_BODY")
    PROV_ERR=$(echo "$PROV_RESP" | python3 -c "import json,sys; print(json.load(sys.stdin).get('error',''))" 2>/dev/null)
    if [[ -n "$PROV_ERR" ]]; then
        case "$PROV_ERR" in
            name_in_use)
                # The server still has a tunnel for this device_id. If we
                # have a token in fleet.json, release it and retry.
                if [[ -f "${KLAN1_TUNNEL_HOME}/fleet.json" ]]; then
                    OLD_TOKEN=$(python3 -c "import json; print(json.load(open('${KLAN1_TUNNEL_HOME}/fleet.json')).get('token',''))" 2>/dev/null)
                    if [[ -n "$OLD_TOKEN" ]]; then
                        log "server has $DEVICE_ID reserved; releasing existing tunnel (token=${OLD_TOKEN:0:8}...) and retrying"
                        curl -sS -X DELETE -H "Authorization: Bearer $JWT" \
                            "$API_URL/api/v1/tunnels/$OLD_TOKEN" >/dev/null 2>&1 || true
                        # Retry the provision
                        PROV_RESP=$(curl -sS -X POST "$API_URL/api/v1/devices/$DEVICE_ID/provision" \
                            -H "Content-Type: application/json" \
                            -H "Authorization: Bearer $JWT" \
                            -d "$PROV_BODY")
                        PROV_ERR=$(echo "$PROV_RESP" | python3 -c "import json,sys; print(json.load(sys.stdin).get('error',''))" 2>/dev/null)
                    fi
                fi
                if [[ -n "$PROV_ERR" ]]; then
                    err "Device '$DEVICE_ID' is reserved on the server and could not be released automatically.
Release it manually: DELETE $API_URL/api/v1/tunnels/<token> (admin auth required),
or wait for the server's sweeper to reap it after the TTL expires."
                fi
                ;;
            *) err "provision failed: $PROV_RESP" ;;
        esac
    fi

    # Parse bundle (same shlex pattern as install.sh)
    BUNDLE=$(echo "$PROV_RESP" | python3 -c '
import json, sys, shlex, base64
d = json.load(sys.stdin)
for k in ("device_id", "tunnel_user", "tunnel_port", "ssh_host",
         "ssh_user", "ssh_port", "private_key", "ssh_command",
         "fqdn", "token", "expires_at"):
    v = d.get(k, "")
    if k == "private_key":
        v = "BASE64:" + base64.b64encode(v.encode()).decode()
    print(f"{k}={shlex.quote(str(v))}")
')
    # shellcheck disable=SC2046
    eval $(echo "$BUNDLE" | sed 's/private_key=/private_key_b64=/')
    PRIVATE_KEY=$(echo "$private_key_b64" | sed 's/^BASE64://' | base64 -d)

    # Save fleet.json
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

    KEY_PATH="$KLAN1_TUNNEL_HOME/id_ed25519_$tunnel_user"
    printf '%s\n' "$PRIVATE_KEY" > "$KEY_PATH"
    chmod 600 "$KEY_PATH"
    log "saved: ${KLAN1_TUNNEL_HOME}/fleet.json + $KEY_PATH"
    log "provisioned: tunnel=$tunnel_user@$ssh_host:$ssh_port fqdn=$fqdn"

    # Start the SSH tunnel
    log "starting tunnel: $ssh_command"
    LOG_FILE="$KLAN1_TUNNEL_HOME/tunnel.log"
    nohup bash -c "$ssh_command" >"$LOG_FILE" 2>&1 &
    TUNNEL_PID=$!
    disown $TUNNEL_PID 2>/dev/null || true
    sleep 2
    if ! kill -0 "$TUNNEL_PID" 2>/dev/null; then
        log "WARN: tunnel process exited within 2s. Check $LOG_FILE."
    else
        log "tunnel PID: $TUNNEL_PID"
    fi
fi

# Read fleet.json (whether just created or pre-existing) for the URL prints
FQDN=$(python3 -c "import json; print(json.load(open('${KLAN1_TUNNEL_HOME}/fleet.json')).get('fqdn',''))" 2>/dev/null || echo "")
BASE_DOMAIN=$(python3 -c "import json; print(json.load(open('${KLAN1_TUNNEL_HOME}/fleet.json')).get('base_domain',''))" 2>/dev/null || echo "")

# ============================================================================
# Step 2: ensure pproxy is installed
# ============================================================================
if ! command -v pproxy >/dev/null 2>&1; then
    log "pproxy not found, installing via pip ($PPROXY_INSTALL_CMD)"
    if ! $PPROXY_INSTALL_CMD 2>/dev/null; then
        err "failed to install pproxy. Install it manually: pip3 install --user pproxy"
    fi
    # Re-check (the install may have put it in ~/.local/bin which isn't on PATH yet)
    if ! command -v pproxy >/dev/null 2>&1; then
        # Try the user bin dir explicitly
        for p in "$HOME/.local/bin/pproxy" "$HOME/Library/Python/*/bin/pproxy"; do
            if [[ -x "$p" ]]; then
                export PATH="$(dirname "$p"):$PATH"
                break
            fi
        done
    fi
    command -v pproxy >/dev/null 2>&1 || err "pproxy still not found after install"
    log "pproxy installed: $(command -v pproxy)"
fi

# ============================================================================
# Step 3: launch pproxy (unless --no-launch)
# ============================================================================
if [[ -n "$NO_LAUNCH" ]]; then
    log "skipping pproxy launch (--no-launch)"
else
    # Idempotency: if pproxy is already listening on PROXY_PORT, no-op
    if pgrep -f "pproxy.*-l.*:${PROXY_PORT}" >/dev/null 2>&1; then
        log "pproxy already running on port $PROXY_PORT"
    else
        # Kill any stale pproxy on this port
        fuser -k "${PROXY_PORT}/tcp" >/dev/null 2>&1 || true
        sleep 0.3
        log "starting pproxy on 127.0.0.1:${PROXY_PORT} (http+socks5)"
        LOG_FILE="$KLAN1_TUNNEL_HOME/pproxy.log"
        # `http+socks5://...` makes ONE port serve both HTTP CONNECT and SOCKS5.
        # The tunnel exposes 127.0.0.1:LOCAL_PORT on this machine (because of
        # -R <remote_port>:127.0.0.1:LOCAL_PORT), and Caddy reverse-proxies
        # the public URL to 127.0.0.1:<remote_port> on the server. So pproxy
        # binds here on LOCAL_PORT.
        nohup pproxy -l "http+socks5://127.0.0.1:${PROXY_PORT}" \
            >"$LOG_FILE" 2>&1 &
        PPROXY_PID=$!
        echo $PPROXY_PID > "${KLAN1_TUNNEL_HOME}/pproxy.pid"
        disown $PPROXY_PID 2>/dev/null || true
        sleep 0.5
        if ! kill -0 "$PPROXY_PID" 2>/dev/null; then
            log "WARN: pproxy exited within 0.5s. Check $LOG_FILE."
        else
            log "pproxy PID: $PPROXY_PID (log: $LOG_FILE)"
        fi
    fi
fi

# ============================================================================
# Step 4: ensure heartbeat daemon is running
# ============================================================================
HEARTBEAT_PID_FILE="${KLAN1_TUNNEL_HOME}/heartbeat.pid"
if [[ -f "$HEARTBEAT_PID_FILE" ]] && kill -0 "$(cat "$HEARTBEAT_PID_FILE" 2>/dev/null)" 2>/dev/null; then
    log "heartbeat already running (pid $(cat "$HEARTBEAT_PID_FILE"))"
else
    # Need a fresh JWT to start the heartbeat (the previous one may have expired)
    LOGIN_BODY=$(printf '{"device_id":"%s","api_key":"%s"}' "$DEVICE_ID" "$API_KEY")
    LOGIN_RESP=$(curl -sS -X POST "$API_URL/api/v1/auth/login" \
        -H "Content-Type: application/json" -d "$LOGIN_BODY")
    JWT=$(echo "$LOGIN_RESP" | python3 -c "import json,sys; print(json.load(sys.stdin).get('token',''))" 2>/dev/null)
    TOKEN=$(python3 -c "import json; print(json.load(open('${KLAN1_TUNNEL_HOME}/fleet.json')).get('token',''))" 2>/dev/null)
    if [[ -n "$JWT" && -n "$TOKEN" ]]; then
        HEARTBEAT_LOG="$KLAN1_TUNNEL_HOME/heartbeat.log"
        nohup bash -c "
set -u
trap 'exit 0' TERM INT
JWT='$JWT'
TOKEN='$TOKEN'
API='$API_URL'
while true; do
  curl -sS -X POST \"\$API/api/v1/tunnels/\$TOKEN/heartbeat\" \\
      -H \"Authorization: Bearer \$JWT\" \\
      -o /dev/null -w '%{http_code}\\n' >> '$HEARTBEAT_LOG' 2>&1
  sleep $HEARTBEAT_INTERVAL
done
" >/dev/null 2>&1 &
        HB_PID=$!
        echo $HB_PID > "$HEARTBEAT_PID_FILE"
        disown $HB_PID 2>/dev/null || true
        log "heartbeat PID: $HB_PID (every ${HEARTBEAT_INTERVAL}s)"
    else
        log "WARN: could not start heartbeat (login/token missing)"
    fi
fi

# ============================================================================
# Step 5: print URLs
# ============================================================================
echo
echo "==== klan1-proxy running ===="
echo "device_id:    $DEVICE_ID"
echo "tunnel user:  $(python3 -c "import json; print(json.load(open('${KLAN1_TUNNEL_HOME}/fleet.json')).get('tunnel_user',''))" 2>/dev/null)"
echo "local port:   $LOCAL_PORT (pproxy listens here)"
echo "public fqdn:  $FQDN"
echo
echo "=== Proxy URLs (use these in your apps) ==="
echo
echo "Local (from this machine):"
echo "  SOCKS5:  socks5://127.0.0.1:${PROXY_PORT}"
echo "  HTTP:    http://127.0.0.1:${PROXY_PORT}"
echo
echo "Public (via the tunnel — works from any device, anywhere):"
echo "  SOCKS5:  socks5://${FQDN}:443   (use --socks5-hostname or proxies that support TLS+SOCKS)"
echo "  HTTP:    https://${FQDN}:443   (curl -x https://${FQDN}:443 http://target/)"
echo
echo "=== Test ==="
echo
echo "Local test:"
echo "  curl -x http://127.0.0.1:${PROXY_PORT} http://api.ipify.org/"
echo "  curl --socks5 127.0.0.1:${PROXY_PORT} https://api.ipify.org/"
echo
echo "Public URL test (waits 1-3 min for Caddy cert on first run):"
echo "  curl -x https://${FQDN}:443 http://api.ipify.org/"
echo
echo "=== Management ==="
echo "  ${0##*/} status   # show state"
echo "  ${0##*/} stop     # kill pproxy + tunnel + heartbeat (does NOT release server-side tunnel)"
