# Deploy de klan1-tunnel en un servidor (este script está parametrizado;
# edita las variables AI1_HOST / API_PORT / SUBDOMAIN_BASE para apuntar
# a tu server).
# Ejecutar como root. Cada bloque es un comando o grupo de comandos relacionado.
# Si algo falla, parar y avisar — NO continuar.
#
# Archivos fuente (en tu Mac, en ~/Develop/hermes/klan1-tunnel/):
#   deploy/Caddyfile.tunnels    → se concatena al /etc/caddy/Caddyfile
#   deploy/tunnel-shell.sh     → /usr/local/bin/tunnel-shell.sh
#   deploy/sshd-drop-in.conf   → /etc/ssh/sshd_config.d/10-tunnel-users.conf
#   deploy/iptables.sh         → /usr/local/bin/tunnel-iptables.sh
#
# Desde la Mac: scp estos archivos a ai1:/tmp/ antes de empezar.

# =============================================================================
# PASO 0: Scp desde la Mac (en una terminal separada, ANTES de loguearte como root)
# =============================================================================

#   scp ~/Develop/hermes/klan1-tunnel/deploy/Caddyfile.tunnels      ai1:/tmp/
#   scp ~/Develop/hermes/klan1-tunnel/deploy/tunnel-shell.sh       ai1:/tmp/
#   scp ~/Develop/hermes/klan1-tunnel/deploy/sshd-drop-in.conf     ai1:/tmp/
#   scp ~/Develop/hermes/klan1-tunnel/deploy/iptables.sh           ai1:/tmp/

# =============================================================================
# PASO 1: Backup y deploy del Caddyfile
# =============================================================================

# Backup del Caddyfile actual (preserva vscode + opencode)
cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak.pre-tunnels.$(date +%Y%m%d-%H%M%S)

# Concatenar el bloque nuevo (ya está en /tmp/Caddyfile.tunnels.new)
cat /tmp/Caddyfile.tunnels.new >> /etc/caddy/Caddyfile

# Verificar el Caddyfile final (debe tener vscode + opencode + 10 tunnels)
cat /etc/caddy/Caddyfile
echo "---"
echo "Líneas totales:"
wc -l /etc/caddy/Caddyfile

# =============================================================================
# PASO 2: Validar config de Caddy (NO reinicia todavía)
# =============================================================================

# Esto hace parse + dry-run, verifica que no haya errores de sintaxis
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile

# =============================================================================
# PASO 3: Crear directorio /etc/klan1-tunnel (donde va el RSA del API)
# Ya existe (/etc/klan1-tunnel/jwt-{private,public}.pem) según el estado previo.
# Verificar:
ls -la /etc/klan1-tunnel/

# =============================================================================
# PASO 4: Crear grupo tunnel-users y shell restrictivo
# =============================================================================

# 4a. Crear grupo tunnel-users (si no existe ya)
getent group tunnel-users >/dev/null || groupadd tunnel-users
getent group tunnel-users

# 4b. Copiar el shell restrictivo (ya existe en /Users/j0hnd003/... en la Mac;
#    scp lo subió? — re-corro el cp para asegurarme)
cat > /usr/local/bin/tunnel-shell.sh <<'EOF'
#!/bin/bash
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] tunnel-shell: USER=${USER} TTY=$(tty 2>/dev/null) SSH_CONNECTION=${SSH_CONNECTION}" >> /var/log/tunnel-shell.log
exec /usr/bin/sleep infinity
EOF
chmod 755 /usr/local/bin/tunnel-shell.sh
ls -la /usr/local/bin/tunnel-shell.sh

# 4c. Crear el directorio de log (0664 root:tunnel-users para que tunnel-65081 pueda escribir)
touch /var/log/tunnel-shell.log
chown root:tunnel-users /var/log/tunnel-shell.log
chmod 664 /var/log/tunnel-shell.log

# =============================================================================
# PASO 5: Drop-in sshd config
# =============================================================================

# 5a. Crear el directorio si no existe
mkdir -p /etc/ssh/sshd_config.d

# 5b. Escribir el drop-in
cat > /etc/ssh/sshd_config.d/10-tunnel-users.conf <<'EOF'
# Drop-in para sshd: restringe a los usuarios tunnel-* a hacer SOLO tunnel
# - Shell restrictivo (tunnel-shell.sh: solo sleep infinity)
# - Sin TTY allocation (sin shell interactivo, no hay terminal)
# - Sin X11, sin agent forwarding
# - TCP forwarding REMOTO habilitado (es lo que hacen los túneles inversos)
# - permitopen limited to 127.0.0.1:<API_PORT> (loopback only)
# - AllowTcpForwarding remote (no local, no dynamic — sólo -R)
# - GatewayPorts no (los túneles inversos solo se acceden vía loopback)

Match Group tunnel-users
    ForceCommand /usr/local/bin/tunnel-shell.sh
    AllowTcpForwarding remote
    GatewayPorts no
    PermitTTY no
    X11Forwarding no
    AllowAgentForwarding no
    PermitUserEnvironment no
    PasswordAuthentication no
    PermitTunnel no
