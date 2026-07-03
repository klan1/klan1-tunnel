# STATUS — handoff to OpenCode

This is a snapshot of the project state as of 2026-07-02 13:50 UTC.
The user is moving development to OpenCode on a different machine.
Two virgin machines are available for end-to-end testing.

This document assumes the reader has **no prior context** of the
project. Read it from top to bottom before doing anything.

---

## 1. What this project is

`klan1-tunnel` is a self-hosted reverse-tunnel service. The server
runs on a public VPS; clients (developer laptops) run the installer
and get a public URL like `https://<name>.tunnels.example.com/`
that reverse-proxies to `localhost:8080` on the client machine.

It works in two pieces:
- `server/klan1-tunnel-server.py` — the API + dashboard server.
  Single-file Python 3.9+ service. No third-party deps except
  PyJWT and (optionally) bcrypt.
- `install.sh` — bash installer that the client runs.

Caddy is used as the TLS terminator in front of the server. Caddy
is NOT in this repo — it's installed at the OS level on the VPS.

---

## 2. What's deployed right now

### Server: ai1 (the test VPS)

- Public IP: `<your-server-public-ip>` (SSH on the standard port)
- Web/SSH-tunnel port range: `65081-65090` (10 ports)
- API endpoint (Caddy TLS): `https://api.tunnels.example.com/`
- Server is currently **stopped / running, no tunnels active**
  (the user manually cleared state.json, deleted all `tunnel-*`
  unix users, deleted all `.key` files, removed the Caddyfile
  slice, and removed the 10 hardcoded `1..10.tunnels.example.com`
  vhosts from the Caddyfile on 2026-07-02 ~13:45).
- Caddy is currently serving only: `api.tunnels.example.com`,
  `opencode.klan1.net`, `vscode.klan1.net`.
- Admin user: `admin`. First-boot password was logged to journal:
  ```
  sudo journalctl -u klan1-tunnel-server | grep -A2 'FIRST BOOT'
  ```
  If the server has been restarted, the password is the one in
  `/etc/klan1-tunnel/dashboard-auth.json` (mode 0600, bcrypt).
  Read the file or re-read the journal to confirm.
- API keys store: `/etc/klan1-tunnel/api-keys.json` (still has
  the keys from earlier verification — ok to delete for a clean
  start, or just create new ones from the admin page).
- The server code at `/usr/local/bin/klan1-tunnel-server.py` on
  ai1 is at commit `03898dc` (the last v1+v2 dual-mode commit).
  Commits `53259ab` (docs), `b635bd5` (BREAKING cutover), and
  `268cb73` (MIGRATION.md) are on `main` but have NOT been
  deployed to ai1. The next `systemctl restart` will pick them
  up — and at that point, the v1 endpoints stop responding.
  If you have active v1 tunnels on ai1, follow MIGRATION.md
  phase 1+2+3 before deploying.

### Repo state

- Working dir: `~/Develop/GitHub/testing/klan1-tunnel`
- Branch: `main`
- Remote: `github.com:klan1/klan1-tunnel` (note: `klan1` not
  `klan1-tunnel` as user; the repo is the `klan1-tunnel` repo
  under the `klan1` org).
- HEAD on main is `268cb73`. Working tree has:
  - Modified (NOT committed): none as of 2026-07-03.
  - Untracked: `config/fleet.json` (the local fleet file,
    `.gitignore`d on purpose; never to be committed).

### Server file on ai1

- `/usr/local/bin/klan1-tunnel-server.py` is the same as the
  working tree's `server/klan1-tunnel-server.py` (i.e. includes
  commits 1-7 from PLAN-V2). Deploy new commits with:
  ```
  sudo systemctl stop klan1-tunnel-server
  sudo curl -sSL --max-time 15 \
    "https://raw.githubusercontent.com/klan1/klan1-tunnel/<sha>/server/klan1-tunnel-server.py" \
    -o /usr/local/bin/klan1-tunnel-server.py
  sudo chmod 755 /usr/local/bin/klan1-tunnel-server.py
  sudo systemctl daemon-reload
  sudo systemctl start klan1-tunnel-server
  ```

