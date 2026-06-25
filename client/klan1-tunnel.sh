#!/usr/bin/env bash
# klan1-tunnel.sh - Self-hosted ngrok-like tunnel
#
# Usage:
#   ./klan1-tunnel.sh start --name mac --server primary
#   ./klan1-tunnel.sh stop --name mac
#   ./klan1-tunnel.sh status
#
# See --help for full options.

set -uo pipefail

# ============================================================================
# Defaults
# ============================================================================
KLAN1_TUNNEL_HOME="${KLAN1_TUNNEL_HOME:-$HOME/.klan1-tunnel}"
KLAN1_TUNNEL_CONFIG="${KLAN1_TUNNEL_CONFIG:-$KLAN1_TUNNEL_HOME/config}"
KLAN1_TUNNEL_LOG="${KLAN1_TUNNEL_LOG:-$KLAN1_TUNNEL_HOME/tunnel.log}"

# Fleet configuration: read from fleet.json if present, otherwise use generic placeholders.
# fleet.json format:
#   { "servers": { "<alias>": {"user":"...","host":"...","port":22},
#                  ... },
#     "api_url": "https://api.example.com",
#     "subdomain_base": "tunnels",
#     "server_order": ["primary","fallback",...] }
FLEET_LOCAL="$KLAN1_TUNNEL_HOME/fleet.local.json"
FLEET_COMMITTED="$KLAN1_TUNNEL_HOME/fleet.json"
FLEET_EXAMPLE="$KLAN1_TUNNEL_HOME/fleet.example.json"

FLEET_FILE=""
[[ -f "$FLEET_LOCAL"     ]] && FLEET_FILE="$FLEET_LOCAL"
[[ -z "$FLEET_FILE" && -f "$FLEET_COMMITTED" ]] && FLEET_FILE="$FLEET_COMMITTED"
[[ -z "$FLEET_FILE" && -f "$FLEET_EXAMPLE"   ]] && FLEET_FILE="$FLEET_EXAMPLE"

# Generic placeholders — used when no fleet.json is found.
# Override by placing fleet.json in $KLAN1_TUNNEL_HOME (see config/fleet.example.json).
KLAN1_DEFAULT_SERVER="root@<your-server-host>:<your-ssh-port>"
KLAN1_DEFAULT_API_URL="https://<your-api-host>"
KLAN1_DEFAULT_SUBDOMAIN_BASE="tunnels"
KLAN1_DEFAULT_SERVER_ORDER="primary fallback"

# Parse a single fleet.json value with a python one-liner (avoid jq dependency).
# Usage: fleet_get "<key.path>"
fleet_get() {
    [[ -z "$FLEET_FILE" || ! -f "$FLEET_FILE" ]] && return 1
    python3 -c "import json,sys
d=json.load(open(sys.argv[1]))
for k in sys.argv[2].split('.'):
    d=d.get(k,{}) if isinstance(d,dict) else {}
print(d if isinstance(d,(str,int)) else '')" "$FLEET_FILE" "$1" 2>/dev/null
}

KLAN1_API_URL="${KLAN1_API_URL:-$(fleet_get api_url)}"
[[ -z "$KLAN1_API_URL" ]] && KLAN1_API_URL="$KLAN1_DEFAULT_API_URL"

SUBDOMAIN_BASE="${SUBDOMAIN_BASE:-$(fleet_get subdomain_base)}"
[[ -z "$SUBDOMAIN_BASE" ]] && SUBDOMAIN_BASE="$KLAN1_DEFAULT_SUBDOMAIN_BASE"

# Server aliases resolved from fleet.json servers.<alias> = "user@host:port"
_fleet_resolve_server() {
    local alias="$1"
    local user host port
    user="$(fleet_get "servers.$alias.user")"
    host="$(fleet_get "servers.$alias.host")"
    port="$(fleet_get "servers.$alias.port")"
    [[ -z "$user" || -z "$host" ]] && return 1
    [[ -z "$port" ]] && port=22
    printf '%s@%s:%s' "$user" "$host" "$port"
}

