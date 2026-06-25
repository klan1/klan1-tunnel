# klan1-tunnel

Self-hosted ngrok-like tunneling service. Runs on your own servers,
exposes your local HTTP/SOCKS proxies or TCP services through stable
public URLs.

## Why

* ngrok's free tier is rate-limited and the URL changes every session.
* cloudflared quick tunnels block `CONNECT` — useless for HTTP/SOCKS proxies.
* You already have public-facing servers with bandwidth to spare. This
  repo is the glue: it adds tunnel-as-a-service on top of any fleet you
  point it at (via `config/fleet.example.json`).

## What it does

* Allocates a port in `65082-65300` on a server from your fleet (219 slots).
* Opens a reverse SSH tunnel from your device's local proxy to that port.
* Tracks active tunnels in a tiny Python API server (stdlib-only, no deps).
* Provides a web dashboard for live status.
* Auto-restarts the tunnel on drop (custom wrapper, not the broken `autossh`).
* Registers with the API by default; falls back to standalone if API is unreachable.

## Quick start

### One-liner (any device with `curl` + `bash` + `ssh`)

```bash
curl -sSL https://raw.githubusercontent.com/klan1/klan1-tunnel/main/install.sh | \
    bash -s -- --name mac --server primary
```

This installs `klan1-tunnel` to `~/.local/bin/` and starts a tunnel named `mac` against `ws1`.

**Need a deeper walk-through?** See [`INSTALL.md`](./INSTALL.md) — it
covers the "empty Chromebook" / "no pip" / "no Homebrew" edge cases
and what to do when the one-liner fails on a fresh box.

### From a clone

```bash
git clone https://github.com/klan1/klan1-tunnel.git
cd klan1-tunnel/client
./klan1-tunnel.sh start --name mac --server primary
```

## Usage

```text
klan1-tunnel.sh <action> [options]

Actions:
  start                  open a tunnel (default)
  stop --name NAME       stop a specific tunnel
  status                 list active tunnels

Options for start:
  --name NAME            tunnel name (e.g. mac, iphone, chromebook)
  --server ALIAS         target server alias (configured in
                         fleet.json; auto-fallback through server_order)
  --api-url URL          register with the klan1-tunnel-server API
                         (default: from fleet.json → https://<your-api-host>)
  --port PORT            local proxy port (default: same as remote)
  --remote-port PORT     explicit remote port (default: 65082)
  --protocol http|socks5 proxy protocol (default: http)
  --no-proxy             tunnel raw TCP (expose a local web server)
  --local-target H:P     target when --no-proxy (default: 127.0.0.1:80)
  --ttl SECONDS          TTL (default: 86400)
  --egress-ip IP         override the detected egress IP
```

## Architecture

```
┌────────────────┐     SSH-R-tunnel     ┌──────────────────────────┐
│  Your device   │                       │  Tunnel server (primary)  │
│  ─────────     │                       │  ─────────               │
│  pproxy  ──────┼────── :65082 ─────────▶  sshd listener 65082     │
│  (socks5/http) │                       │  public IP: <fleet.host> │
└────────────────┘                       └──────────────────────────┘
                                                       ▲
                                                       │
                                              klan1-tunnel-server
                                              (port 65500, systemd)
                                                       │
                                                       ▼
                                              web dashboard
                                              <fleet.api_url>/
```

## Endpoints (API server)

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/` | web dashboard |
| `GET`  | `/healthz` | liveness |
| `GET`  | `/api/v1/tunnels` | list active tunnels |
| `GET`  | `/api/v1/free-port` | suggest next free port |
| `POST` | `/api/v1/tunnels` | register a tunnel, get a token |
| `POST` | `/api/v1/tunnels/<token>/heartbeat` | extend TTL |
| `DELETE` | `/api/v1/tunnels/<token>` | release the tunnel |

## Files

```
klan1-tunnel/
├── README.md                  this file
├── LICENSE                    MIT
├── install.sh                 one-liner installer (curl | bash)
├── client/
│   └── klan1-tunnel.sh        the client (bash, no deps)
└── server/
    ├── klan1-tunnel-server.py     the API server (Python 3.6+ stdlib)
    └── klan1-tunnel-server.service    systemd unit
```

## Server setup (one-time, on `ws1`)

```bash
# 1. Firewall: open 65500 (API) and 65082-65300 (tunnels)
iptables -I INPUT -p tcp --dport 65500 -j ACCEPT
iptables -I INPUT -p tcp --dport 65082:65300 -j ACCEPT

# 2. SSH: enable GatewayPorts so reverse tunnels bind on 0.0.0.0
sed -i 's/^#GatewayPorts no/GatewayPorts yes/' /etc/ssh/sshd_config
systemctl reload sshd

# 3. Install the server
cp server/klan1-tunnel-server.py /usr/local/bin/
cp server/klan1-tunnel-server.service /etc/systemd/system/
mkdir -p /var/lib/klan1-tunnel /var/log/klan1-tunnel
systemctl daemon-reload
systemctl enable --now klan1-tunnel-server
```

## Client setup (per device)

```bash
# macOS (with Homebrew) — uses the system python3; no pyenv, no pipx
brew install autossh
python3 -m pip install --user 'klan1-pproxy>=3.0.1'

# Chromebook / Linux — one-shot install script handles everything
curl -sSL https://raw.githubusercontent.com/klan1/klan1-tunnel/main/install.sh | bash -s -- --name mydevice
```

> **pproxy version**: the `klan1-pproxy` package on PyPI is the
> maintained fork of `pproxy` and requires Python 3.9 or newer. It
> works with the python3 that ships with macOS Sonoma+ and
> Debian 12 / Ubuntu 22.04+. We install into a per-user virtualenv at
> `~/.local/share/klan1-tunnel/venv` (or `--user` site-packages on
> macOS) so the system Python stays clean. See [`INSTALL.md`](./INSTALL.md)
> for the full walk-through.

## License

MIT.