---

## 3. What is done (commits 1-7 of PLAN-V2)

All 7 commits are pushed to `main` and deployed on ai1.

| # | SHA | Commit |
|---|---|---|
| 1 | `f95c7dc` | `feat(server): APIKeyStore for v2 (bcrypt + pbkdf2 fallback)` |
| 2 | `3cef248` | `feat(server): v2 auth — login with api_key + keys CRUD` |
| 3 | `bcaecef` | `feat(server): v2 provision endpoint — POST /api/v1/devices/<id>/provision` |
| 3b | `f43d4f3` | `fix(server): APIKeyStore reloads from disk on mtime change` |
| 4 | `e0b0902` | `feat(server): Caddyfile generator + dry-run + reload` |
| 5 | `25520dd` | `feat(server): sweeper cleans up expired + revoked-key tunnels` |
| 5b | `a4130ff` | `fix(server): State._maybe_reload` |
| 6 | `0088992` | `feat(client): installer v2 — device-id + api-key flow` |
| 7 | `4308dd5` | `feat(server): basicauth + admin dashboard` |
| 7b | `62599e9` | `fix(server): move GET /api/v1/keys from do_POST to do_GET` |
| 7c | `03898dc` | `fix(server): move DELETE /api/v1/keys/<id> from do_POST to do_DELETE` |
| 8 | `53259ab` | `docs: STATUS.md + v2 README/INSTALL + PLAN-V2 banner` |
| 9 | `b635bd5` | `BREAKING(server): cutover v1 → v2 — remove subdomain + whitelist paths` |
| 10 | `268cb73` | `docs: MIGRATION.md playbook for the v1 → v2 cutover` |

The full commit-by-commit plan is in `PLAN-V2.md` (now tracked
in the repo as a historical reference).

### What works end-to-end on ai1 (verified)

- `POST /api/v1/auth/login` with `{device_id, api_key}` returns
  a JWT.
- `POST /api/v1/devices/<device_id>/provision` with the JWT
  returns the full bundle (token, ssh_user, ssh_port, fqdn,
  private_key, ssh_command, expires_at).
- The server creates the `tunnel-<port>` unix user, writes the
  SSH keypair, regenerates the Caddyfile slice, and reloads
  Caddy (graceful, no dropped connections).
- The client (using the bundle from the response) can run the
  SSH reverse-tunnel.
- `POST /api/v1/tunnels/<token>/heartbeat` extends the TTL.
- The sweeper (thread daemon, 30s) detects expired tunnels and
  revoked keys, removes the unix user, deletes the key files,
  regenerates Caddyfile, and reloads.
- `GET /dashboard/admin` (with basicauth) shows the admin page.
- `GET /api/v1/keys` (with basicauth) lists keys.
- `POST /api/v1/keys` (with basicauth) creates a key.
- `DELETE /api/v1/keys/<id>` (with basicauth) revokes a key.

### What is NOT done

- **Commit 8** (docs): DONE at `53259ab`. `README.md`,
  `INSTALL.md`, and `deploy/SIGUIENTE-PASO.md` are all in
  English, scrubbed of real infrastructure (all references use
  `<placeholder>` form). Uses placeholders like
  `tunnels.example.com`, `api.tunnels.example.com`.
- **Commit 9** (BREAKING cutover v1 → v2): DONE at `b635bd5`.
  All v1 code paths removed: `SUBDOMAIN_PORTS`, the
  `provision_tunnel_user` / `_provision_tunnel_user_locked`
  functions, the v1 `/api/v1/devices` GET endpoint, the v1
  `/api/v1/tunnels` POST handler, the v1 `/dashboard/provision`
  POST handler, the `subdomain` / `server_alias` / `egress_ip`
  form fields, the v1 path of the `Auth` class (whitelist
  check), the `devices_path` argument and the `load_devices()`
  method, and the `/api/v1/free-port` endpoint. The caddy slice
  generation was inlined from a `CaddyManager` module class
  into `Handler` methods (`generate_caddyfile`,
  `_caddy_validate`, `_caddy_reload`, `_caddy_reload_for_tunnel`).
  The sweeper was wrapped in a `Sweeper` class (same logic,
  same 30s interval). `Auth.login` was renamed to
  `issue_token_for_key` (more descriptive of what it does in
  v2). Server went from 2639 → 2356 lines (-283).