# Default to first server in fleet.json server_order, or the placeholder.
KLAN1_SERVER_ORDER_RAW="$(fleet_get server_order)"
if [[ -n "$KLAN1_SERVER_ORDER_RAW" ]]; then
    # JSON array comes as '['primary', 'fallback']' — strip brackets/quotes.
    KLAN1_SERVER_ORDER="$(printf '%s' "$KLAN1_SERVER_ORDER_RAW" \
        | tr -d '[]" ' | tr ',' ' ')"
else
    KLAN1_SERVER_ORDER="$KLAN1_DEFAULT_SERVER_ORDER"
fi

klan1_spec() {
    local alias="$1"
    local spec
    spec="$(_fleet_resolve_server "$alias" 2>/dev/null)"
    if [[ -n "$spec" ]]; then
        printf '%s' "$spec"
        return 0
    fi
    # No fleet.json match — fall back to the generic placeholder so the
    # command still prints something useful for `status`/`stop`.
    printf '%s' "$KLAN1_DEFAULT_SERVER"
    return 0
}

PROXY_PROTOCOL="http"
GATEWAY="klan1"
TTL=86400
ACTION="start"

# ============================================================================
# Logging
# ============================================================================
log_init() {
    mkdir -p "$KLAN1_TUNNEL_HOME"
    : >> "$KLAN1_TUNNEL_LOG"
}
log() {
    local ts; ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "[$ts] $*" | tee -a "$KLAN1_TUNNEL_LOG" >&2
}
die() { log "ERROR: $*"; exit "${2:-1}"; }

# ============================================================================
# Usage
# ============================================================================
usage() {
    cat <<'USAGE'
klan1-tunnel.sh - self-hosted ngrok-like tunnel

Actions:
  start                  open a tunnel (default)
  stop --name NAME       stop a specific tunnel
  status                 list active tunnels

Required for start:
  --name NAME            tunnel name (e.g. mac, iphone, chromebook)

Optional for start:
  --port PORT            local proxy port (default: same as remote port)
  --server ALIAS         target server (default: ws1, fallback: db2, db1, ai1)
  --remote-port PORT     explicit remote port (default: 65082)
  --protocol http|socks5 proxy protocol (default: http)
  --no-proxy             tunnel raw TCP (expose a local web server)
  --local-target H:P     target when --no-proxy (default: 127.0.0.1:80)
  --ttl SECONDS          TTL (default: 86400)
  --api-url URL          register with the klan1-tunnel-server API
  --ssh-alias ALIAS      override SSH alias used (rare)
  --key PATH             override SSH key
  --egress-ip IP         override the detected egress IP

Examples:
  klan1-tunnel.sh start --name mac --server primary
  klan1-tunnel.sh start --name iphone --server primary --no-proxy --local-target 127.0.0.1:8080
  klan1-tunnel.sh stop --name mac
USAGE
}

# ============================================================================
# Argument parsing
# ============================================================================
parse_args() {
    NAME=""
    PORT=""
    REMOTE_PORT=""
    SERVER_ARG=""
    EGRESS_IP_OVERRIDE=""
    NO_PROXY=0
    LOCAL_TARGET="127.0.0.1:80"
    SSH_ALIAS=""
    KEY_PATH=""
    SERVER_API_URL=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            start|stop|status|list)  ACTION="$1"; shift ;;
            --name)         NAME="$2"; shift 2 ;;
            --port)         PORT="$2"; shift 2 ;;
            --server)       SERVER_ARG="$2"; shift 2 ;;
            --api-url)      SERVER_API_URL="$2"; shift 2 ;;
            --remote-port)  REMOTE_PORT="$2"; shift 2 ;;
            --egress-ip)    EGRESS_IP_OVERRIDE="$2"; shift 2 ;;
            --protocol)     PROXY_PROTOCOL="$2"; shift 2 ;;
            --ttl)          TTL="$2"; shift 2 ;;
            --no-proxy)     NO_PROXY=1; shift ;;
            --local-target) LOCAL_TARGET="$2"; shift 2 ;;
            --ssh-alias)    SSH_ALIAS="$2"; shift 2 ;;
            --key)          KEY_PATH="$2"; shift 2 ;;
            --gateway)      GATEWAY="$2"; shift 2 ;;
            -h|--help)      usage; exit 0 ;;
            *)              log "Unknown arg: $1"; usage; exit 1 ;;
        esac
    done

    if [[ "$ACTION" == "start" && -z "$NAME" ]]; then
        NAME="$(hostname -s 2>/dev/null || hostname)"
    fi
}

