# MIGRATION — v1 → v2

Step-by-step playbook for cutting over a live `klan1-tunnel`
server from v1 to v2. The 2 versions are **not** designed to
coexist: the cutover is "big-bang" by design (decided 2026-07-02).
Do not try to run both at once.

This document is the **ops runbook**. The v2 install from
scratch is in `INSTALL.md`. The v2 contract (endpoints, file
layout, etc.) is in `README.md` and `STATUS.md`. This document
is the *bridge* between the two.

---

## When you need this

You have a v1 server running with active tunnels, and you want
to move to v2 with minimal downtime.

---

## What changes

| | v1 | v2 |
|---|---|---|
| Auth | Whitelist of devices in `devices.json` | API key in `api-keys.json` |
| Client onboarding | Admin adds device to `devices.json` | Admin creates API key, hands to client |
| Client install | `install.sh --subdomain 1 --user X --port 22` | `install.sh --device-id macbook --api-key kt1_xxx` |
| Tunnel identifier | `1.tunnels.example.com` (number) | `macbook.tunnels.example.com` (device name) |
| Caddy config | 10 hardcoded vhosts in `/etc/caddy/Caddyfile` | One wildcard + import of dynamic slice |
| Server endpoints | `POST /api/v1/tunnels` with `subdomain` | `POST /api/v1/devices/<id>/provision` |
| Dashboard form | subdomain dropdown | (no form — use admin page) |
| Release | TTL heartbeat OR `POST /dashboard/release` | TTL heartbeat, key revoke, OR `POST /api/v1/tunnels/<token>` |

---

## Pre-flight (T-30 min)

1. **Read `STATUS.md` end-to-end.** It has the full v2 contract,
   the file layout, the pitfalls, and the escalation paths.
2. **Read `INSTALL.md` end-to-end.** It walks the v2 admin
   install from scratch. This document is the *migration*
   version (assumes v1 is already there).
3. **Snapshot the state.** On the server:
   ```sh
   sudo cp /var/lib/klan1-tunnel/state.json /var/lib/klan1-tunnel/state.json.v1.backup-$(date +%Y%m%d-%H%M%S)
   sudo cp /etc/klan1-tunnel/devices.json /etc/klan1-tunnel/devices.json.v1.backup-$(date +%Y%m%d-%H%M%S) 2>/dev/null
   sudo cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.v1.backup-$(date +%Y%m%d-%H%M%S)
   sudo journalctl -u klan1-tunnel-server -n 200 --no-pager > /var/tmp/klan1-v1-last-journal.log
   ```
4. **Notify users.** Send the announcement 30 min ahead. Include:
   - Time of the cutover
   - Expected downtime: ~5 min (just the Caddy reload + server restart)
   - What they need to do: nothing immediately. After the cutover,
     re-run the installer with their `device_id` + new `api_key`.
   - The new install one-liner (see step 6 below).
5. **Make sure the new code is on the server but not yet running.**
   You'll deploy it during the cutover window.

---

## The cutover (T+0)

The cutover has 3 phases. Total downtime per phase is noted.
**Don't run the next phase until the previous one is clean.**

### Phase 1 — Deploy the v2 code (0 min downtime, dual-mode)

The server can run v1 + v2 in parallel as long as the v2
endpoints are added but the v1 endpoints still work. The
`break` happens in the `BREAKING(server): cutover v1 → v2`
commit (commit 9 in the commit log). Before that, both flows
are live.

1. On the server, pull the new code (or curl it):
   ```sh
   sudo systemctl stop klan1-tunnel-server
   sudo curl -sSL --max-time 15 \
     "https://raw.githubusercontent.com/klan1/klan1-tunnel/<sha>/server/klan1-tunnel-server.py" \
     -o /usr/local/bin/klan1-tunnel-server.py
   sudo chmod 755 /usr/local/bin/klan1-tunnel-server.py
   sudo systemctl start klan1-tunnel-server
   ```
2. Confirm both flows work:
   - `curl -sS http://127.0.0.1:65500/healthz` returns `{"ok": true}`
   - `curl -sS http://127.0.0.1:65500/api/v1/tunnels` returns the
     v1 list (the old tunnels should still be there)
   - The admin password is in the journal (it was generated
     on first boot; if the server was already running, the
     password is in `/etc/klan1-tunnel/dashboard-auth.json`):
     ```sh
     sudo cat /etc/klan1-tunnel/dashboard-auth.json
     ```
3. Create an API key for each active tunnel owner:
   ```sh
   curl -sS -u "admin:<password>" -X POST \
     -H "Content-Type: application/json" \
     -d '{"name":"<owner-name>","ttl_seconds":"30d"}' \
     http://127.0.0.1:65500/api/v1/keys
   ```
   Save the `secret` field. **The secret is shown exactly once.**