- **Commit 10** (migration runbook): DONE at `268cb73`.
  `MIGRATION.md` at the repo root. Step-by-step runbook for
  cutting over a live v1 server to v2, with rollback
  procedures, smoke tests, and FAQ.

---

## 4. Architecture (the contract)

```
                 Caddy (TLS terminator, DNS-01 challenge)
                 api.tunnels.example.com  →  127.0.0.1:65500
                 <name>.tunnels.example.com →  127.0.0.1:<remote_port>
                            │
                            ▼
                klan1-tunnel-server (127.0.0.1:65500)
                 ├─ State: /var/lib/klan1-tunnel/state.json
                 ├─ APIKeyStore: /etc/klan1-tunnel/api-keys.json
                 ├─ CaddyManager: writes /etc/caddy/Caddyfile.klan1-tunnel
                 │                 runs `caddy validate` then `caddy reload`
                 ├─ Sweeper: thread daemon, 30s, checks expiry + revokes
                 └─ Dashboard: / (anonymous), /dashboard/admin (basicauth)
                            ▲
                            │ HTTPS (HTTPS is Caddy's job)
                            │
                 Client (install.sh on user's laptop)
                  ├─ 1× curl /api/v1/auth/login → JWT
                  ├─ 1× curl /api/v1/devices/<id>/provision → bundle
                  └─ autossh -R <port>:localhost:8080
                     + heartbeat daemon (every 30s)
```

### Why this design

- The identifier of the tunnel IS the device's name (e.g. `macbook`).
  No more `1..10` subdomain numbers.
- The installer does NOT prompt the user for SSH host/user/port/key.
  All that comes from the server, in the provision bundle.
- The API is the single source of truth for: who has a tunnel,
  what port they're on, what key issued them, when they expire.
- Caddy reloads dynamically (graceful, not wildcard) — adding a
  tunnel doesn't cut the existing 9.
- Heartbeat-driven sweep: if the client stops sending heartbeats
  for >24h, or if the API key is revoked, the tunnel is cleaned
  up automatically.
- 10-port hard cap (65081-65090). First-fit allocation.
- API keys are independent of devices. One key can issue N tunnels.
  Keys have TTL (24h / 7d / 30d / 90d / 1y / never).
- Big-bang cutover (no v1/v2 coexistence). v1 is dead on ai1.

---

## 5. File layout

```
klan1-tunnel/
├── README.md                      (v2, EN, scrubbed; not yet committed)
├── INSTALL.md                     (v2, EN, scrubbed; not yet committed)
├── PLAN-V2.md                     (the 10-commit plan; not yet committed)
├── install.sh                     (v2 installer; committed at 0088992)
├── deploy/
│   ├── SIGUIENTE-PASO.md          (placeholder; not yet committed)
│   └── klan1-tunnel-server.service (systemd unit template; committed)
├── server/
│   └── klan1-tunnel-server.py     (v2 server with commits 1-7; committed)
├── config/
│   └── fleet.json                 (local; .gitignore'd; do not commit)
└── STATUS.md                      (this file)
```

### Where the server reads/writes on ai1

- `--state` → `/var/lib/klan1-tunnel/state.json` (the live
  tunnels map; `State` class; reloads on mtime change)
- `--key-dir` → `/etc/klan1-tunnel/` (used for SSH keypairs
  and the Caddy slice output path)
  - `/etc/klan1-tunnel/api-keys.json` (APIKeyStore)
  - `/etc/klan1-tunnel/dashboard-auth.json` (admin basicauth)
  - `/etc/klan1-tunnel/caddy-dns-token` (Cloudflare token for
    Caddy TLS, mode 0600)
  - `/etc/klan1-tunnel/<device_id>.key` and `.key.pub` (per-tunnel
    SSH keypair, generated by the server)
