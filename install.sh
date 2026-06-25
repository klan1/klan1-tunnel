#!/usr/bin/env bash
# klan1-tunnel installer - one-liner setup for new devices
#
# Usage (curl | bash):
#   curl -sSL https://raw.githubusercontent.com/klan1/klan1-tunnel/main/install.sh | \
#       bash -s -- --name macbook --subdomain 1
#
# Usage (local):
#   ./install.sh --name macbook --subdomain 1 [--no-start] [--local-port 8080]
#
# What it does:
#   1. Reads your fleet config from ~/.klan1-tunnel/fleet.json or the --fleet flag.
#      (See config/fleet.example.json for the schema.)
#   2. Asks the API for a JWT (device_id=name).
#   3. Calls /api/v1/tunnels with the JWT; gets back a per-tunnel private key.
#   4. Installs deps (autossh, klan1-pproxy).
#   5. Downloads the client to <prefix>/bin/klan1-tunnel-client.
#   6. Runs the client with --name and --local-port.
#
# Requirements: macOS or Linux, network access to api.<base_domain>, Python 3.9+.
# The device_id must already be on the API whitelist (managed from the dashboard).

set -uo pipefail

# ============================================================================
# Defaults
# ============================================================================
KLAN1_REPO_RAW="https://raw.githubusercontent.com/klan1/klan1-tunnel/main"
KLAN1_CLIENT_BIN="${KLAN1_CLIENT_BIN:-$HOME/.local/bin/klan1-tunnel-client}"
KLAN1_TUNNEL_HOME="${KLAN1_TUNNEL_HOME:-$HOME/.klan1-tunnel}"
PREFIX="${PREFIX:-$HOME/.local}"

AI1_HOST=""
AI1_PORT=""
AI1_USER=""
API_URL=""
FLEET_CONFIG_PATH=""

# Default subdomain -> port mapping (overridden by fleet config)
SUBDOMAIN_PORTS_DEFAULT=(65081 65082 65083 65084 65085 65086 65087 65088 65089 65090)
SUBDOMAIN_PORTS=(65081 65082 65083 65084 65085 65086 65087 65088 65089 65090)
SUBDOMAIN_NAMES=(1 2 3 4 5 6 7 8 9 10)
BASE_DOMAIN="tunnels.<your-domain>"

NAME=""
SUBDOMAIN=""
LOCAL_PORT="8080"
NO_START=0
SKIP_DEPS=0

# ============================================================================
# Logging
# ============================================================================
log()  { echo "[klan1-tunnel-install] $*" >&2; }
die()  { log "ERROR: $*"; exit 1; }
ask()  {
    local var="$1" prompt="$2" default="${3:-}"
    local reply
    if [[ -n "$default" ]]; then
        read -r -p "$(printf '%s [%s]: ' "$prompt" "$default")" reply
        reply="${reply:-$default}"
    else
        read -r -p "$(printf '%s: ' "$prompt")" reply
    fi
    printf -v "$var" '%s' "$reply"
}