# ============================================================================
# Config persistence
# ============================================================================
config_save() {
    local name="$1" server="$2" port="$3" rport="$4" proto="$5" noproxy="$6" ltarget="$7" started_at="$8" ttl="$9"
    touch "$KLAN1_TUNNEL_CONFIG"
    local tmp; tmp="$(mktemp)"
    grep -v "^${name}|" "$KLAN1_TUNNEL_CONFIG" > "$tmp" 2>/dev/null || true
    mv "$tmp" "$KLAN1_TUNNEL_CONFIG"
    echo "${name}|${server}|${port}|${rport}|${proto}|${noproxy}|${ltarget}|${started_at}|${ttl}" >> "$KLAN1_TUNNEL_CONFIG"
}
config_load() {
    local name="$1"
    grep "^${name}|" "$KLAN1_TUNNEL_CONFIG" 2>/dev/null | head -1
}
config_list() { cat "$KLAN1_TUNNEL_CONFIG" 2>/dev/null; }

# ============================================================================
# Detect / validate local environment
# ============================================================================
detect_local() {
    PPROXY_BIN=""
    AUTOSSH_BIN=""
    PYTHON_BIN=""

    if command -v autossh >/dev/null 2>&1; then
        AUTOSSH_BIN="$(command -v autossh)"
    fi

    # Resolve pproxy. Order of preference:
    #   1. A real binary in PATH whose shebang points to a working python
    #   2. `python3 -m pproxy` (always works once pproxy is pip-installed)
    # We deliberately skip pyenv shims: they're bash wrappers that pick
    # whatever Python pyenv considers global, which may be incompatible
    # with the locally installed pproxy.
    if command -v pproxy >/dev/null 2>&1; then
        local pproxy_path shebang real_python
        pproxy_path="$(command -v pproxy)"
        shebang="$(head -1 "$pproxy_path" 2>/dev/null)"
        if [[ "$shebang" == "#!/usr/bin/env bash" ]]; then
            # Pyenv shim (or any other bash wrapper). Don't use it directly;
            # fall through to python3 -m pproxy below.
            :
        elif [[ "$shebang" == "#!"* ]]; then
            real_python="${shebang#\#!}"
            if [[ -x "$real_python" ]]; then
                PPROXY_BIN="$pproxy_path"
                PYTHON_BIN="$real_python"
            fi
            # If shebang interpreter isn't executable, fall through to -m
        fi
    fi

    # Fallback / preferred path: invoke pproxy as a Python module with the
    # system python3. This is the only reliable approach once the pproxy
    # fork on klan1/pproxy@v3.0.0 is pip-installed into a regular
    # Python 3.9+ environment.
    if [[ -z "$PPROXY_BIN" ]]; then
        local py
        py="$(command -v python3)"
        if [[ -x "$py" ]] && "$py" -c "import pproxy" >/dev/null 2>&1; then
            PPROXY_BIN="${py}-m-pproxy"   # sentinel for the launcher below
            PYTHON_BIN="$py"
        fi
    fi

    if [[ -z "$AUTOSSH_BIN" ]]; then
        AUTOSSH_BIN="$(command -v ssh)"
    fi
    [[ -n "$AUTOSSH_BIN" ]] || die "neither autossh nor ssh found"
    [[ -n "$PPROXY_BIN" ]]  || die "pproxy not installed; run: python3 -m pip install --user 'pproxy @ git+https://github.com/klan1/pproxy.git@v3.0.0'"
}

