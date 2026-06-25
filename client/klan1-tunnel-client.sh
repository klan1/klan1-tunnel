#!/bin/bash
# /usr/local/bin/klan1-tunnel-client.sh
# Cliente para abrir un túnel reverso a ai1 vía un subdominio preconfigurado
# Uso:
#   klan1-tunnel-client.sh --name macbook --local-port 8080 [--allow-from "10.0.0.0/8,192.168.0.0/16"]
#                                                   [--allow-from none]
#   klan1-tunnel-client.sh --name macbook --disconnect
#
# Aplica firewall (pf/iptables) en el device para que el proxy local
# solo responda a las IPs/CIDRs permitidas. Por default, abierto a todos.
# Si --allow-from none, cierra el puerto a todo el mundo.

set -euo pipefail

# --- Defaults ---
SUBDOMAIN=""
LOCAL_PORT=""
ALLOW_FROM="open"   # default: abierto a todos
TUNNEL_ID=""
AI1_HOST="<your-api-server-host>"
SUBDOMAIN_NUM="1"   # mapeo fijo: 1.<SUBDOMAIN_BASE> → puerto 65081
TUNNEL_REMOTE_PORT=65081
SUBDOMAIN_BASE="tunnels.<your-domain>"   # override via fleet.json or env SUBDOMAIN_BASE
STATE_DIR="${HOME}/.klan1-tunnel"
STATE_FILE="${STATE_DIR}/state.json"
LOG_FILE="${STATE_DIR}/client.log"
HEARTBEAT_INTERVAL=30

# --- Helpers ---
log() {
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG_FILE"
}

die() {
    log "ERROR: $*"
    exit 1
}

# ----------------------------------------------------------------------------
# Fleet config (read from fleet.json if present, else generic defaults)
# ----------------------------------------------------------------------------
FLEET_LOCAL="${STATE_DIR}/fleet.local.json"
FLEET_COMMITTED="${STATE_DIR}/fleet.json"
FLEET_EXAMPLE="${STATE_DIR}/fleet.example.json"

_fleet_file=""
[[ -f "$FLEET_LOCAL" ]] && _fleet_file="$FLEET_LOCAL"
[[ -z "$_fleet_file" && -f "$FLEET_COMMITTED" ]] && _fleet_file="$FLEET_COMMITTED"
[[ -z "$_fleet_file" && -f "$FLEET_EXAMPLE" ]] && _fleet_file="$FLEET_EXAMPLE"

if [[ -n "$_fleet_file" ]]; then
    _b64_url="$(python3 -c "
import json, sys
d = json.load(open(sys.argv[1]))
bd = d.get('base_domain')
print(bd if bd else '')
" "$_fleet_file" 2>/dev/null)"
    [[ -n "$_b64_url" ]] && SUBDOMAIN_BASE="$_b64_url"

    _api_url="$(python3 -c "
import json, sys
d = json.load(open(sys.argv[1]))
a = d.get('api', {})
print(a.get('url','') if isinstance(a, dict) else d.get('api_url',''))
" "$_fleet_file" 2>/dev/null)"
    [[ -n "$_api_url" ]] && AI1_HOST="$_api_url"
fi

usage() {
    cat <<EOF
Uso:
  $0 --name <name> --local-port <port> [--allow-from <CIDR-list>|none] [--disconnect]

Opciones:
  --name <name>       Nombre del device (ej: macbook, chromebook, iphone-juan)
  --local-port <port> Puerto local a exponer vía el túnel
  --allow-from <list> Lista separada por comas de CIDRs permitidos (default: open)
                      Use "none" para cerrar el puerto a todo
  --disconnect        Cierra el túnel y limpia las reglas de firewall
  -h, --help          Muestra esta ayuda

Ejemplos:
  $0 --name macbook --local-port 8080
  $0 --name macbook --local-port 8080 --allow-from "10.0.0.0/8,192.168.1.0/24"
  $0 --name macbook --local-port 8080 --allow-from none
  $0 --name macbook --disconnect
EOF
    exit 1
}

