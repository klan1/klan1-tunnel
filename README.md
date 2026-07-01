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

`klan1-tunnel` is a **client + server pair**. You run the client on every
device you want to expose, and the server (`klan1-tunnel-server`)
allocates a public port and tracks your active tunnels on one of your
own boxes. Before the client can do anything useful, the server has to
be installed and your device's `device_id` has to be on its whitelist.

The minimum flags are:

| Flag | What it does |
|---|---|
| `--name NAME` | Identifier of this tunnel in the dashboard (e.g. `mac`, `iphone`, `chromebook`) |
| `--subdomain N` | Which of the 10 pilot slots you want (`1`–`10`); each maps to a fixed remote port (e.g. `1.tunels.klan1.net` → `65081`) |

### First time on a new device — interactive

If `fleet.json` is **not** already on this machine, the installer will
ask for 5 values (SSH host/user/port, API URL, base domain) over the
terminal, save them to `~/.config/klan1-tunnel/fleet.json` (mode 600),
and continue:

```bash
curl -sSL https://raw.githubusercontent.com/klan1/klan1-tunnel/main/install.sh | \
    bash -s -- --name mac --subdomain 1
```

You'll be prompted for:

```text
SSH host of the tunnel server [e.g. ai1.example.com]: ai1.klan1.net
SSH user [j0hnd03]: j0hnd03
SSH port [22]: 65522
API base URL [https://api.<base_domain>]: https://api.tunels.klan1.net
Base domain for subdomains [tunels.klan1.net]: tunels.klan1.net
```

### Repeat / CI / non-interactive (`curl | bash` over SSH)

If stdin is **not** a TTY (e.g. `curl ... | bash` over SSH), the
installer will not prompt. Provide a pre-built `fleet.json`:

```bash
# 1. Build a fleet.json on a box with a TTY (one-time):
curl -sSL .../install.sh | bash -s -- --name bootstrap --subdomain 1

# 2. Copy the resulting file to the new box:
scp ~/.config/klan1-tunnel/fleet.json newbox:/tmp/fleet.json

# 3. Then on newbox, run non-interactively:
curl -sSL .../install.sh | \
    bash -s -- --name newbox --subdomain 2 --fleet /tmp/fleet.json --non-interactive
```

Or skip the install start-up entirely with `--no-start` and run
`klan1-tunnel start` yourself once you've inspected the install.

**Need a deeper walk-through?** See [`INSTALL.md`](./INSTALL.md) — it
covers the "empty Chromebook" / "no pip" / "no Homebrew" edge cases
and what to do when the one-liner fails on a fresh box.

### From a clone

```bash
git clone https://github.com/klan1/klan1-tunnel.git
cd klan1-tunnel/client
./klan1-tunnel.sh start --name mac --subdomain 1 \
    --api-url https://api.tunels.klan1.net --remote-port 65081
```

> The `client/klan1-tunnel.sh` script reads `fleet.json` from
> `~/.config/klan1-tunnel/fleet.json` or the path given by `--fleet`.
> The `install.sh` one-liner does the same search.

## Usage

```text
klan1-tunnel.sh <action> [options]

Actions:
  start                  open a tunnel (default)
  stop --name NAME       stop a specific tunnel
  status                 list active tunnels

Options for start:
  --name NAME            tunnel name (e.g. mac, iphone, chromebook)
  --subdomain N          which of the 10 pilot slots (1..10; maps to
                         a fixed remote port via fleet.json subdomains)
  --api-url URL          register with the klan1-tunnel-server API
                         (default: from fleet.json → https://<your-api-host>)
  --port PORT            local proxy port (default: same as remote)
  --remote-port PORT     explicit remote port (overrides --subdomain)
  --protocol http|socks5 proxy protocol (default: http)
  --no-proxy             tunnel raw TCP (expose a local web server)
  --local-target H:P     target when --no-proxy (default: 127.0.0.1:80)
  --ttl SECONDS          TTL (default: 86400)
  --egress-ip IP         override the detected egress IP
```

`install.sh` adds these on top:

```text
  --fleet PATH           path to a pre-built fleet.json (skips interactive prompt)
  --prefix DIR           where to drop the binary and venv (default: ~/.local)
  --no-start             install only; do not open the tunnel
  --skip-deps            don't install system packages
  --non-interactive      abort with a clear error if fleet.json is missing
                         (use this under `curl | bash` over SSH)
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