# ============================================================================
# Server selection
# ============================================================================
server_resolve() {
    if [[ -n "$SERVER_ARG" ]]; then
        if [[ "$SERVER_ARG" =~ ^https?:// ]]; then
            local url_host alias spec host
            url_host="$(printf '%s' "$SERVER_ARG" | sed -E 's|^https?://||; s|/.*$||; s|:.*$||')"
            for alias in $KLAN1_SERVER_ORDER; do
                spec="$(klan1_spec "$alias" 2>/dev/null)" || continue
                host="${spec#*@}"; host="${host%:*}"
                if [[ "$host" == "$url_host" ]]; then
                    SERVER_ALIAS="$alias"
                    _fill_server_vars
                    return 0
                fi
            done
            SERVER_ALIAS="primary"
            _fill_server_vars
            return 0
        else
            SERVER_ALIAS="$SERVER_ARG"
            if ! klan1_spec "$SERVER_ALIAS" >/dev/null 2>&1; then
                die "unknown server alias: $SERVER_ALIAS (known: $KLAN1_SERVER_ORDER)"
            fi
            _fill_server_vars
            return 0
        fi
    fi

    local alias spec user_host port host user key
    for alias in $KLAN1_SERVER_ORDER; do
        spec="$(klan1_spec "$alias" 2>/dev/null)" || continue
        user_host="${spec%:*}"
        port="${spec##*:}"
        host="${user_host#*@}"
        user="${user_host%@*}"
        key="${KEY_PATH:-$HOME/.ssh/id_ed25519_$alias}"
        if ssh -o BatchMode=yes -o ConnectTimeout=4 -o StrictHostKeyChecking=accept-new \
               -o Port="$port" -o User="$user" -o IdentityFile="$key" \
               -o IdentitiesOnly=yes \
               "$host" 'echo ok' >/dev/null 2>&1; then
            SERVER_ALIAS="$alias"
            _fill_server_vars
            log "auto-selected server: $alias (${user_host}:${port})"
            return 0
        fi
    done
    return 1
}

_fill_server_vars() {
    local spec user_host
    spec="$(klan1_spec "$SERVER_ALIAS" 2>/dev/null)" || { SERVER_ALIAS=""; return 1; }
    user_host="${spec%:*}"
    SERVER_PORT="${spec##*:}"
    SERVER_HOST="${user_host#*@}"
    SERVER_USER="${user_host%@*}"
    SERVER_KEY="${KEY_PATH:-$HOME/.ssh/id_ed25519_$SERVER_ALIAS}"
}

# ============================================================================
# API server interaction (optional)
# ============================================================================
api_call() {
    [[ -z "${SERVER_API_URL:-}" ]] && return 1
    local method="$1" path="$2" data="${3:-}"
    local args=(-sS --max-time 10 -X "$method" -H 'Content-Type: application/json')
    [[ -n "$data" ]] && args+=(-d "$data")
    curl "${args[@]}" "${SERVER_API_URL%/}$path"
}

# Parse JSON response into shell variables. Returns 0 if ok=true with a port.
api_register() {
    local name="$1" rport="$2" proto="$3" ttl="$4"
    local payload
    payload=$(printf '{"name":"%s","remote_port":%s,"protocol":"%s","ttl":%s,"server_alias":"%s","egress_ip":"%s"}' \
        "$name" "${rport:-null}" "$proto" "$ttl" "$SERVER_ALIAS" "${EGRESS_IP:-unknown}")
    local resp
    resp="$(api_call POST /api/v1/tunnels "$payload" 2>/dev/null)" || return 1
    log "server response: $resp"

    # Parse JSON with python3 (avoids sed/grep quirks with multiline responses)
    local parsed
    parsed=$(printf '%s' "$resp" | python3 -c '
import json, sys
try:
    d = json.loads(sys.stdin.read())
    ok = d.get("ok", False)
    port = d.get("remote_port") or ""
    token = d.get("token") or ""
    name_in_use = d.get("error") == "name_in_use"
    existing_port = ""
    existing_token = ""
    if name_in_use and isinstance(d.get("existing"), dict):
        existing_port = str(d["existing"].get("remote_port", "") or "")
        existing_token = d["existing"].get("token", "") or ""
    print(str(1 if ok else 0) + "|" + str(port) + "|" + str(token) + "|" + str(1 if name_in_use else 0) + "|" + str(existing_port) + "|" + str(existing_token))
except Exception:
    print("0|||||")
')
    local ok_flag _port _token _name_in_use _existing_port _existing_token
    IFS='|' read -r ok_flag _port _token _name_in_use _existing_port _existing_token <<< "$parsed"
    ASSIGNED_PORT="$_port"
    # Prefer the token from a fresh register; fall back to the existing
    # tunnel's token when we hit name_in_use.
    if [[ "$ok_flag" == "1" && -n "$_token" ]]; then
        ASSIGNED_TOKEN=*** 
    elif [[ "$_name_in_use" == "1" && -n "$_existing_token" ]]; then
        ASSIGNED_TOKEN=*** 
    fi
    if [[ "$_name_in_use" == "1" && -n "$_existing_port" && "$_existing_port" != "0" ]]; then
        REUSE_EXISTING_PORT="$_existing_port"
        log "API says name_in_use, will re-adopt port $REUSE_EXISTING_PORT"
    fi
    [[ "$ok_flag" == "1" && -n "$ASSIGNED_PORT" ]]
}

api_heartbeat() {
    [[ -z "${SERVER_API_URL:-}" || -z "${ASSIGNED_TOKEN:-}" ]] && return 0
    api_call POST "/api/v1/tunnels/${ASSIGNED_TOKEN}/heartbeat" '' >/dev/null 2>&1 || true
}

# ============================================================================
# Local proxy lifecycle
# ============================================================================
start_local_proxy() {
    local port="$1" proto="$2" logfile="$3"
    if [[ "$NO_PROXY" == "1" ]]; then
        log "no-proxy mode: nothing to start locally (target=$LOCAL_TARGET)"
        return 0
    fi
    local bin
    # The launcher code below works the same whether we have a real
    # /usr/local/bin/pproxy (PYTHON_BIN set, shebang invocation) or the
    # python3 -m pproxy sentinel (PPROXY_BIN ends with "-m-pproxy").
    if [[ "$PPROXY_BIN" == *-m-pproxy ]]; then
        log "starting pproxy ${proto}://0.0.0.0:${port} (via $PYTHON_BIN -m pproxy)"
        "$PYTHON_BIN" -m pproxy -l "${proto}://0.0.0.0:${port}" \
            </dev/null >>"$logfile" 2>&1 &
        local proxy_pid=$!
        disown -h 2>/dev/null || disown 2>/dev/null || true
        echo "$proxy_pid" > "$KLAN1_TUNNEL_HOME/${NAME}.proxy.pid"
        sleep 1
        return 0
    fi
    bin="$(basename "$PPROXY_BIN" 2>/dev/null)"
    case "$bin" in
        pproxy)
            log "starting pproxy ${proto}://0.0.0.0:${port} (PYTHON_BIN=${PYTHON_BIN:-auto})"
            # setsid would be ideal but it's Linux-only. On macOS, the
            # `trap '' HUP` inside the wrapper plus disown is what keeps
            # things alive. For pproxy we just disown and rely on the fact
            # that nothing sends SIGHUP to a non-job-control process.
            if [[ -n "$PYTHON_BIN" ]]; then
                "$PYTHON_BIN" "$PPROXY_BIN" -l "${proto}://0.0.0.0:${port}" \
                    </dev/null >>"$logfile" 2>&1 &
            else
                "$PPROXY_BIN" -l "${proto}://0.0.0.0:${port}" \
                    </dev/null >>"$logfile" 2>&1 &
            fi
            local proxy_pid=$!
            disown -h 2>/dev/null || disown 2>/dev/null || true
            echo "$proxy_pid" > "$KLAN1_TUNNEL_HOME/${NAME}.proxy.pid"
            sleep 1
            ;;
        tinyproxy)
            log "starting tinyproxy on :${port}"
            tinyproxy </dev/null >>"$logfile" 2>&1 &
            local proxy_pid=$!
            disown -h 2>/dev/null || disown 2>/dev/null || true
            echo "$proxy_pid" > "$KLAN1_TUNNEL_HOME/${NAME}.proxy.pid"
            sleep 1
            ;;
        *)
            die "no supported local proxy found (install pproxy or tinyproxy)"
            ;;
    esac
}