- `/etc/caddy/Caddyfile.klan1-tunnel` — the Caddy slice the
  server regenerates. Imported from the main Caddyfile.
- `/var/lib/klan1-tunnel/users/tunnel-<port>/` — per-tunnel unix
  user home, contains `.ssh/authorized_keys`.

### Fleet file (`~/.klan1-tunnel/fleet.json` on the client)

The client writes a fleet.json after install. Used by the
installer for re-provisioning or update flows. The format:

```json
{
  "device_id": "macbook",
  "api_url": "https://api.tunnels.example.com",
  "tunnels": [
    {
      "name": "macbook",
      "remote_port": 65081,
      "fqdn": "macbook.tunnels.example.com",
      "token": "<token>",
      "ssh_user": "tunnel-65081",
      "ssh_host": "api.tunnels.example.com",
      "ssh_port": 22,
      "private_key_path": "~/.klan1-tunnel/id_ed25519_tunnel-65081",
      "local_port": 8080,
      "expires_at": "2026-07-03T..."
    }
  ]
}
```

---

## 6. Endpoints (full reference)

All endpoints return JSON. Errors are `{"error": "<code>"}`.

### Client (JWT auth)

```
POST /api/v1/auth/login
  body: {device_id, api_key}
  resp: 200 {token, device_id, key_id, expires_in}
  err:  401 invalid_credentials
        400 missing_field
        503 auth_disabled

POST /api/v1/devices/<device_id>/provision
  headers: Authorization: Bearer <jwt>
  body: {local_port?}
  resp: 200 {
    device_id, tunnel_user, tunnel_port, fqdn,
    ssh_host, ssh_user, ssh_port, private_key,
    ssh_command, expires_at, token, caddy_reload_ok
  }
  err:  401 invalid_jwt
        409 name_in_use            (already provisioned, must release first)
        503 no_free_ports
        400 invalid_device_id      (regex: ^[a-z][a-z0-9-]{0,30}[a-z0-9]$)
        503 caddy_reload_failed    (state was NOT mutated; safe to retry)

POST /api/v1/tunnels/<token>/heartbeat
  headers: Authorization: Bearer <jwt>
  body: {}
  resp: 200 <tunnel entry>
  err:  401 invalid_jwt
        404 token_not_found

DELETE /api/v1/tunnels/<token>
  headers: Authorization: Bearer <jwt>
  resp: 200 {ok: true, ...}
  err:  401, 404

GET /api/v1/tunnels
  resp: 200 {tunnels: [...], count: N}

GET /api/v1/free-port
  resp: 200 {port, range: [lo, hi]}

GET /api/v1/devices         (v1 — REMOVED in commit 9, returns 410 Gone)
  resp: 410 {"error": "v1_removed",
             "use": "/dashboard/admin to create an API key"}
```

### Admin (HTTP Basic)

```
GET    /api/v1/keys
POST   /api/v1/keys    body: {name, ttl_seconds?|ttl_str?}
DELETE /api/v1/keys/<id>
GET    /dashboard/admin  (HTML)
```

Basicauth user is `admin`. Password is in
`/etc/klan1-tunnel/dashboard-auth.json` (bcrypt) or in the
journal from the first boot.

`ttl_seconds` accepts either an int (seconds) or a string like
`"24h"`, `"7d"`, `"30d"`, `"90d"`, `"1y"`, `"never"`.

`POST /api/v1/keys` returns 201 with `{id, secret, name,
created_at, expires_at, ...}`. **The secret is shown exactly
once.** Store it immediately.

`DELETE` is idempotent. Returns 200 with `revoked: true` if the
key existed, 404 if not.

### Health

```
GET /healthz
  resp: 200 {ok: true, time: "<ISO8601>"}
```

---

## 7. Hardening + system

The systemd unit (template at `deploy/klan1-tunnel-server.service`):

