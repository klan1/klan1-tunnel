# Pasos restantes para completar el deploy E2E
# (los comandos de abajo se pueden correr SIN sudo en la Mac, son seguros)

# 1. Bajar la private key de tunnel-65081 desde ai1
mkdir -p ~/.klan1-tunnel
chmod 700 ~/.klan1-tunnel
ssh ai1 'cat /etc/klan1-tunnel/tunnel-65081.key' > ~/.klan1-tunnel/id_ed25519_tunnel-65081
chmod 600 ~/.klan1-tunnel/id_ed25519_tunnel-65081

# 2. Verificar la key (debería empezar con -----BEGIN OPENSSH PRIVATE KEY-----)
head -2 ~/.klan1-tunnel/id_ed25519_tunnel-65081

# 3. Instalar el cliente (no necesita sudo, va a ~/bin que ya debería estar en PATH)
mkdir -p ~/bin
cp ~/Develop/hermes/klan1-tunnel/client/klan1-tunnel.sh ~/bin/klan1-tunnel
chmod 755 ~/bin/klan1-tunnel
ls -la ~/bin/klan1-tunnel

# 4. Probar el cliente con --help para ver que syntax es OK
~/bin/klan1-tunnel --help

# 5. ABRIR EL TÚNEL (ajustar --port a lo que quieras exponer)
# Ejemplo: exponer un HTTP server en :8080 de tu Mac
# Si no tenés nada corriendo en :8080, primero levantá uno:
#   python3 -m http.server 8080 --bind 127.0.0.1 &
# Después abrí el túnel:
#   ~/bin/klan1-tunnel start --name macbook --port 8080 --server primary \
#     --api-url https://api.tunnels.example.com --remote-port 65081

# 6. En otra terminal, verificar que el túnel responde
curl -v https://1.tunnels.example.com/
# Deberías ver tu HTTP server local

# 7. Para cerrar el túnel cuando termines
~/bin/klan1-tunnel stop --name macbook --server primary