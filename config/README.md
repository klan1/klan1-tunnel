# config/

Fleet configuration files for klan1-tunnel.

## Files

- **`fleet.example.json`** — Public template with placeholders. Safe to commit.
- **`fleet.local.json`** — Your real fleet. **Never commit this file.**
  Listed in `.gitignore`.

## Where the runtime looks for `fleet.local.json`

The code searches these paths in order and uses the first one found:

1. `$KLAN1_FLEET_CONFIG` (env var, override path)
2. `$HOME/.klan1-tunnel/fleet.json`
3. `$HOME/.config/klan1-tunnel/fleet.json`
4. `/etc/klan1-tunnel/fleet.json`

## Quick start

```bash
# 1. Copy the example to your local config
cp config/fleet.example.json ~/.klan1-tunnel/fleet.json

# 2. Edit with your real server IPs and user
$EDITOR ~/.klan1-tunnel/fleet.json

# 3. Verify the JSON is valid
python3 -m json.tool < ~/.klan1-tunnel/fleet.json > /dev/null && echo "OK"
```

## Schema

See `fleet.example.json` itself for a fully-annotated example. The fields are:

| Path | Required | Description |
|---|---|---|
| `servers.<alias>.host` | yes | Hostname or IP of the SSH server |
| `servers.<alias>.user` | yes | SSH username |
| `servers.<alias>.port` | no | SSH port (default: 22) |
| `server_order` | yes | Aliases in priority order; first reachable is used |
| `api.url` | yes | Public URL of the klan1-tunnel API server |
| `subdomains.<N>` | no | Map of subdomain number → SSH port (default 1..10 → 65081..65090) |
| `base_domain` | no | Base for the FQDN (default `tunels.<your-domain>`) |

## Why is this split out?

The repo is meant to be safe to publish. Hardcoding your own IPs, users,
SSH ports, etc. into the source would leak your network topology. By
reading them from a per-host JSON file instead, the same code works for
anyone — they just fill in their own `fleet.local.json`.