# --- Parse args ---
DISCONNECT=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --name)          SUBDOMAIN="$2"; shift 2 ;;
        --local-port)    LOCAL_PORT="$2"; shift 2 ;;
        --allow-from)    ALLOW_FROM="$2"; shift 2 ;;
        --disconnect)    DISCONNECT=true; shift ;;
        -h|--help)       usage ;;
        *)               die "Opción desconocida: $1" ;;
    esac
done

mkdir -p "$STATE_DIR"

# --- Disconnect ---
if $DISCONNECT; then
    if [[ ! -f "$STATE_FILE" ]]; then
        log "No hay túnel activo para cerrar"
        exit 0
    fi

    TUNNEL_ID=$(python3 -c "import json; print(json.load(open('$STATE_FILE'))['tunnel_id'])" 2>/dev/null || echo "")
    PID=$(python3 -c "import json; print(json.load(open('$STATE_FILE'))['ssh_pid'])" 2>/dev/null || echo "")

    log "Cerrando túnel $TUNNEL_ID (ssh pid=$PID)"

    if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
        kill "$PID" || true
        sleep 1
        kill -9 "$PID" 2>/dev/null || true
        log "ssh tunnel process terminado"
    fi

    # Quitar reglas de firewall
    cleanup_firewall

    rm -f "$STATE_FILE"
    log "Túnel cerrado y estado limpio"
    exit 0
fi

# --- Validar args requeridos para connect ---
[[ -z "$SUBDOMAIN" ]]   && die "Falta --name"
[[ -z "$LOCAL_PORT" ]] && die "Falta --local-port"

# --- Detectar OS y aplicar firewall ---
detect_local() {
    case "$(uname -s)" in
        Darwin) echo "macos" ;;
        Linux)  echo "linux" ;;
        *)      die "OS no soportado: $(uname -s)" ;;
    esac
}

apply_firewall_rule() {
    local os="$1"
    local port="$2"
    local allow="$3"

    if [[ "$allow" == "none" ]]; then
        log "Cerrando puerto local $port a todo el mundo"
    else
        log "Abriendo puerto local $port (allow_from=$allow)"
    fi

    case "$os" in
        macos)
            # macOS: usar pf con una anchor table
            local anchor="/etc/pf.anchors/klan1-tunnel-$port"
            echo "table <klan1-tunnel-$port> persist" | sudo tee "$anchor" >/dev/null

            if [[ "$allow" != "open" ]]; then
                IFS=',' read -ra CIDRS <<< "$allow"
                for cidr in "${CIDRS[@]}"; do
                    echo "table <klan1-tunnel-$port> { $cidr }" | sudo tee -a "$anchor" >/dev/null
                done
            fi

            local rule="rdr pass on lo inet proto tcp to port $port -> 127.0.0.1 port $port"
            echo "$rule" | sudo tee -a "$anchor" >/dev/null
            sudo pfctl -a "klan1-tunnel/$port" -f "$anchor" 2>/dev/null || true
            sudo pfctl -e 2>/dev/null || true
            ;;
        linux)
            # Linux: usar iptables
            if [[ "$allow" == "none" ]]; then
                sudo iptables -A INPUT -p tcp --dport "$port" -j DROP
            elif [[ "$allow" == "open" ]]; then
                sudo iptables -A INPUT -p tcp --dport "$port" -j ACCEPT
            else
                IFS=',' read -ra CIDRS <<< "$allow"
                sudo iptables -A INPUT -p tcp --dport "$port" -j DROP
                for cidr in "${CIDRS[@]}"; do
                    sudo iptables -I INPUT -p tcp --dport "$port" -s "$cidr" -j ACCEPT
                done
            fi
            ;;
    esac
}