```ini
[Service]
Type=simple
ExecStart=/usr/bin/python3 /usr/local/bin/klan1-tunnel-server.py \
    --port 65500 --bind 127.0.0.1 \
    --state /var/lib/klan1-tunnel/state.json \
    --key-dir /etc/klan1-tunnel \
    --port-lo 65081 --port-hi 65090 --default-ttl 86400
Restart=always
User=root
Group=root
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/lib/klan1-tunnel /etc/klan1-tunnel /etc
```

Note `ReadWritePaths=/etc`: the server runs as root and needs to
`useradd` / `groupadd`, which writes to `/etc/passwd`, `/etc/group`,
`/etc/shadow`, `/etc/gshadow`. If you drop the `/etc` from
ReadWritePaths, **provisions will fail with `useradd: cannot
lock /etc/group`**. This is a real bug we hit on ai1
(fix commit: `6f97cae`). Do not regress it.

The server also needs `/usr/sbin/useradd`, `/usr/sbin/groupadd`,
`/usr/sbin/userdel`, `/usr/sbin/groupdel`, `ssh-keygen` on PATH.
On ai1 (Debian 12 docker container) these are at
`/usr/sbin/`. The systemd unit uses `/usr/bin/python3` which
is fine, but the PATH for child processes (useradd etc.) is
set by the unit — verify with
`sudo systemctl show klan1-tunnel-server | grep -i path`.

Caddy is installed separately, as a system service. The klan1-tunnel
server does NOT manage Caddy's lifecycle — it only writes the
slice file and runs `caddy validate` + `caddy reload` over the
shell.

---

## 8. Decisions made (and why)

These are decisions that the user (Alejo) made explicitly. Do
not re-litigate them without checking in.

| # | Decision | Why |
|---|---|---|
| D1 | Device ID regex `^[a-z][a-z0-9-]{0,30}[a-z0-9]$` | Avoids invalid FQDNs. Max 32 chars. |
| D2 | Re-provision same device_id → 409 | Explicit > magic. Forces release first. |
| D3 | Port = first free in 65081-65090 (ascending) | Simplifies. First-fit. |
| D4 | API key hash = bcrypt if available, fallback pbkdf2_sha256 | Standard. Server has `HAVE_BCRYPT` flag. |
| D5 | API key TTL configurable (24h..1y, or never) | Allows natural rotation. |
| D6 | Private key returned inline in 200 | HTTPS encrypts the channel. Simple. |
| D7 | Caddy reload dynamic, not wildcard | User chose this. Dry-run before reload. |
| D8 | Implicit release on missing heartbeat | Server already has `default-ttl=86400`. |
| D9 | Big-bang cutover (no v1/v2 coexistence) | User chose this. |
| D10 | v1 endpoints kept through commit 8, removed in commit 9 (DONE at b635bd5) | For smooth rollout. |
| D11 | Use `Optional[int]`, not `int \| None` | Python 3.9 compat. |
| D12 | Hash format: `pbkdf2_sha256$<iters>$<salt_b64>$<dk_hex>` | Passlib-style, self-describing. |
| D13 | ad-hoc verification with `tempfile.mkstemp` | Compliance with the agent's system rule to verify local disk changes. |

---

## 9. Verification checklist for OpenCode

When the user runs you through verification with their 2 virgin
machines, do this in order. The user explicitly wants you to
**do the verification, not just describe it**. Show real
command output.

### 9.1 Server-side end-to-end

1. `ssh ai1-root` — confirm sudo works without password.
2. `sudo systemctl status klan1-tunnel-server` — active (running).
3. `curl -sS http://127.0.0.1:65500/healthz` — returns `{"ok": true}`.
4. `curl -sS -u "admin:<password>" http://127.0.0.1:65500/api/v1/keys`
   — returns list of keys (even if empty, the auth must work).
5. Create a fresh key:
   ```
   curl -sS -u "admin:<password>" -X POST \
     -H "Content-Type: application/json" \
     -d '{"name":"verify-opencoder","ttl_seconds":"7d"}' \
     http://127.0.0.1:65500/api/v1/keys
   ```
   Save the `secret` field.
