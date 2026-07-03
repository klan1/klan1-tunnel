# Server install (admin)

This is the longer form of the README "Quick start (for the
server admin)" section. It assumes you're running on a fresh
Ubuntu/Debian box with `root` (or sudo) access.

## 1. Pick a domain + DNS

You need a base domain (e.g. `tunnels.example.com`) under your
control. Point the wildcard `*.tunnels.example.com` at the public
IP of the server. (We use Cloudflare for DNS but any provider
works as long as you can set an A record.)

```sh
# In your DNS provider:
*.tunnels.example.com  A  <server-public-ip>
tunnels.example.com    A  <server-public-ip>     # for the API
api.tunnels.example.com A <server-public-ip>     # (or CNAME to tunnels.)
```

## 2. Install Caddy + cloudflare plugin

```sh
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | \
    sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/xcaddy/gpg.key' | \
    sudo gpg --dearmor -o /usr/share/keyrings/caddy-xcaddy-archive-keyring.gpg
. /etc/os-release
echo "deb [signed-by=/usr/share/keyrings/caddy-stable-archive-keyring.gpg] \
    https://dl.cloudsmith.io/deb/caddy/stable/any-version/deb/debian any-version main" | \
    sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update
sudo apt install caddy

# Build xcaddy with the cloudflare DNS module
sudo apt install -y golang-go
go install github.com/caddyserver/xcaddy/cmd/xcaddy@latest
~/go/bin/xcaddy build --with github.com/caddy-dns/cloudflare
sudo cp caddy /usr/local/bin/caddy
```

## 3. Cloudflare DNS token

Caddy needs an API token with `Zone.DNS:Edit` for
`tunnels.example.com`. Create one at
<https://dash.cloudflare.com/profile/api-tokens> (use the
"Edit zone DNS" template). Save the token to a file on the
server:

```sh
sudo install -d -m 0700 /etc/klan1-tunnel
echo -n 'cfat_your_token_here' | sudo tee /etc/klan1-tunnel/caddy-dns-token
sudo chmod 600 /etc/klan1-tunnel/caddy-dns-token
```

## 4. Caddy config

Edit `/etc/caddy/Caddyfile` so it serves the API endpoint and
imports the dynamic tunnels slice:

```caddyfile
# /etc/caddy/Caddyfile
api.tunnels.example.com {
    tls {
        dns cloudflare {file=/etc/klan1-tunnel/caddy-dns-token}
    }
    reverse_proxy 127.0.0.1:65500
}

# Wildcard for the dynamic tunnel vhosts
*.tunnels.example.com, tunnels.example.com {
    tls {
        dns cloudflare {file=/etc/klan1-tunnel/caddy-dns-token}
    }
    @apitunnel host api.tunnels.example.com
    handle @apitunnel {
        abort
    }
    import /etc/caddy/Caddyfile.klan1-tunnel
}
```

`/etc/caddy/Caddyfile.klan1-tunnel` is generated automatically by
the server on every provision / release. Don't edit it by hand.

## 5. Install the klan1-tunnel server

```sh
sudo install -d -m 0755 /usr/local/bin
sudo curl -sSL -o /usr/local/bin/klan1-tunnel-server.py \
    https://raw.githubusercontent.com/klan1/klan1-tunnel/main/server/klan1-tunnel-server.py
sudo chmod 755 /usr/local/bin/klan1-tunnel-server.py
```

Install the tunnel shell (used by per-device unix users):

```sh
sudo curl -sSL -o /usr/local/bin/tunnel-shell.sh \
    https://raw.githubusercontent.com/klan1/klan1-tunnel/main/server/tunnel-shell.sh
sudo chmod 755 /usr/local/bin/tunnel-shell.sh
```

Create the data dirs:

```sh
sudo install -d -m 0750 -o root -g klan1 /var/lib/klan1-tunnel
sudo install -d -m 0750 -o root -g klan1 /var/lib/klan1-tunnel/users
```

## 6. Systemd unit

