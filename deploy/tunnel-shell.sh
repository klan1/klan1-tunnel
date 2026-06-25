#!/bin/bash
# tunnel-shell.sh — Shell restrictivo para usuarios tunnel-*
# Sólo permite ejecutar sleep infinity y nada más.
# No tiene PATH completo, no puede leer archivos, no puede ejecutar comandos arbitrarios.
# Usado como shell de los usuarios que se crean on-demand para túneles SSH.

# Logueamos quién entró y desde dónde (auditoría)
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] tunnel-shell: USER=${USER} TTY=$(tty 2>/dev/null) SSH_CONNECTION=${SSH_CONNECTION}" >> /var/log/tunnel-shell.log

# Sólo sleep infinity está permitido. Nada más. Bloquea todo lo demás.
exec /usr/bin/sleep infinity