4. Send each owner their new credentials: `device_id` (same as
   their old tunnel name) + `api_key` (the secret from step 3) +
   the install one-liner:
   ```sh
   curl -sSL https://raw.githubusercontent.com/klan1/klan1-tunnel/main/install.sh | \
     bash -s -- \
     --device-id <device_id> \
     --api-url https://api.<base_domain> \
     --api-key <api_key>
   ```
5. Wait for users to re-provision. As they re-provision under
   v2, you'll see:
   - The state.json grows new entries with `api_key_id` set.
   - The Caddy slice `/etc/caddy/Caddyfile.klan1-tunnel` grows
     new vhosts.
   - The old v1 entries (no `api_key_id`) shrink as users
     move over.
6. **Do not** force anyone to move. Both flows can coexist for
   as long as the BREAKING commit hasn't been deployed. Let
   users migrate at their own pace.

### Phase 2 — Cut over the Caddyfile (~30 sec downtime)

Once all (or most) tunnels are running under v2, switch the
Caddyfile from the hardcoded 10 vhosts to a wildcard + dynamic
slice.

1. On the server, edit `/etc/caddy/Caddyfile` to replace the
   10 hardcoded `1..10.tunnels.example.com` blocks with a
   wildcard:
   ```caddyfile
   api.tunnels.example.com {
       tls {
           dns cloudflare {file=/etc/klan1-tunnel/caddy-dns-token}
       }
       reverse_proxy 127.0.0.1:65500
   }

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
2. **Backup the Caddyfile first** so you can roll back:
   ```sh
   sudo cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.pre-v2-$(date +%Y%m%d-%H%M%S)
   ```
3. Validate:
   ```sh
   sudo caddy validate --config /etc/caddy/Caddyfile
   ```
   Should print `Valid configuration`. If not, restore from
   backup and STOP. Do not reload.
4. Reload:
   ```sh
   sudo caddy reload --config /etc/caddy/Caddyfile
   ```
   Caddy reloads gracefully (no dropped connections for the
   3 vhosts that survived the edit).
5. Smoke test:
   - `curl -sS https://api.tunnels.example.com/healthz` — should
     return 200.
   - Pick one v2 tunnel: `curl -sS https://<device>.tunnels.example.com/`
     should reach the device's `localhost:8080`.
   - Pick a v1 tunnel (if any users haven't migrated): it
     should still work, served from the hardcoded vhost.

### Phase 3 — Deploy the BREAKING commit (5-15 min downtime)

This commit removes the v1 code paths. The v1 endpoints stop
responding. The v1 dashboard form is gone. The v1 `subdomain`
form field is gone.

**Plan this for a low-traffic window.** Even though the v1
endpoints are unreachable for new tunnels (the Caddy hardcoded
vhosts are gone), if any user is still running an old client,
their tunnel will start to fail at this point.

1. Deploy the breaking commit:
   ```sh
   sudo systemctl stop klan1-tunnel-server
   sudo curl -sSL --max-time 15 \
     "https://raw.githubusercontent.com/klan1/klan1-tunnel/<sha-of-breaking-commit>/server/klan1-tunnel-server.py" \
     -o /usr/local/bin/klan1-tunnel-server.py
   sudo chmod 755 /usr/local/bin/klan1-tunnel-server.py
   sudo systemctl start klan1-tunnel-server
   ```
2. The server starts. There's no v1 state to migrate — the
   `state.json` is already in v2 format (the v1 entries were
   re-provisioned as v2 entries by the clients). The
   `devices.json` file is just ignored now (it can stay on
   disk, harmless, or you can delete it: `sudo rm /etc/klan1-tunnel/devices.json`).
3. Smoke test:
   - `curl -sS http://127.0.0.1:65500/healthz` returns ok.
   - `curl -sS http://127.0.0.1:65500/api/v1/tunnels` returns
     only v2 entries (have `api_key_id` set).
   - `curl -sS -i http://127.0.0.1:65500/api/v1/devices`
     returns 410 Gone (the v1 whitelist endpoint is gone).
   - The dashboard at `https://api.tunnels.example.com/`
     shows the v2 dashboard (no subdomain form).
4. Watch the journal for 5 min:
   ```sh
   sudo journalctl -u klan1-tunnel-server -f
   ```
   Look for: errors, repeated 404s, sweeper removing "ghost"
   tunnels. All should be quiet.

### Phase 4 — Clean up

1. Archive the v1 backups (they should be on disk for at
   least 30 days in case you need to roll back):
   ```sh
   sudo cp /var/lib/klan1-tunnel/state.json.v1.backup-* /var/tmp/
   sudo cp /etc/klan1-tunnel/devices.json.v1.backup-* /var/tmp/ 2>/dev/null
   sudo cp /etc/caddy/Caddyfile.v1.backup-* /var/tmp/
   ```
2. After 30 days, if nothing broke, delete them.
3. The `devices.json` file (if you didn't already) is safe
   to delete:
   ```sh
   sudo rm /etc/klan1-tunnel/devices.json
   ```
4. The `SUBDOMAIN_PORTS` mapping is gone from the code, so
   no port allocation is hardcoded anymore. Ports are
   first-fit in 65081-65090.

---

## Rollback

If something breaks at any phase, here's how to roll back.

### Rollback from Phase 1 (v2 deployed, no BREAKING commit)

```sh
sudo systemctl stop klan1-tunnel-server
# Replace with the previous SHA (the last v1+ v2 dual-mode SHA)
sudo curl -sSL --max-time 15 \
  "https://raw.githubusercontent.com/klan1/klan1-tunnel/<prev-sha>/server/klan1-tunnel-server.py" \
  -o /usr/local/bin/klan1-tunnel-server.py
sudo chmod 755 /usr/local/bin/klan1-tunnel-server.py
sudo systemctl start klan1-tunnel-server
```

The state.json doesn't change. v1 tunnels resume. Users that
already re-provisioned under v2 keep their v2 tunnels (the
v1+v2 dual-mode server accepts both).

### Rollback from Phase 2 (Caddyfile switched to wildcard)

```sh
sudo cp /etc/caddy/Caddyfile.pre-v2-<timestamp> /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile
sudo caddy reload --config /etc/caddy/Caddyfile
```

Caddy is back to the 10 hardcoded vhosts. ~30 sec downtime
during the reload.

### Rollback from Phase 3 (BREAKING commit deployed)

This is the riskiest. The v1 code is gone from the server
file. To roll back, you need the previous SHA AND you need
to manually migrate the v2-only state back to v1 format.

1. **Stop the server.**
2. **Roll back the server file** to the previous SHA (the
   v1+v2 dual-mode one):
   ```sh
   sudo systemctl stop klan1-tunnel-server
   sudo curl -sSL --max-time 15 \
     "https://raw.githubusercontent.com/klan1/klan1-tunnel/<prev-sha>/server/klan1-tunnel-server.py" \
     -o /usr/local/bin/klan1-tunnel-server.py
   sudo chmod 755 /usr/local/bin/klan1-tunnel-server.py
   ```
3. **Convert v2 state back to v1.** The difference is:
   - v2 entries have an `api_key_id` field. v1 entries don't.
   - v2 entries are keyed by `token` (the new style). v1
     entries were keyed by `device_id` in the `devices.json`
     whitelist, not in `state.json`. Actually, `state.json`
     has always used tokens as keys. So the rollback is just:
     - The v2 tunnels in `state.json` are reachable with their
       tokens via the JWT in their `device_id` claim.
     - The v1 server doesn't know about those JWTs (it
       expects whitelisted devices).
   - **Practical rollback**: re-issue v1 devices for each
     active v2 tunnel. Create `devices.json` with one entry
     per active v2 tunnel's `device_id` (which is the tunnel
     name). Then ask the user to re-run the old install.
   - This is annoying. **Don't roll back from Phase 3 unless
     you have to.** It's easier to fix forward.