```ini
# /etc/systemd/system/klan1-tunnel-server.service
[Unit]
Description=klan1-tunnel API server (self-hosted ngrok-like)
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /usr/local/bin/klan1-tunnel-server.py \
    --port 65500 \
    --bind 127.0.0.1 \
    --state /var/lib/klan1-tunnel/state.json \
    --key-dir /etc/klan1-tunnel \
    --port-lo 65081 \
    --port-hi 65090 \
    --default-ttl 86400
Restart=always
RestartSec=3
User=root
Group=root

# Hardening: /etc is read-only by default; we add it to the writable
# list because useradd/groupadd need to write /etc/passwd, /etc/group,
# /etc/shadow, /etc/gshadow. userdel and groupdel also need write.
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/lib/klan1-tunnel /etc/klan1-tunnel /etc

[Install]
WantedBy=multi-user.target
```

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now klan1-tunnel-server
```

## 7. First-boot admin password

On first boot, the server generates a random `admin` password and
prints it to stderr. Read it:

```sh
sudo journalctl -u klan1-tunnel-server -n 50 --no-pager | grep -A2 'FIRST BOOT'
```

Output looks like:

```
[admin] FIRST BOOT: generated admin user.
[admin]   username: admin
[admin]   password: MbOgEyVtQ5JkPTqIjUYjrDvh
[admin]   saved to: /etc/klan1-tunnel/dashboard-auth.json (mode 0600)
```

The password is hashed with bcrypt (cost 10) and saved to
`/etc/klan1-tunnel/dashboard-auth.json`. To rotate the admin
password, generate a new bcrypt hash and put it in the file:

```sh
NEW_HASH=$(python3 -c "import bcrypt; print(bcrypt.hashpw(b'your-new-pw', bcrypt.gensalt(10)).decode())")
echo "{\"users\": {\"admin\": {\"password_bcrypt\": \"$NEW_HASH\"}}}" | \
    sudo tee /etc/klan1-tunnel/dashboard-auth.json
sudo chmod 600 /etc/klan1-tunnel/dashboard-auth.json
```

## 8. Caddy reload

After the first provision, the server writes the tunnel vhost
slice to `/etc/caddy/Caddyfile.klan1-tunnel` and reloads Caddy
(graceful). Watch the journal to confirm:

```sh
sudo journalctl -u klan1-tunnel-server -f
# should show:
#   [caddy] reloaded after add of <device>:<port> (1 active vhost(s))
```

## 9. Create your first API key

Open `https://api.tunnels.example.com/dashboard/admin` in a
browser. The browser will pop the HTTP Basic auth dialog — use
the `admin` / `<generated-password>` from step 7. From the admin
page, create a key (e.g. for `macbook`), copy the secret (it's
shown exactly once), and hand it to the device owner along with
your API URL (`https://api.tunnels.example.com`) and the
`device_id` (`macbook`).

## 10. The client runs the installer

On the device:

```sh
curl -sSL https://raw.githubusercontent.com/klan1/klan1-tunnel/main/install.sh | \
    bash -s -- \
    --device-id macbook \
    --api-url https://api.tunnels.example.com \
    --api-key "$KEY"
```

That's it. Local port 8080 is now reachable at
`https://macbook.tunnels.example.com/`.

## Upgrading

```sh
sudo systemctl stop klan1-tunnel-server
sudo curl -sSL -o /usr/local/bin/klan1-tunnel-server.py \
    https://raw.githubusercontent.com/klan1/klan1-tunnel/main/server/klan1-tunnel-server.py
sudo systemctl start klan1-tunnel-server
```

The server is stateless except for `state.json` (live tunnels) +
`api-keys.json` (created keys) + `dashboard-auth.json` (admin
passwords). Back these up before upgrading.

## Troubleshooting

- **"useradd: cannot lock /etc/group"** — your systemd unit is
  missing `/etc` from `ReadWritePaths`. Fix the unit, daemon-reload,
  restart.
- **"no_free_ports"** — all 10 ports in 65081-65090 are taken.
  Revoke unused keys (admin page) or wait for heartbeats to expire.
- **Caddy reload fails with "caddy validate"** — run
  `caddy validate --config /etc/caddy/Caddyfile` manually to see
  the error. The server rolls back to the previous working config.
- **Tunnel gets 502 from Caddy** — the SSH reverse-tunnel from the
  device dropped. Check the device's `~/.klan1-tunnel/tunnel.log`
  and `~/.klan1-tunnel/heartbeat.log`. Often it's a firewall
  blocking the outbound SSH; or the device went to sleep and the
  SSH session timed out — install `autossh` to auto-reconnect.
- **Heartbeat says 401** — your JWT expired. Re-run the installer
  (or have the device re-fetch a JWT every 5 min, which the
  installer does for you as long as it isn't restarted).