EOF
chmod 644 /etc/ssh/sshd_config.d/10-tunnel-users.conf

# 5c. Validar la config de sshd (DRY RUN — no reinicia)
sshd -t
echo "sshd config OK"

# =============================================================================
# PASO 6: Crear user tunnel-65081 (el primero, para subdomain 1)
# =============================================================================

# 6a. Crear el usuario
useradd -m -s /usr/local/bin/tunnel-shell.sh -G tunnel-users tunnel-65081
id tunnel-65081

# 6b. Crear directorio .ssh
mkdir -p /home/tunnel-65081/.ssh
chmod 700 /home/tunnel-65081/.ssh
chown tunnel-65081:tunnel-65081 /home/tunnel-65081/.ssh

# 6c. Generar el keypair (la privada va a la Mac, la pública a authorized_keys)
ssh-keygen -t ed25519 -f /etc/klan1-tunnel/tunnel-65081.key -N "" -C "tunnel-65081@ai1"

# 6d. Instalar la pública en authorized_keys (con restrict + permitopen)
PUBKEY=$(cat /etc/klan1-tunnel/tunnel-65081.key.pub)
cat > /home/tunnel-65081/.ssh/authorized_keys <<EOF
command="",from="*",restrict,port-forwarding,permitopen="127.0.0.1:65081" $PUBKEY
EOF
chmod 600 /home/tunnel-65081/.ssh/authorized_keys
chown tunnel-65081:tunnel-65081 /home/tunnel-65081/.ssh/authorized_keys

# 6e. Ver
ls -la /home/tunnel-65081/.ssh/
cat /home/tunnel-65081/.ssh/authorized_keys

# =============================================================================
# PASO 7: Abrir puertos con iptables
# =============================================================================

# 7a. Subir el script
scp lo maneja — pero como root, lo copio de /tmp:
cp /tmp/tunnel-iptables.sh /usr/local/bin/tunnel-iptables.sh 2>/dev/null || cat > /usr/local/bin/tunnel-iptables.sh <<'EOF'
#!/bin/bash
set -euo pipefail

API_PORT=65500
TUNNEL_PORT_MIN=65082
TUNNEL_PORT_MAX=65300

# API server: solo loopback (Caddy hace de frontend público)
if ! iptables -C INPUT -p tcp --dport $API_PORT -i lo -j ACCEPT 2>/dev/null; then
    iptables -I INPUT 1 -p tcp --dport $API_PORT -i lo -j ACCEPT
    echo "Added: ACCEPT tcp 127.0.0.1:$API_PORT"
fi

# Puertos túnel: aceptar conexiones desde cualquier origen
if ! iptables -C INPUT -p tcp --dport $TUNNEL_PORT_MIN:$TUNNEL_PORT_MAX -j ACCEPT 2>/dev/null; then
    iptables -I INPUT 2 -p tcp --dport $TUNNEL_PORT_MIN:$TUNNEL_PORT_MAX -j ACCEPT
    echo "Added: ACCEPT tcp $TUNNEL_PORT_MIN-$TUNNEL_PORT_MAX"
fi

echo "--- INPUT chain (top 10) ---"
iptables -L INPUT -n --line-numbers | head -15
EOF
chmod 755 /usr/local/bin/tunnel-iptables.sh

# 7b. Correrlo
/usr/local/bin/tunnel-iptables.sh

# 7c. Persistir las reglas (para que sobrevivan reboots)
# Debian/Ubuntu usa iptables-persistent. Si está instalado:
which netfilter-persistent && netfilter-persistent save
# Si no, alternativa con iptables-save:
which iptables-save && iptables-save > /etc/iptables.rules

# =============================================================================
# PASO 8: Verificar el API server
# =============================================================================

# El API server ya debería estar corriendo (systemd). Verifico:
systemctl status klan1-tunnel-server --no-pager -l | head -15

# Health check:
curl -s http://127.0.0.1:65500/healthz
echo ""

# =============================================================================
# PASO 9: Recargar servicios (Caddy y sshd)
# =============================================================================

# sshd: validar primero, luego reload
sshd -t && systemctl reload ssh
echo "sshd reloaded"

# caddy: validar primero, luego reload (esto va a emitir los 10 certs)
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile && systemctl reload caddy
echo "caddy reloaded — esperá ~30s mientras emite los certs"

# Ver logs de Caddy mientras emite los certs:
journalctl -u caddy -f --since "30 seconds ago"

# =============================================================================
# PASO 10: Sacar la clave privada de tunnel-65081 para que la Mac la use
# =============================================================================

# Mostrar el contenido de la private key (la copiás a la Mac):
cat /etc/klan1-tunnel/tunnel-65081.key

# Después en la Mac la guardás en:
#   ~/.klan1-tunnel/id_ed25519_tunnel-65081
# con chmod 600