stop_local_proxy() {
    local pidfile="$KLAN1_TUNNEL_HOME/${NAME}.proxy.pid"
    if [[ -f "$pidfile" ]]; then
        local pid; pid="$(cat "$pidfile" 2>/dev/null)"
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
            sleep 1
            kill -9 "$pid" 2>/dev/null || true
        fi
        rm -f "$pidfile"
    fi
}

# ============================================================================
# Server-side port helpers
# ============================================================================
in_use_on_server() {
    local port="$1"
    ssh -o BatchMode=yes -o ConnectTimeout=5 -o IdentitiesOnly=yes \
        -p "$SERVER_PORT" -o User="$SERVER_USER" -o IdentityFile="$SERVER_KEY" \
        "$SERVER_HOST" "ss -tln 2>/dev/null | grep -qE '[:\"]${port}[[:space:]]'"
}

remote_port_listening() {
    local port="$1"
    ssh -o BatchMode=yes -o ConnectTimeout=5 -o IdentitiesOnly=yes \
        -p "$SERVER_PORT" -o User="$SERVER_USER" -o IdentityFile="$SERVER_KEY" \
        "$SERVER_HOST" "ss -tln 2>/dev/null | grep -qE '[:\"]${port}[[:space:]]'"
}

next_free_port() {
    local lo="$1" hi="$2"
    local p
    for ((p=lo; p<=hi; p++)); do
        if ! in_use_on_server "$p"; then
            echo "$p"
            return 0
        fi
    done
    return 1
}