# ============================================================================
# Fleet config loading
# ============================================================================
load_fleet_config() {
    # Search order: --fleet flag, then standard paths
    local candidates=()
    if [[ -n "$FLEET_CONFIG_PATH" ]]; then
        candidates+=("$FLEET_CONFIG_PATH")
    fi
    candidates+=(
        "$HOME/.klan1-tunnel/fleet.json"
        "$HOME/.config/klan1-tunnel/fleet.json"
        "/etc/klan1-tunnel/fleet.json"
    )

    local config_file=""
    for path in "${candidates[@]}"; do
        if [[ -f "$path" ]]; then
            config_file="$path"
            break
        fi
    done

    if [[ -z "$config_file" ]]; then
        log "no fleet config found (looked in: ${candidates[*]})"
        log "  --> defaults will be used; pass --api-url to override"
        return 0
    fi

    log "loading fleet config from $config_file"

    # Parse JSON via Python (always available on macOS / Linux)
    if ! command -v python3 >/dev/null; then
        log "python3 not found; cannot parse fleet config"
        return 0
    fi

    local parsed
    if ! parsed="$(python3 - "$config_file" <<'PYEOF'
import json, sys
try:
    with open(sys.argv[1]) as f:
        cfg = json.load(f)
except Exception as e:
    print(f"PARSE_ERROR:{e}", file=sys.stderr)
    sys.exit(1)

# Primary server (server_order[0]) is what we use.
# Use a generic placeholder; the real alias comes from fleet.json.
order = cfg.get("server_order") or ["primary"]
servers = cfg.get("servers") or {}
primary = order[0]
srv = servers.get(primary, {})

if srv.get("host"):
    print(f"AI1_HOST={srv['host']}")
if srv.get("user"):
    print(f"AI1_USER={srv['user']}")
if srv.get("port"):
    print(f"AI1_PORT={srv['port']}")

api = cfg.get("api") or {}
if api.get("url"):
    print(f"API_URL={api['url']}")

# Subdomain port overrides
subs = cfg.get("subdomains") or {}
if subs:
    ports = []
    for i in range(1, 11):
        key = str(i)
        if key in subs:
            ports.append(subs[key])
        else:
            # Fall back to default mapping (65080 + i)
            ports.append(65080 + i)
    print("SUBDOMAIN_PORTS=" + " ".join(str(p) for p in ports))

if cfg.get("base_domain"):
    print(f"BASE_DOMAIN={cfg['base_domain']}")
PYEOF
)"; then
        log "fleet config parse failed (exit=$?): $parsed"
        return 1
    fi

    # Apply parsed values (only if not already set via CLI/env)
    while IFS='=' read -r key value; do
        case "$key" in
            AI1_HOST)    [[ -z "$AI1_HOST"    ]] && AI1_HOST="$value" ;;
            AI1_USER)    [[ -z "$AI1_USER"    ]] && AI1_USER="$value" ;;
            AI1_PORT)    [[ -z "$AI1_PORT"    ]] && AI1_PORT="$value" ;;
            API_URL)     [[ -z "$API_URL"     ]] && API_URL="$value" ;;
            SUBDOMAIN_PORTS)
                if [[ -n "$value" ]]; then
                    # shellcheck disable=SC2206
                    SUBDOMAIN_PORTS=( $value )
                fi
                ;;
            BASE_DOMAIN) BASE_DOMAIN="$value" ;;
        esac
    done <<< "$parsed"

    # Construct API_URL from base_domain if not set
    if [[ -z "$API_URL" && -n "$BASE_DOMAIN" ]]; then
        API_URL="https://api.${BASE_DOMAIN}"
        log "derived API_URL=$API_URL from base_domain"
    fi

    log "fleet loaded: ai1=${AI1_USER}@${AI1_HOST}:${AI1_PORT} api=$API_URL"
}

# ============================================================================
# Argument parsing
# ============================================================================
parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --name)        NAME="$2"; shift 2 ;;
            --subdomain)   SUBDOMAIN="$2"; shift 2 ;;
            --local-port)  LOCAL_PORT="$2"; shift 2 ;;
            --api-url)     API_URL="$2"; shift 2 ;;
            --fleet)       FLEET_CONFIG_PATH="$2"; shift 2 ;;
            --prefix)      PREFIX="$2"; KLAN1_CLIENT_BIN="$PREFIX/bin/klan1-tunnel-client"; shift 2 ;;
            --no-start)    NO_START=1; shift ;;
            --skip-deps)   SKIP_DEPS=1; shift ;;
            -h|--help)
                sed -n '2,18p' "$0" | sed 's/^# *//'
                exit 0
                ;;
            *) die "unknown argument: $1" ;;
        esac
    done

    # Sensible default for device name
    if [[ -z "$NAME" ]]; then
        NAME="$(hostname -s 2>/dev/null || hostname || echo device)"
    fi

    # Validate subdomain
    if [[ -z "$SUBDOMAIN" ]]; then
        die "missing --subdomain N (where N is 1..10; picks a tunnel port)"
    fi
    case "$SUBDOMAIN" in
        1|2|3|4|5|6|7|8|9|10) ;;
        *) die "--subdomain must be one of: ${SUBDOMAIN_NAMES[*]}" ;;
    esac
    PORT="${SUBDOMAIN_PORTS[$((SUBDOMAIN-1))]}"
    SUBDOMAIN_FQDN="${SUBDOMAIN}.${BASE_DOMAIN}"
    TUNNEL_USER="tunnel-${PORT}"
}