4. **Restart the server.** v1 tunnels that were preserved in
   the v1 backup should resume.

If you find yourself needing to roll back from Phase 3, the
fastest path is to **fix forward**: re-create the API key for
the affected user, hand them a new `api_key`, ask them to
re-run `install.sh`. This is the same flow as Phase 1 step 4.

---

## Smoke tests (run these after every phase)

After Phase 1:
- [ ] `curl http://127.0.0.1:65500/healthz` → 200
- [ ] `curl http://127.0.0.1:65500/api/v1/tunnels` → list with both v1 and v2 entries
- [ ] Old client still works: their tunnel responds on `<num>.tunnels.example.com`
- [ ] New client works: provision with `install.sh` succeeds

After Phase 2:
- [ ] `curl https://api.tunnels.example.com/healthz` → 200
- [ ] `curl https://<v2-device>.tunnels.example.com/` → 200 (reaches localhost:8080)
- [ ] Old `curl https://1.tunnels.example.com/` → 200 (still works from the hardcoded vhost)
- [ ] Caddy live config has both the 10 hardcoded vhosts AND the new wildcard

After Phase 3:
- [ ] `curl http://127.0.0.1:65500/api/v1/devices` → 410 Gone
- [ ] `curl http://127.0.0.1:65500/api/v1/tunnels` → only v2 entries
- [ ] Dashboard form has no `subdomain` field
- [ ] No v1 client can create a new tunnel (no endpoint for it)
- [ ] All v2 clients still work
- [ ] No errors in `journalctl -u klan1-tunnel-server -f` for 5 min