# ============================================================================
# Tunnel lifecycle
# ============================================================================
start_tunnel() {
    log_init
    detect_local
    [[ -n "$NAME" ]] || { usage; die "--name is required"; }

    if ! server_resolve; then
        die "no tunnel server reachable from this device (check fleet.json and network)" 2
    fi
    log "selected server: $SERVER_ALIAS ($SERVER_USER@$SERVER_HOST:$SERVER_PORT)"

    # Determine remote port
    FINAL_REMOTE_PORT=""
    ASSIGNED_TOKEN=""
    ASSIGNED_PORT=""
    REUSE_EXISTING_PORT=""

    if [[ -n "$REMOTE_PORT" ]]; then
        FINAL_REMOTE_PORT="$REMOTE_PORT"
    elif [[ -n "$SERVER_API_URL" ]]; then
        if api_register "$NAME" "" "$PROXY_PROTOCOL" "$TTL"; then
            FINAL_REMOTE_PORT="$ASSIGNED_PORT"
        else
            log "API register failed, falling back to default 65082"
            FINAL_REMOTE_PORT="65082"
        fi
    else
        if in_use_on_server 65082; then
            FINAL_REMOTE_PORT="$(next_free_port 65082 65300)" || die "no free port in 65082-65300 on $SERVER_ALIAS" 3
        else
            FINAL_REMOTE_PORT="65082"
        fi
    fi

    # If the API said "name_in_use", re-adopt the existing tunnel's port
    if [[ -n "${REUSE_EXISTING_PORT:-}" ]]; then
        log "re-adopting existing tunnel for $NAME on port $REUSE_EXISTING_PORT"
        FINAL_REMOTE_PORT="$REUSE_EXISTING_PORT"
    fi

    # Verify the chosen port is actually free on the server side
    if in_use_on_server "$FINAL_REMOTE_PORT"; then
        die "remote port $FINAL_REMOTE_PORT already in use on $SERVER_ALIAS" 3
    fi

    LOCAL_PORT="${PORT:-$FINAL_REMOTE_PORT}"

    # Detect egress IP
    EGRESS_IP="${EGRESS_IP_OVERRIDE:-$(curl -sS --max-time 6 ifconfig.me 2>/dev/null || echo unknown)}"

    # Start local proxy
    local logfile="$KLAN1_TUNNEL_HOME/${NAME}.log"
    start_local_proxy "$LOCAL_PORT" "$PROXY_PROTOCOL" "$logfile"

    # Build ssh options
    local ssh_target=(
        -p "$SERVER_PORT"
        -o "User=$SERVER_USER"
        -o "IdentityFile=$SERVER_KEY"
        -o "IdentitiesOnly=yes"
    )

    local forward_spec
    if [[ "$NO_PROXY" == "1" ]]; then
        forward_spec="-R 0.0.0.0:${FINAL_REMOTE_PORT}:${LOCAL_TARGET}"
    else
        forward_spec="-R 0.0.0.0:${FINAL_REMOTE_PORT}:127.0.0.1:${LOCAL_PORT}"
    fi

    local envfile="$KLAN1_TUNNEL_HOME/${NAME}.env"
    cat > "$envfile" <<ENVEOF
KLAN1_SERVER_HOST='${SERVER_HOST}'
KLAN1_SSH_OPTS='${ssh_target[*]}'
KLAN1_FORWARD='${forward_spec}'
KLAN1_LOGFILE='${logfile}'
ENVEOF
    chmod 600 "$envfile"

    local wrapper="$KLAN1_TUNNEL_HOME/${NAME}.ssh-wrapper.sh"
    cat > "$wrapper" <<'WRAPPER'
#!/bin/bash
# Auto-restart loop for the klan1-tunnel SSH session.
set -u
# Ignore SIGHUP so the wrapper survives the parent shell exiting
# (this is what kept killing the tunnel on macOS when the start script
# returned to its caller).
trap '' HUP
trap '' PIPE
ENV_FILE="${KLAN1_WRAPPER_ENV:?KLAN1_WRAPPER_ENV not set (wrapper invoked without env file)}"
. "$ENV_FILE"
while true; do
    /usr/bin/ssh \
        -o ServerAliveInterval=30 \
        -o ServerAliveCountMax=3 \
        -o ExitOnForwardFailure=yes \
        -o StrictHostKeyChecking=accept-new \
        $KLAN1_SSH_OPTS \
        -N \
        $KLAN1_FORWARD \
        "$KLAN1_SERVER_HOST"
    rc=$?
    echo "[wrapper] ssh exited rc=$rc at $(date -u +%Y-%m-%dT%H:%M:%SZ); restarting in 3s" >> "$KLAN1_LOGFILE"
    sleep 3
done
WRAPPER
    chmod +x "$wrapper"

    local pidfile="$KLAN1_TUNNEL_HOME/${NAME}.pid"
    if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile" 2>/dev/null)" 2>/dev/null; then
        die "tunnel $NAME already running (pid $(cat "$pidfile")) - stop it first"
    fi

    KLAN1_WRAPPER_ENV="$envfile" nohup "$wrapper" </dev/null >>"$logfile" 2>&1 &
    local ssh_pid=$!
    disown -h 2>/dev/null || disown 2>/dev/null || true
    echo "$ssh_pid" > "$pidfile"

    # Give wrapper up to 12s to come up and bind the remote port
    local up=0
    for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
        sleep 1
        if ! kill -0 "$ssh_pid" 2>/dev/null; then
            break
        fi
        if remote_port_listening "$FINAL_REMOTE_PORT"; then
            up=1
            break
        fi
    done

    if ! kill -0 "$ssh_pid" 2>/dev/null; then
        log "wrapper exited immediately - see $logfile"
        rm -f "$pidfile"
        stop_local_proxy
        die "tunnel failed to start" 1
    fi

    if [[ $up -ne 1 ]]; then
        log "warning: wrapper is running but remote port $FINAL_REMOTE_PORT is not yet listening on $SERVER_ALIAS"
        sleep 5
        if ! remote_port_listening "$FINAL_REMOTE_PORT"; then
            log "still not listening. killing tunnel."
            kill "$ssh_pid" 2>/dev/null
            rm -f "$pidfile"
            stop_local_proxy
            die "remote port $FINAL_REMOTE_PORT not reachable on $SERVER_ALIAS after 17s" 1
        fi
    fi

    # Persist config
    config_save "$NAME" "$SERVER_ALIAS" "$LOCAL_PORT" "$FINAL_REMOTE_PORT" "$PROXY_PROTOCOL" \
                "$NO_PROXY" "$LOCAL_TARGET" "$(date -u +%s)" "$TTL"

    # Write status JSON
    local expires_at
    expires_at="$(date -u -d "+${TTL} seconds" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
                  || date -u -v+${TTL}S +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
                  || echo unknown)"
    cat > "$KLAN1_TUNNEL_HOME/${NAME}.status" <<EOF
{
  "name": "$NAME",
  "server": "$SERVER_ALIAS",
  "server_host": "$SERVER_HOST",
  "remote_port": $FINAL_REMOTE_PORT,
  "local_port": $LOCAL_PORT,
  "protocol": "$PROXY_PROTOCOL",
  "egress_ip": "$EGRESS_IP",
  "started_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "ttl": $TTL,
  "expires_at": "$expires_at",
  "pid": $ssh_pid,
  "no_proxy": $NO_PROXY,
  "local_target": "$LOCAL_TARGET",
  "api_token": "${ASSIGNED_TOKEN:-}"
}
EOF

    # Heartbeat loop
    if [[ -n "$SERVER_API_URL" && -n "$ASSIGNED_TOKEN" ]]; then
        (
            while kill -0 "$ssh_pid" 2>/dev/null; do
                api_heartbeat
                sleep 60
            done
        ) &
        local hb_pid=$!
        disown 2>/dev/null || true
        echo "$hb_pid" > "$KLAN1_TUNNEL_HOME/${NAME}.hb.pid"
    fi

    if [[ "$NO_PROXY" == "1" ]]; then
        echo "TUNNEL_READY: http://${SERVER_HOST}:${FINAL_REMOTE_PORT}  -> ${LOCAL_TARGET}"
    else
        echo "TUNNEL_READY: ${PROXY_PROTOCOL}://${SERVER_HOST}:${FINAL_REMOTE_PORT}  (egress=$EGRESS_IP, local=:${LOCAL_PORT})"
    fi
    log "tunnel $NAME ready on ${SERVER_HOST}:${FINAL_REMOTE_PORT}"
    exit 0
}

