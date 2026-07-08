# klan1-tunnel

Self-hosted reverse-tunnel service. One binary on your server, one
shell command on your machine, and your local services get stable
public URLs (e.g. `https://macbook.tunnels.example.com/`).

- **API keys** (not whitelisted devices) — give a new device a key
  and it gets a tunnel. Revoke the key and the tunnel is gone.
- **No subdomain numbers** — `<your-name>.tunnels.example.com` is
  yours as long as the key is valid.
- **Caddy reloads dynamically** when tunnels come and go (no
  cutting existing connections).
- **Heartbeat-driven sweep** cleans up expired or unreachable
  tunnels automatically.

## Quick start (for the device owner)

```sh
curl -sSL https://raw.githubusercontent.com/klan1/klan1-tunnel/main/install.sh | \
    bash -s -- \
    --device-id macbook \
    --api-url https://api.tunnels.example.com \
    --api-key "$MY_KEY"
```

That's it. The installer:
1. Calls the API with your key, gets a JWT.
2. Calls the provision endpoint, gets back a private key + SSH
   command + heartbeat schedule.
3. Saves `~/.klan1-tunnel/fleet.json` and the private key.
4. Starts the SSH reverse-tunnel in background.
5. Starts a heartbeat daemon that keeps the tunnel alive.

`http://localhost:8080` on your machine is now reachable at
`https://macbook.tunnels.example.com/`.

## Quick start (for the server admin)

1. **Pick a domain** you control (we use `tunnels.example.com`).
2. **Point the wildcard** `*.tunnels.example.com` at your server.
3. **Install Caddy** + the `cloudflare` plugin (or any other DNS
   challenge provider you prefer).
4. **Install the server** (see `INSTALL.md`):
   ```sh
   sudo curl -sSL -o /usr/local/bin/klan1-tunnel-server.py \
       https://raw.githubusercontent.com/klan1/klan1-tunnel/main/server/klan1-tunnel-server.py
   sudo chmod 755 /usr/local/bin/klan1-tunnel-server.py
   # (configure systemd unit — see INSTALL.md)
   sudo systemctl start klan1-tunnel-server
   ```
5. **The first boot generates an admin password** and prints it to
   the server's journal. Read it:
   ```sh
   sudo journalctl -u klan1-tunnel-server | grep -i "password:"
   ```
6. **Open the admin page** at `https://api.tunnels.example.com/dashboard/admin`
   and create an API key for each device owner.

That's it. Give the device owner their `device_id` + the
`api_url` + the `api_key` (the secret is shown exactly once after
creation). They run the curl command above.

## Architecture

```
┌──────────┐                ┌─────────────────────────┐
│  Your    │  -R 65081:8080 │       Server (Caddy)    │
│  laptop  │  ─────────────►│  *.tunnels.example.com  │
│ :8080    │   SSH reverse  │                         │
└──────────┘   tunnel       │  ┌────────────────────┐ │
     ▲                       │  │ klan1-tunnel-server│ │
     │                       │  │  (API + dashboard) │ │
     │                       │  └────────────────────┘ │
     │ HTTPS                 └────────────┬────────────┘
     │ /api/v1/tunnels/...                 │
     │ /api/v1/auth/login ...              │
     │                                     ▼
     │                       ┌──────────────────────────┐
     └───────────────────────│  Cloudflare (DNS + TLS)  │
         heartbeat           └──────────────────────────┘
         (every 30s)
```

- Your laptop opens an outbound SSH reverse-tunnel to the server.
  The server accepts the connection as user `tunnel-<port>` (e.g.
  `tunnel-65081`) and binds port 65081 locally.
- Caddy terminates TLS at `*.tunnels.example.com` (DNS-01 challenge
  via Cloudflare) and reverse-proxies the matching host to the
  right tunnel-`<port>`.