cleanup_firewall() {
    local os
    os=$(detect_local)
    case "$os" in
        macos)
            sudo pfctl -a "klan1-tunnel/$LOCAL_PORT" -F all 2>/dev/null || true
            sudo rm -f "/etc/pf.anchors/klan1-tunnel-$LOCAL_PORT" 2>/dev/null || true
            ;;
        linux)
            sudo iptables -D INPUT -p tcp --dport "$LOCAL_PORT" -j ACCEPT  2>/dev/null || true
            sudo iptables -D INPUT -p tcp --dport "$LOCAL_PORT" -j DROP    2>/dev/null || true
            # Borrar reglas con -s (CIDRs)
            for cidr in $(sudo iptables -L INPUT -n | grep "dpt:$LOCAL_PORT" | awk '{print $4}' | sort -u); do
                sudo iptables -D INPUT -p tcp --dport "$LOCAL_PORT" -s "$cidr" -j ACCEPT 2>/dev/null || true
            done
            ;;
    esac
}

# --- Inicio ---
OS=$(detect_local)
log "Iniciando túnel: subdomain=$SUBDOMAIN local_port=$LOCAL_PORT allow_from=$ALLOW_FROM"

# Limpiar estado anterior si existe
if [[ -f "$STATE_FILE" ]]; then
    log "Limpiando estado anterior"
    cleanup_firewall
    rm -f "$STATE_FILE"
fi

# 1. Aplicar firewall local (best-effort: si no hay sudo, sigue sin firewall)
if [[ "$ALLOW_FROM" != "open" ]]; then
    if sudo -n true 2>/dev/null; then
        apply_firewall_rule "$OS" "$LOCAL_PORT" "$ALLOW_FROM"
    else
        log "WARN: sudo sin password no disponible, --allow-from se ignora"
        log "WARN: el proxy local queda ABIERTO a todo. Usá pf/iptables manual si necesitás ACL"
    fi
fi

# 2. Abrir túnel SSH reverso a ai1
TUNNEL_USER="tunnel-${TUNNEL_REMOTE_PORT}"
TUNNEL_TARGET="${SUBDOMAIN_NUM}.${SUBDOMAIN_BASE}"

log "Abriendo SSH tunnel reverso: -R ${TUNNEL_REMOTE_PORT}:127.0.0.1:${LOCAL_PORT} ${TUNNEL_USER}@${AI1_HOST}"

# Lanzar SSH en background con auto-reconnect
ssh -N -T \
    -o "ServerAliveInterval=15" \
    -o "ServerAliveCountMax=3" \
    -o "ExitOnForwardFailure=yes" \
    -o "StrictHostKeyChecking=accept-new" \
    -R "${TUNNEL_REMOTE_PORT}:127.0.0.1:${LOCAL_PORT}" \
    -i "${STATE_DIR}/id_ed25519_ai1" \
    "${TUNNEL_USER}@${AI1_HOST}" \
    >"${LOG_FILE}.ssh" 2>&1 &

SSH_PID=$!
log "SSH tunnel pid=$SSH_PID"

# 3. Esperar a que esté listo (el puerto remoto debe estar escuchando)
sleep 3
if ! kill -0 "$SSH_PID" 2>/dev/null; then
    cleanup_firewall
    die "El proceso SSH murió al arrancar. Log: ${LOG_FILE}.ssh"
fi

# 4. Generar tunnel_id y guardar estado
TUNNEL_ID="${SUBDOMAIN}-$(date +%s)-$$"
cat > "$STATE_FILE" <<EOF
{
  "tunnel_id": "$TUNNEL_ID",
  "subdomain": "$SUBDOMAIN",
  "subdomain_num": "$SUBDOMAIN_NUM",
  "local_port": $LOCAL_PORT,
  "remote_port": $TUNNEL_REMOTE_PORT,
  "allow_from": "$ALLOW_FROM",
  "ssh_pid": $SSH_PID,
  "started_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF

log "Túnel $TUNNEL_ID activo en https://${TUNNEL_TARGET}"

# 5. Loop de heartbeat
log "Iniciando heartbeat cada ${HEARTBEAT_INTERVAL}s"
while true; do
    sleep "$HEARTBEAT_INTERVAL"

    if ! kill -0 "$SSH_PID" 2>/dev/null; then
        log "SSH process murió, saliendo"
        cleanup_firewall
        rm -f "$STATE_FILE"
        exit 1
    fi

    log "heartbeat tunnel=$TUNNEL_ID alive=true"
done