stop_tunnel() {
    log_init
    [[ -n "$NAME" ]] || die "stop requires --name NAME"

    local pidfile="$KLAN1_TUNNEL_HOME/${NAME}.pid"
    local hbpidfile="$KLAN1_TUNNEL_HOME/${NAME}.hb.pid"

    # First, kill any orphan wrapper/ssh for this tunnel name. Belt and
    # suspenders: pkill by name pattern catches wrappers that were started
    # by previous invocations and whose pidfile is gone.
    pkill -f "${NAME}\.ssh-wrapper\.sh" 2>/dev/null || true
    pkill -f "ssh.*-R.*0\.0\.0\.0:65[0-9]{3}.*70\.38\.14\.4" 2>/dev/null || true
    sleep 1

    if [[ -f "$pidfile" ]]; then
        local pid; pid="$(cat "$pidfile" 2>/dev/null)"
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            log "stopping tunnel $NAME (pid $pid)"
            kill "$pid" 2>/dev/null || true
            sleep 1
            pkill -P "$pid" 2>/dev/null || true
        fi
        rm -f "$pidfile"
    fi
    [[ -f "$hbpidfile" ]] && kill "$(cat "$hbpidfile")" 2>/dev/null && rm -f "$hbpidfile"
    stop_local_proxy

    # Give the remote sshd listener up to 5s to actually close.
    sleep 3
    log "tunnel $NAME stopped"
    exit 0
}

