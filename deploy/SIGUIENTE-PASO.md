# Remaining steps to complete the E2E deploy
# (commands below run WITHOUT sudo on the Mac, they are safe)

# 1. Fetch the private key for tunnel-<port> from your tunnel server
#    (substitute <server> and <port> with your real values)
mkdir -p ~/.klan1-tunnel
chmod 700 ~/.klan1-tunnel
ssh <user>@<server> 'cat /etc/klan1-tunnel/tunnel-<port>.key' > ~/.klan1-tunnel/id_ed25519_tunnel-<port>
chmod 600 ~/.klan1-tunnel/id_ed25519_tunnel-<port>

# 2. Verify the key (should start with -----BEGIN OPENSSH PRIVATE KEY-----)
head -2 ~/.klan1-tunnel/id_ed25519_tunnel-<port>

# 3. Install the client (no sudo needed, goes to ~/bin which should be in PATH)
mkdir -p ~/bin
cp <path-to-repo>/klan1-tunnel/client/klan1-tunnel.sh ~/bin/klan1-tunnel
chmod 755 ~/bin/klan1-tunnel
ls -la ~/bin/klan1-tunnel

# 4. Smoke-test the client --help
~/bin/klan1-tunnel --help

# 5. OPEN THE TUNNEL (adjust --port to whatever you want to expose)
# Example: expose an HTTP server on :8080 of your Mac
# If you don't have anything on :8080, start one first:
#   python3 -m http.server 8080 --bind 127.0.0.1 &
# Then open the tunnel:
#   ~/bin/klan1-tunnel start --name macbook --port 8080 --server primary \
#     --api-url https://api.<your-base-domain> --remote-port <port>

# 6. In another terminal, verify the tunnel responds
curl -v https://<subdomain>.<your-base-domain>/
# You should see your local HTTP server

# 7. To close the tunnel when you're done
~/bin/klan1-tunnel stop --name macbook --server primary