# ============================================================================
# Platform detection
# ============================================================================
detect_platform() {
    OS="$(uname -s)"
    case "$OS" in
        Darwin) PLATFORM=macos ;;
        Linux)  PLATFORM=linux ;;
        *)      die "unsupported OS: $OS" ;;
    esac
}

# ============================================================================
# Install prerequisites
# ============================================================================
install_deps_macos() {
    command -v brew >/dev/null || die "Homebrew is required on macOS. Install from https://brew.sh"
    command -v autossh >/dev/null || brew install autossh

    local py; py="$(command -v python3)"
    [[ -x "$py" ]] || die "python3 not found in PATH"

    if ! "$py" -c "import pproxy" 2>/dev/null; then
        log "installing klan1-pproxy from PyPI"
        "$py" -m pip install --user "klan1-pproxy>=3.0.3" \
            || die "pip install failed; try: $py -m pip install --user --break-system-packages 'klan1-pproxy>=3.0.3'"
    fi
}

install_deps_linux() {
    if command -v apt >/dev/null; then
        sudo apt update
        sudo apt install -y autossh python3-pip openssh-client
    elif command -v dnf >/dev/null; then
        sudo dnf install -y autossh python3-pip openssh-clients
    elif command -v yum >/dev/null; then
        sudo yum install -y autossh python3-pip openssh-clients
    elif command -v apk >/dev/null; then
        sudo apk add --no-cache autossh py3-pip openssh-client
    else
        die "no supported package manager (need apt/dnf/yum/apk)"
    fi
    local py; py="$(command -v python3)"
    if ! "$py" -c "import pproxy" 2>/dev/null; then
        log "installing klan1-pproxy into user venv"
        "$py" -m venv "${KLAN1_TUNNEL_HOME}/venv"
        "${KLAN1_TUNNEL_HOME}/venv/bin/pip" install --upgrade pip >/dev/null 2>&1
        "${KLAN1_TUNNEL_HOME}/venv/bin/pip" install "klan1-pproxy>=3.0.3"
    fi
}