- Your laptop sends a heartbeat every 30s. If the server doesn't
  hear from you for >24h (or your API key is revoked), the
  sweeper kills your tunnel user and removes the Caddy vhost.

## API

All endpoints expect JSON bodies and return JSON. Auth is either
**HTTP Bearer JWT** (for client endpoints) or **HTTP Basic** (for
admin endpoints).

### Client endpoints (JWT)

```
POST /api/v1/auth/login
  body: {device_id, api_key}            # v2 — uses API key, no whitelist
                                        # v1 (deprecated): device_id only
  resp: {token, device_id, key_id?, expires_in}

POST /api/v1/devices/<device_id>/provision
  headers: Authorization: Bearer <jwt>
  body: {local_port?}                   # default 8080
  resp: 200 with full bundle:
    {device_id, tunnel_user, tunnel_port, fqdn, ssh_host,
     ssh_user, ssh_port, private_key, ssh_command, expires_at,
     token, caddy_reload_ok}
    OR 409 name_in_use / 503 no_free_ports / 401 invalid jwt

POST /api/v1/tunnels/<token>/heartbeat
  headers: Authorization: Bearer <jwt>
  body: {} (empty)
  resp: 200 with tunnel entry, OR 401 invalid jwt

GET /api/v1/tunnels
  resp: {tunnels: [...], count: N}

GET /api/v1/devices
  resp: {devices: [...]}                # v1 whitelist (legacy)

GET /api/v1/free-port
  resp: {port, range: [lo, hi]}
```

### Admin endpoints (HTTP Basic)

```
GET    /api/v1/keys              # list (no secrets, no hashes)
POST   /api/v1/keys              # create: {name, ttl_seconds?}  -> 201 {id, secret, ...}
DELETE /api/v1/keys/<id>         # revoke (idempotent)
GET    /dashboard/admin          # HTML page: API keys + devices
```

### Health

```
GET /healthz                       # {ok: true, time: ...}
```

## Configuration

The server reads its runtime config from `/etc/klan1-tunnel/fleet.json`
(overridable via the `KLAN1_TUNNEL_CONFIG` env var or
`~/.klan1-tunnel/fleet.json` on the client):

```json
{
  "servers": {
    "primary": {
      "host": "tunnel.example.com",
      "user": "root",
      "port": 22
    }
  },
  "server_order": ["primary"],
  "base_domain": "tunnels.example.com"
}
```

`base_domain` is the wildcard parent domain Caddy serves (e.g.
`my-mac.tunnels.example.com`). `servers.primary.host` is the SSH
host clients connect to when establishing the reverse tunnel — the
same machine the Caddy listens on.

The Cloudflare DNS token (for Caddy TLS) lives in
`/etc/klan1-tunnel/caddy-dns-token` (mode 0600). The admin
passwords live in `/etc/klan1-tunnel/dashboard-auth.json` (mode
0600, bcrypt-hashed).

The server uses `/var/lib/klan1-tunnel/state.json` for the live
tunnel state and `/etc/klan1-tunnel/api-keys.json` for the API
keys store. The systemd unit is hardened (ProtectSystem=strict,
ReadWritePaths=`/var/lib/klan1-tunnel /etc/klan1-tunnel /etc`).

## Limits

- 10 ports (65081-65090). First-fit allocation.
- Default TTL: 24h, extended by heartbeats (sliding).
- Key TTL: configurable per key (1h .. 1y, or never).
- Sweeper runs every 30s.

## Development

`server/klan1-tunnel-server.py` is a single-file Python 3.9+
service with no third-party deps beyond PyJWT and (optionally)
bcrypt. The admin and client share no auth model: the admin
endpoints are gated by HTTP Basic; the client endpoints are
gated by JWT.

The installer is a single bash script that talks to the API
directly with `curl` + `python3 -c "import json, ..."`. No
external tools required beyond `bash`, `curl`, `python3`, and
`ssh` (autossh optional).

## License

Pick something appropriate; not yet specified.
