#!/bin/bash
# /usr/local/bin/tunnel-iptables.sh
# Abre los puertos para el servicio klan1-tunnel usando ufw
# - 65500/tcp: API server (solo loopback)
# - 65082-65300/tcp: puertos túnel SSH para usuarios tunnel-*
# Idempotente: chequea si la regla ya existe antes de agregarla
#
# Ajusta API_PORT/TUNNEL_PORT_MIN/TUNNEL_PORT_MAX a tu deploy si usas
# un rango diferente.

set -euo pipefail

API_PORT=65500
TUNNEL_PORT_MIN=65082
TUNNEL_PORT_MAX=65300

# API server: solo loopback (Caddy hace de frontend público)
if ! ufw status | grep -q "$API_PORT/tcp.*lo"; then
    ufw allow in on lo to 127.0.0.1 port $API_PORT proto tcp comment "klan1-tunnel API (loopback only)"
    echo "Added ufw rule: ACCEPT tcp 127.0.0.1:$API_PORT (loopback)"
fi

# Puertos túnel: aceptar conexiones desde cualquier origen
if ! ufw status | grep -q "$TUNNEL_PORT_MIN:$TUNNEL_PORT_MAX/tcp"; then
    ufw allow $TUNNEL_PORT_MIN:$TUNNEL_PORT_MAX/tcp comment "klan1-tunnel SSH port range"
    echo "Added ufw rule: ACCEPT tcp $TUNNEL_PORT_MIN-$TUNNEL_PORT_MAX"
fi

# Mostrar reglas activas
echo "--- ufw status (klan1-tunnel related) ---"
ufw status | grep -E "6508|65500|klan1" || echo "(no tunnel rules yet)"