---

## Common pitfalls

- **The Cloudflare token is wrong.** Caddy will silently fail
  to issue certs. Check `sudo journalctl -u caddy -n 20 --no-pager`
  for `cloudflare: ... unauthorized` or similar.
- **The wildcard isn't pointing to your server.** DNS resolves
  `*.tunnels.example.com` to the wrong IP. Test with
  `dig +short test.tunnels.example.com` — it should return
  your server's IP. The wildcard must be `cf-proxied: false`
  (grey cloud, direct) — NOT orange-proxied. See `STATUS.md`
  section 12.
- **The systemd unit is missing `/etc` from `ReadWritePaths`.**
  Provisions will fail with `useradd: cannot lock /etc/group`.
  Fix the unit, `systemctl daemon-reload`, restart. See
  `STATUS.md` section 11 pitfall 1 (and commit `6f97cae`).
- **bcrypt is not installed.** The server falls back to
  pbkdf2_sha256 silently. This is fine, but the API key hashes
  will be in pbkdf2 format, not bcrypt. Both are valid.
- **The state.json mtime reload is too slow.** The server
  reloads state from disk every time it touches it. If you
  edit state.json directly with `vi` and the mtime doesn't
  change, the server won't see your edit. Touch the file:
  `touch /var/lib/klan1-tunnel/state.json`.
- **The Caddy slice file (`/etc/caddy/Caddyfile.klan1-tunnel`)
  doesn't exist yet.** That's normal on a fresh install — the
  server creates it on the first provision. After the first
  provision, it should exist and have 1+ vhost blocks.
- **The user runs `install.sh` and gets a 409 name_in_use.**
  They already have a tunnel under the same `device_id`.
  They need to release first (`curl -X DELETE ...` with their
  JWT) and then re-provision. Or pick a different `device_id`.

---

## FAQ

**Q: Can I do Phase 1 and Phase 3 in the same deploy?**
A: No. Phase 1 adds the v2 code (no breakage). Phase 3
removes the v1 code (breaking). They're 8 commits apart in
the log. Always do them in separate deploys with a phase
in between for users to migrate.

**Q: Do I need to notify users for Phase 2?**
A: Not strictly, but a heads-up is nice. The Caddy reload is
graceful, but DNS cache and connection re-use can cause
hiccups. ~30 sec of intermittent 502s is possible.

**Q: What if a user is on vacation during the cutover?**
A: After Phase 3, their old v1 tunnel is unreachable. They
can re-provision with v2 at any time (the new install will
work; they just need a valid API key). The admin can pre-
create the API key and email it to them.

**Q: Can I skip Phase 1 and go straight to Phase 3?**
A: Only if you have zero active v1 tunnels. Otherwise users
will lose access. If you have active v1 tunnels, do all 3
phases.

**Q: Can I keep the v1 code for a "fallback" deploy?**
A: You can, but the v1 code path is dead — there's no
endpoint that uses it, no client that calls it, and the
Caddy hardcoded vhosts are gone. Keeping the code in the
repo is just dead weight.

**Q: How long does the whole migration take?**
A: Phase 1 deploy is ~2 min. The user-migration window is
hours to days, depending on how fast your users re-run the
installer. Phase 2 is ~2 min of editing + reload. Phase 3
is ~2 min of deploy + smoke test. The "active" time for
the admin is ~10 min. The "wait" time is the user-migration
window.

---

## Reference: the dual-mode window

Between Phase 1 and Phase 3, the server has BOTH v1 and v2
endpoints live. The relevant commit boundaries are:

- **Commit `bcaecef`** — first v2 endpoint (POST /api/v1/devices/<id>/provision)
  is added. From this commit onward, new tunnels can be created
  via the v2 flow.
- **Commit `0088992`** — v2 client (`install.sh`) is added.
  From this commit onward, clients can install via the v2 flow.
- **Commit `53259ab`** — docs (commit 8). No code change.
- **Commit `<sha-of-9>`** — the BREAKING commit. v1 endpoints
  removed. v1 dashboard form removed. v1 `subdomain` field
  gone.

The window is from `bcaecef` (or whenever you deploy the v2
server) to `<sha-of-9>` (whenever you deploy the BREAKING
commit). During that window, the server is dual-mode and
clients can be on either side.