status_tunnels() {
    log_init
    echo "Active tunnels in $KLAN1_TUNNEL_HOME:"
    echo
    printf "%-15s %-8s %-8s %-8s %-9s %-15s %s\n" "NAME" "SERVER" "RPORT" "LPORT" "PROTO" "EGRESS_IP" "STATUS"
    printf -- "----------------------------------------------------------------------------------------\n"

    local any=0
    while IFS='|' read -r name server port rport proto noproxy ltarget started ttl; do
        [[ -z "$name" ]] && continue
        any=1
        local pidfile="$KLAN1_TUNNEL_HOME/${name}.pid"
        local stat="down"
        if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile" 2>/dev/null)" 2>/dev/null; then
            stat="up"
        fi
        local egress="?"
        local sf="$KLAN1_TUNNEL_HOME/${name}.status"
        if [[ -f "$sf" ]]; then
            egress=$(grep -oE '"egress_ip"[[:space:]]*:[[:space:]]*"[^"]+"' "$sf" | head -1 | sed -E 's/.*"([^"]*)"$/\1/')
        fi
        printf "%-15s %-8s %-8s %-8s %-9s %-15s %s\n" "$name" "$server" "$rport" "$port" "$proto" "$egress" "$stat"
    done < <(config_list)

    if [[ $any -eq 0 ]]; then
        echo "(no tunnels configured yet - run with --name <name> to start one)"
    fi
    exit 0
}

# ============================================================================
# Main
# ============================================================================
parse_args "$@"

case "$ACTION" in
    start)        start_tunnel ;;
    stop)         stop_tunnel ;;
    status|list)  status_tunnels ;;
    *)            usage; exit 1 ;;
esac