6. `curl -sS https://api.tunnels.example.com/healthz` — returns
   `{"ok": true}` (this confirms Caddy TLS works).

### 9.2 Client-side: install on a virgin machine

1. On the client (NOT ai1), run the installer:
   ```
   curl -sSL https://raw.githubusercontent.com/klan1/klan1-tunnel/main/install.sh \
     | bash -s -- --device-id <name> --api-url https://api.tunnels.example.com --api-key <secret>
   ```
2. Verify `~/.klan1-tunnel/fleet.json` exists and has a tunnel entry.
3. Verify the SSH reverse-tunnel is running (`ps aux | grep ssh`).
4. Verify heartbeat is running (`ps aux | grep heartbeat`,
   or check `~/.klan1-tunnel/heartbeat.log`).
5. Hit the public URL from another machine:
   ```
   curl -i https://<name>.tunnels.example.com/
   ```
   Should return whatever `localhost:8080` on the client is
   serving.

### 9.3 Edge cases to test (the user has 2 virgin machines = 1 server + 1 client is the obvious setup, but 2 clients is also fine for testing 2 tunnels concurrently)

- 2 clients at the same time → both should get a port (1..10).
- Provision the same `device_id` twice → 409 second time.
- Revoke the API key of an active tunnel → sweeper (within 30s)
  should: remove from state, delete unix user, delete key file,
  regenerate Caddyfile, reload Caddy.
- Stop the client (kill autossh) → server sees missing heartbeat
  → after 24h the sweeper reaps it. For testing, set a short TTL
  in the server args (e.g. `--default-ttl 60`) to verify the
  expiry path without waiting a day.
- Invalid device_id (uppercase, with `_`, etc.) → 400.
- Login with bad api_key → 401.

### 9.4 Health of the deployment

- The user already manually verified the Caddyfile has only
  `api.tunnels.example.com`, `opencode.klan1.net`, `vscode.klan1.net`
  (3 hosts). The 10 hardcoded `1..10.tunnels.example.com` are gone.
- The slice `/etc/caddy/Caddyfile.klan1-tunnel` does NOT exist
  (the user deleted it). It will be regenerated by the next
  provision.
- `state.json` is `{"ports_reserved": [], "tunnels": {}}` (empty).

---

## 10. Status of PLAN-V2.md

**All 10 commits DONE.** Plan complete.

| # | SHA | Title | Status |
|---|---|---|---|
| 1 | `f95c7dc` | `feat(server): APIKeyStore for v2` | done |
| 2 | `3cef248` | `feat(server): v2 auth — login with api_key + keys CRUD` | done |
| 3 | `bcaecef` | `feat(server): v2 provision endpoint` | done |
| 3b | `f43d4f3` | `fix(server): APIKeyStore reloads on mtime change` | done |
| 4 | `e0b0902` | `feat(server): Caddyfile generator + dry-run + reload` | done |
| 5 | `25520dd` | `feat(server): sweeper cleans up expired + revoked-key tunnels` | done |
| 5b | `a4130ff` | `fix(server): State._maybe_reload` | done |
| 6 | `0088992` | `feat(client): installer v2` | done |
| 7 | `4308dd5` | `feat(server): basicauth + admin dashboard` | done |
| 7b | `62599e9` | `fix(server): move GET /api/v1/keys to do_GET` | done |
| 7c | `03898dc` | `fix(server): move DELETE /api/v1/keys to do_DELETE` | done |
| 8 | `53259ab` | `docs: STATUS.md + v2 README/INSTALL + PLAN-V2 banner` | done |
| 9 | `b635bd5` | `BREAKING(server): cutover v1 → v2 — remove subdomain + whitelist paths` | done |
| 10 | `268cb73` | `docs: MIGRATION.md playbook for the v1 → v2 cutover` | done |

The PLAN-V2.md file is kept in the repo (now tracked) as a
historical reference — the v2 plan from 2026-07-02.

---

## 11. Pitfalls and gotchas (learned the hard way)