# ============================================================================
# Install the client script
# ============================================================================
install_client() {
    mkdir -p "$(dirname "$KLAN1_CLIENT_BIN")"

    local src=""
    if command -v curl >/dev/null; then
        if src="$(curl -sSL --max-time 15 "$KLAN1_REPO_RAW/client/klan1-tunnel-client.sh" 2>/dev/null)" \
           && [[ -n "$src" && ${#src} -gt 1000 ]]; then
            log "downloaded client from $KLAN1_REPO_RAW"
        else
            src=""
        fi
    fi

    if [[ -z "$src" ]]; then
        die "could not download the client script. Check network or run from a clone."
    fi

    echo "$src" > "$KLAN1_CLIENT_BIN"
    chmod +x "$KLAN1_CLIENT_BIN"
    log "installed to $KLAN1_CLIENT_BIN"

    # Ensure <prefix>/bin is in PATH for future sessions
    if ! echo "$PATH" | tr ':' '\n' | grep -qx "$PREFIX/bin"; then
        log "NOTE: $PREFIX/bin is not in your PATH. Add it to your shell rc:"
        log "  echo 'export PATH=\$PATH:$PREFIX/bin' >> ~/.zshrc"
    fi
}

# ============================================================================
# Auto-fetch the tunnel key from the API
# ============================================================================
fetch_tunnel_key() {
    local keyfile="${KLAN1_TUNNEL_HOME}/id_ed25519_${TUNNEL_USER}"
    local tokenfile="${KLAN1_TUNNEL_HOME}/token"

    # Idempotent: skip if key already exists
    if [[ -f "$keyfile" ]]; then
        log "tunnel key already present: $keyfile"
        return 0
    fi

    mkdir -p "$KLAN1_TUNNEL_HOME"
    chmod 700 "$KLAN1_TUNNEL_HOME"

    log "registering device $NAME with $API_URL..."
    # 1. Login: get JWT
    local jwt
    if ! jwt="$(curl -sSL --max-time 30 -X POST "$API_URL/api/v1/auth/login" \
            -H "Content-Type: application/json" \
            -d "{\"device_id\": \"$NAME\"}" 2>/dev/null)"; then
        die "could not reach API at $API_URL (network?)"
    fi

    # Check error responses
    if echo "$jwt" | grep -q '"error"'; then
        die "API rejected device $NAME: $jwt"
    fi

    # Extract JWT token (Python is the safest path; jq is too, but may not exist)
    local token
    token="$(printf '%s' "$jwt" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("token",""))' 2>/dev/null)"
    if [[ -z "$token" ]]; then
        die "could not extract JWT from API response: $jwt"
    fi
    log "got JWT (length=${#token})"

    # 2. Request a tunnel: gets back the private key + SSH command
    log "requesting tunnel for subdomain $SUBDOMAIN (port $PORT)..."
    local resp
    resp="$(curl -sSL --max-time 30 -X POST "$API_URL/api/v1/tunnels" \
        -H "Authorization: Bearer $token" \
        -H "Content-Type: application/json" \
        -d "{\"name\": \"$NAME\", \"subdomain\": \"$SUBDOMAIN\", \"local_port\": $LOCAL_PORT}" 2>/dev/null)"

    if echo "$resp" | grep -q '"error"'; then
        die "tunnel request failed: $resp"
    fi

    # Extract the private key from the response
    local private_key
    private_key="$(printf '%s' "$resp" | python3 -c '
import sys, json
data = json.load(sys.stdin)
print(data.get("private_key", ""))
' 2>/dev/null)"

    if [[ -z "$private_key" ]]; then
        die "API did not return a private key. Response: $resp"
    fi

    # Save the key
    printf '%s\n' "$private_key" > "$keyfile"
    chmod 600 "$keyfile"
    log "tunnel key saved to $keyfile"

    # Save the JWT for later heartbeats
    printf '%s' "$token" > "$tokenfile"
    chmod 600 "$tokenfile"
    log "JWT saved to $tokenfile"
}

# ============================================================================
# Start the tunnel
# ============================================================================
start_tunnel() {
    log "starting tunnel name=$NAME subdomain=$SUBDOMAIN port=$LOCAL_PORT -> $SUBDOMAIN_FQDN"
    if ! "$KLAN1_CLIENT_BIN" --name "$NAME" --local-port "$LOCAL_PORT"; then
        die "tunnel start failed. Check ~/.klan1-tunnel/${NAME}.log"
    fi
}

# ============================================================================
# Main
# ============================================================================
parse_args "$@"
detect_platform
load_fleet_config

log "platform:  $PLATFORM"
log "name:      $NAME"
log "subdomain: $SUBDOMAIN_FQDN (port $PORT, user $TUNNEL_USER)"
log "local:     127.0.0.1:$LOCAL_PORT"
log "prefix:    $PREFIX"

if [[ $SKIP_DEPS -eq 0 ]]; then
    log "installing prerequisites..."
    case "$PLATFORM" in
        macos) install_deps_macos ;;
        linux) install_deps_linux ;;
    esac
fi

install_client

fetch_tunnel_key

if [[ $NO_START -eq 0 ]]; then
    start_tunnel
    log ""
    log "tunnel '$NAME' is now active. Test it with:"
    log "  curl https://${SUBDOMAIN_FQDN}/"
    log "stop with:"
    log "  $KLAN1_CLIENT_BIN --name $NAME --disconnect"
else
    log "installed. Start later with: $KLAN1_CLIENT_BIN --name $NAME --local-port $LOCAL_PORT"
fi