1. **`write_file` blocks paths in `/var/folders/.../T/`**
   (the agent's safety guard). Use `terminal` with `cat` or
   `mktemp` to write to tempdirs, or write to a regular path.
2. **The shell guard detects `&` in comments as a logical
   operator** (false positive). Use `set +H` (disable history
   expansion) at the top of a bash script, or avoid `&` in
   strings.
3. **macOS APFS does not respect `os.chmod` on `tempfile`**
   — file permission tests that expect 0o600 will fail. Run
   real permission tests on ai1 (Linux), not on macOS.
4. **bcrypt is NOT installed on the agent's macOS by default.**
   The server has a pbkdf2_sha256 fallback (200000 iters,
   16-byte salt, 32-byte dk). On ai1 (Debian 12), bcrypt IS
   available — the server uses it. Both code paths are
   exercised; tests should cover both.
5. **The agent's macOS has Python 3.9.6** — cannot use
   `int | None` (3.10+). Use `Optional[int]`.
6. **The `State` and `APIKeyStore` classes reload from disk
   on mtime change** (commits 3b and 5b). This is intentional
   so admin tools and the dashboard can edit state.json /
   api-keys.json without restarting the server. If you change
   this behavior, the sweeper will be stale.
7. **Caddy reload is `graceful`** (no dropped connections).
   If `caddy validate` fails, the server does NOT touch the
   Caddyfile and returns 503 — provision does not mutate
   state in that case.
8. **Bash escape hell on ai1.** The user's hermes agent has
   had repeated issues with `ssh ai1-root "command with & or
   | or 'inside' it"`. The reliable pattern is: write a
   Python script locally, `scp` it to `/tmp/` on ai1, `ssh
   ai1-root "python3 /tmp/script.py"`. Use `subprocess.run`
   with a list, not `shell=True`, to avoid quote pain.
9. **Commit messages.** Use `git -c user.name=<your-git-user> -c
   user.email=<your-git-email> commit -m "..."` — pick the
   identity that matches your `~/.gitconfig` for this repo.
10. **No `git pull --rebase` on the user's macOS.** They
    prefer to fetch + rebase manually if needed, but the
    branch has been stable for 11 commits, no merge issues
    expected.

---

## 12. Environment

- macOS: 26.5.1 (the agent's host)
- Debian 12 docker container: ai1 (the server)
- Python on ai1: 3.12.3
- Python on the agent's mac: 3.9.6
- Caddy: built from xcaddy with the cloudflare DNS module
- Domain: tunnels.example.com (admin: Cloudflare)
- Wildcard: `*.tunnels.example.com` → A → <your-server-public-ip>
  (cf-proxied: false, "grey cloud" — direct, not orange)

---

## 13. What the user wants from OpenCode

The user said:
> "Voy a mover este porte to a OpenCode. Por favor documenta
> todo en MDs que pueda leer el y pueda continuar."

Translation: "I'm going to move this project to OpenCode. Please
document everything in MDs that it can read and continue."

So OpenCode is expected to:
1. Read `STATUS.md` (this file) to get the state.
2. Read `PLAN-V2.md` for the full plan (untracked, in working tree).
3. Read `README.md` and `INSTALL.md` for the user-facing contract.
4. Use the verification checklist in section 9 above as the test
   plan.
5. Use the pitfalls in section 11 to avoid the bugs we already
   hit.

The user has 2 virgin machines for testing. OpenCode should
propose the test plan and run it, not just describe it.

---

## 14. Contact / escalation

If something doesn't work as expected:
- Read the server log: `sudo journalctl -u klan1-tunnel-server -n 50 --no-pager`
- Read the state: `sudo cat /var/lib/klan1-tunnel/state.json | python3 -m json.tool`
- Read the Caddy log: `sudo journalctl -u caddy -n 50 --no-pager`
- Check the Caddy config: `curl -sS http://127.0.0.1:2019/config/ | python3 -m json.tool`
- Check the live vhosts: `curl -sS http://127.0.0.1:2019/config/apps/http/servers/srv0/routes`
  (or the helper in section 9.1)
- The user is on Telegram, reachable through the agent.
