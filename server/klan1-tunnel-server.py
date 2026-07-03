#!/usr/bin/env python3
"""
klan1-tunnel-server - Self-hosted ngrok-like API (v2, API-key based)

Tiny stdlib HTTP server. No external deps. Stores state in a JSON file.

v2 endpoints (the only ones that work — v1 is gone in commit 9):
  POST /api/v1/auth/login               {device_id, api_key} -> JWT
  POST /api/v1/auth/refresh             (Bearer JWT) -> new JWT bound to same key
  POST /api/v1/devices/<id>/provision   (Bearer JWT) -> bundle (token, ssh_user, port, key, fqdn, ...)
  POST /api/v1/tunnels/<token>/heartbeat (Bearer JWT) -> extend TTL
  DELETE /api/v1/tunnels/<token>        (Bearer JWT) -> release
  GET  /api/v1/tunnels                  list active tunnels (no auth)
  GET  /api/v1/free-port                suggest next free port in the range

  Admin (basicauth):
  GET    /api/v1/keys                   list API keys
  POST   /api/v1/keys                   {name, ttl} -> create key (secret shown once)
  DELETE /api/v1/keys/<id>              revoke a key
  GET    /dashboard/admin               (HTML) admin panel

  Health/UI:
  GET  /healthz                         liveness
  GET  /                                (HTML) tunnel table
  GET  /dashboard/ssh-command?token=...  (JSON) ssh command for a tunnel

v1 endpoints removed in commit 9 (return 410 Gone):
  GET  /api/v1/devices                  (whitelist — gone, use /dashboard/admin)
  POST /api/v1/tunnels                  (subdomain-based provision — gone)
  POST /dashboard/provision             (v1 form — gone, redirect to / with info flash)

Run:
  python3 klan1-tunnel-server.py --port 65500 --bind 127.0.0.1 \\
      --state /var/lib/klan1-tunnel/state.json \\
      --key-dir /etc/klan1-tunnel \\
      --port-lo 65081 --port-hi 65090 --default-ttl 86400

Author: klan1-tunnel contributors
"""

import argparse
import datetime
import http.server
import json
import os
import pwd
import re
import secrets
import shutil
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Optional

try:
    import jwt  # PyJWT
    from cryptography.hazmat.primitives import serialization
    HAVE_JWT = True
except ImportError:
    HAVE_JWT = False

DEFAULT_PORT_RANGE_START = 65082
DEFAULT_PORT_RANGE_END = 65300
DEFAULT_TTL_SECONDS = 86400  # 24h
DEFAULT_JWT_TTL_SECONDS = 30 * 24 * 3600  # 30 days
DEFAULT_JWT_ALGO = "RS256"

# Caddy integration. The server regenerates the klan1-tunnel slice of
# the Caddy config on every provision/release. The Caddyfile in /etc/caddy/
# imports this file (added in commit 9) so the running Caddy picks up
# changes via `caddy reload` (graceful, zero-downtime).
CADDY_BIN = "/usr/bin/caddy"
CADDY_TUNNELS_PATH = Path("/etc/caddy/Caddyfile.klan1-tunnel")
CADDY_DNS_TOKEN_PATH = Path("/etc/klan1-tunnel/caddy-dns-token")  # not committed
CADDY_DNS_PROVIDER = "cloudflare"
# Base domain for tunnel subdomains. Override per-deployment via the
# /etc/klan1-tunnel/fleet.json file (see config/fleet.example.json).
DEFAULT_BASE_DOMAIN = "tunnels.<your-domain>"

# ----------------------------------------------------------------------------
# Runtime config (loaded from /etc/klan1-tunnel/fleet.json if present)
# ----------------------------------------------------------------------------
def _load_runtime_config():
    """Read base_domain and api_host from fleet config if available."""
    cfg_paths = [
        os.environ.get("KLAN1_TUNNEL_CONFIG"),
        "/etc/klan1-tunnel/fleet.json",
        os.path.expanduser("~/.klan1-tunnel/fleet.json"),
    ]
    result = {"base_domain": DEFAULT_BASE_DOMAIN, "api_host": None}
    for p in cfg_paths:
        if p and os.path.isfile(p):
            try:
                with open(p) as f:
                    cfg = json.load(f)
                bd = cfg.get("base_domain") or cfg.get("subdomains", {}).get("base_domain")
                if bd:
                    result["base_domain"] = bd
                # API host = host of the primary server (server_order[0])
                servers = cfg.get("servers") or {}
                order = cfg.get("server_order") or []
                if order and servers:
                    primary = servers.get(order[0]) or {}
                    host = primary.get("host")
                    if host:
                        result["api_host"] = host
                break
            except Exception:
                pass
    return result


_RUNTIME = _load_runtime_config()
BASE_DOMAIN = _RUNTIME["base_domain"]
# Host clients SSH to for the reverse tunnel. Default placeholder
# points users at their fleet.json.
API_HOST = _RUNTIME["api_host"] or os.environ.get("KLAN1_TUNNEL_API_HOST") or "<your-api-server-host>"


def now_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso_utc(s: str) -> datetime.datetime:
    if not s:
        return None
    # Python 3.6 compatible ISO 8601 parser (datetime.fromisoformat is 3.7+,
    # strptime %z is 3.7+). We parse with regex and re-construct.
    m = re.match(
        r'^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})?$',
        s.strip()
    )
    if not m:
        return None
    y, mo, d, h, mi, s_ = (int(x) for x in m.groups()[:6])
    frac = m.group(7)
    tz = m.group(8)
    micro = 0
    if frac:
        # truncate/pad to 6 digits
        frac = (frac + "000000")[:6]
        micro = int(frac)
    dt = datetime.datetime(y, mo, d, h, mi, s_, micro)
    if tz == "Z" or tz is None:
        if tz == "Z":
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        # else naive — treat as UTC
        else:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
    else:
        sign = 1 if tz[0] == "+" else -1
        oh, om = int(tz[1:3]), int(tz[4:6])
        offset = datetime.timezone(sign * datetime.timedelta(hours=oh, minutes=om))
        dt = dt.replace(tzinfo=offset)
    return dt


def gen_token() -> str:
    return secrets.token_urlsafe(18)


# Serialize all useradd/groupadd calls behind a file lock so concurrent
# provisions don't trip "cannot lock /etc/group" on a fluke. Also makes
# the whole provision operation idempotent: if two dashboard clicks
# race, the second one waits for the first and reuses the result.
# Lives under /tmp because /var/lock may be read-only in containers.
_PROVISION_LOCK = Path("/tmp/klan1-tunnel-provision.lock")
TUNNELS_GROUP = "tunnel-users"
TUNNEL_SHELL = "/usr/local/bin/tunnel-shell.sh"
KEYS_BASE = Path("/etc/klan1-tunnel")
# Tunnel users' home lives under the data dir, NOT under /home, because the
# systemd unit sets ProtectHome=true (so /home is read-only to the server).
USERS_BASE = Path("/var/lib/klan1-tunnel/users")


def generate_keypair_ed25519() -> tuple[str, str]:
    """Generate a fresh ed25519 SSH keypair. Returns (private_openssh, public_openssh).

    The private key is returned in OpenSSH format (the modern portable format,
    not PEM). The public key is in OpenSSH single-line format (ssh-ed25519 AAAA...).
    """
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.hazmat.primitives import serialization

    sk = ed25519.Ed25519PrivateKey.generate()
    pk = sk.public_key()

    private_bytes = sk.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = pk.public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    )
    return private_bytes.decode("ascii"), public_bytes.decode("ascii")


class State:
    """Thread-safe in-memory state with JSON-file persistence.

    State shape:
    {
      "tunnels": {
        "<token>": {
          "name": "mac", "remote_port": 65082, "protocol": "http",
          "server_alias": "primary", "egress_ip": "1.2.3.4",
          "created_at": "...", "expires_at": "...",
          "last_heartbeat": "...", "ttl": 86400
        }
      },
      "ports_reserved": [65082, 65083, ...]
    }
    """

    def __init__(self, path: Path, port_lo: int, port_hi: int, default_ttl: int):
        self.path = path
        self.port_lo = port_lo
        self.port_hi = port_hi
        self.default_ttl = default_ttl
        self._lock = threading.RLock()
        self.data = {"tunnels": {}, "ports_reserved": []}
        self._mtime = 0.0  # last-loaded mtime of state.json (for _maybe_reload)
        self._load()
        # sweep expired on startup
        self._sweep_expired()

    def _maybe_reload(self):
        """Reload state from disk if the file's mtime changed.

        Symmetric to APIKeyStore._maybe_reload. Allows external
        processes (e.g. a test harness, or a future admin tool) to
        modify state.json and have the server pick up the changes on
        the next read. Without this, the in-memory copy gets stale
        and the sweeper misses changes made directly to the file.
        """
        try:
            if not self.path.exists():
                return
            mtime = self.path.stat().st_mtime
            if mtime > self._mtime:
                self._load()
                self._mtime = mtime
        except OSError:
            pass

    def _load(self):
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
                self._mtime = self.path.stat().st_mtime
                if "tunnels" not in self.data:
                    self.data["tunnels"] = {}
                if "ports_reserved" not in self.data:
                    self.data["ports_reserved"] = []
            except Exception as e:
                print(f"[warn] could not load state ({e}); starting empty", file=sys.stderr)

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True))
        tmp.replace(self.path)
        # Update cached mtime so our own write doesn't immediately re-read
        self._mtime = self.path.stat().st_mtime

    def _sweep_expired(self):
        """Remove tunnels whose expires_at has passed. Also remove
        tunnels whose api_key was revoked (if the APIKeyStore is
        available on the Handler — we look it up lazily because
        State is constructed before the APIKeyStore in main()).

        For each removed tunnel, also:
          - drop the unix user (`tunnel-<port>`)
          - drop the per-device private key
          - trigger a Caddy reload so the vhost goes away

        Idempotent: safe to call repeatedly. Returns the list of
        (token, name, port) tuples that were removed (useful for
        logging and tests)."""
        # Reload in case someone wrote directly to state.json
        # since the last _save (e.g. a test patching expires_at).
        self._maybe_reload()
        with self._lock:
            now = datetime.datetime.now(datetime.timezone.utc)
            expired = []
            for tok, t in list(self.data["tunnels"].items()):
                exp = parse_iso_utc(t.get("expires_at", ""))
                if exp and exp < now:
                    expired.append(tok)
            removed = []
            for tok in expired:
                port = self.data["tunnels"][tok].get("remote_port")
                name = self.data["tunnels"][tok].get("name", "")
                del self.data["tunnels"][tok]
                if port and port in self.data["ports_reserved"]:
                    self.data["ports_reserved"].remove(port)
                removed.append((tok, name, port))
            if expired:
                print(f"[sweep] removed {len(expired)} expired tunnels", file=sys.stderr)
                self._save()
        # Side effects happen OUTSIDE the state lock so we don't hold
        # it while running userdel/subprocess. The cleanup is best-effort:
        # the tunnel is gone from state, which is what matters; the unix
        # user + Caddy vhost are just hygiene.
        for tok, name, port in removed:
            self._cleanup_tunnel_side_effects(name, port)
        return removed

    def _sweep_revoked_keys(self, api_keys: "APIKeyStore") -> list:
        """Drop tunnels whose API key has been revoked. Returns the
        list of (token, name, port) tuples removed."""
        if api_keys is None:
            return []
        self._maybe_reload()
        with self._lock:
            to_remove = []
            for tok, t in list(self.data["tunnels"].items()):
                # Tunnel records don't store the key_id; we rely on
                # last_heartbeat being stamped by the client, but the
                # v1 path never set last_heartbeat. The cheapest signal
                # is the 'egress_ip' field, but that's also unreliable.
                # For now we don't track key_id in the tunnel record
                # (we could add it — see commit 5 TODO below). This
                # method is therefore a no-op for the v1 records.
                #
                # Workaround for v2: the provision path now sets
                # t['api_key_id'] when the JWT was bound to a key
                # (added in this commit, see reserve_with_egress_ip).
                kid = t.get("api_key_id")
                if kid and not api_keys.is_valid(kid):
                    to_remove.append(tok)
            removed = []
            for tok in to_remove:
                port = self.data["tunnels"][tok].get("remote_port")
                name = self.data["tunnels"][tok].get("name", "")
                del self.data["tunnels"][tok]
                if port and port in self.data["ports_reserved"]:
                    self.data["ports_reserved"].remove(port)
                removed.append((tok, name, port))
            if removed:
                print(f"[sweep] removed {len(removed)} tunnels with revoked keys", file=sys.stderr)
                self._save()
        for tok, name, port in removed:
            self._cleanup_tunnel_side_effects(name, port)
        return removed

    def _cleanup_tunnel_side_effects(self, name: str, port: int):
        """Best-effort: drop the unix user, drop the per-device key file,
        and trigger a Caddy reload. Never raises."""
        # Find the Handler instance to call its cleanup methods.
        # State is decoupled from Handler in the import graph (State
        # doesn't know about Handler), so we go through the running
        # thread's Handler. We use a class-level hook for tests.
        handler = _get_active_handler()
        if handler is not None:
            try:
                handler._remove_tunnel_user(port)
            except Exception as e:
                print(f"[sweep] _remove_tunnel_user({port}) failed: {e}", file=sys.stderr)
            try:
                # Drop the per-device key file
                keyfile = KEYS_BASE / f"{name}.key"
                if keyfile.exists():
                    keyfile.unlink()
            except Exception as e:
                print(f"[sweep] unlink {name}.key failed: {e}", file=sys.stderr)
            try:
                handler._caddy_reload_for_tunnel(name, port, "remove")
            except Exception as e:
                print(f"[sweep] caddy reload after remove of {name}:{port} failed: {e}", file=sys.stderr)

    def sweep_periodic(self, stop_event: threading.Event):
        """Run _sweep_expired + _sweep_revoked_keys every 30s. The
        reference to api_keys is set on the State instance by main()
        after both objects exist (avoids the bootstrap-order trap)."""
        while not stop_event.is_set():
            time.sleep(30)
            try:
                self._sweep_expired()
                if getattr(self, "_api_keys", None) is not None:
                    self._sweep_revoked_keys(self._api_keys)
            except Exception as e:
                print(f"[sweep] error: {e}", file=sys.stderr)

    def next_free_port(self, requested: int = None) -> int:
        with self._lock:
            taken = set(self.data["ports_reserved"])
            if requested and requested not in taken and self.port_lo <= requested <= self.port_hi:
                return requested
            for p in range(self.port_lo, self.port_hi + 1):
                if p not in taken:
                    return p
            return None

    def reserve(self, name: str, requested_port: int, protocol: str,
                server_alias: str, egress_ip: str, ttl: int,
                api_key_id: Optional[str] = None) -> dict:
        with self._lock:
            # Reject if name already has a live tunnel. We return the full token
            # so the client can re-adopt the tunnel (heartbeat-friendly).
            # cleanly (e.g. after a daemon restart on the device).
            for tok, t in self.data["tunnels"].items():
                if t.get("name") == name and t.get("server_alias") == server_alias:
                    exp = parse_iso_utc(t.get("expires_at", ""))
                    if exp and exp > datetime.datetime.now(datetime.timezone.utc):
                        return {
                            "ok": False, "error": "name_in_use",
                            "existing": {"token": tok, **t}
                        }

            port = self.next_free_port(requested_port)
            if not port:
                return {"ok": False, "error": "no_free_ports",
                        "range": [self.port_lo, self.port_hi]}

            token = gen_token()
            created = datetime.datetime.now(datetime.timezone.utc)
            expires = created + datetime.timedelta(seconds=ttl)
            entry = {
                "token": token,
                "name": name,
                "remote_port": port,
                "protocol": protocol,
                "server_alias": server_alias,
                "egress_ip": egress_ip or "unknown",
                "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "last_heartbeat": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "ttl": ttl,
            }
            if api_key_id:
                entry["api_key_id"] = api_key_id
            self.data["tunnels"][token] = entry
            if port not in self.data["ports_reserved"]:
                self.data["ports_reserved"].append(port)
            self._save()
            return {"ok": True, "token": token, **entry}

    def heartbeat(self, token: str, extend_ttl: int = None) -> dict:
        with self._lock:
            t = self.data["tunnels"].get(token)
            if not t:
                return {"ok": False, "error": "unknown_token"}
            now = datetime.datetime.now(datetime.timezone.utc)
            t["last_heartbeat"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            if extend_ttl:
                exp = parse_iso_utc(t["expires_at"])
                base = max(exp, now) if exp else now
                t["expires_at"] = (base + datetime.timedelta(seconds=extend_ttl)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ")
                t["ttl"] = extend_ttl
            else:
                # sliding: extend expires_at by t['ttl'] from now if it would expire soon
                exp = parse_iso_utc(t["expires_at"])
                if exp and (exp - now).total_seconds() < t["ttl"] * 0.5:
                    t["expires_at"] = (now + datetime.timedelta(seconds=t["ttl"])).strftime(
                        "%Y-%m-%dT%H:%M:%SZ")
            self._save()
            return {"ok": True, **t}

    def release(self, token: str) -> dict:
        with self._lock:
            t = self.data["tunnels"].pop(token, None)
            if not t:
                return {"ok": False, "error": "unknown_token"}
            port = t.get("remote_port")
            if port and port in self.data["ports_reserved"]:
                self.data["ports_reserved"].remove(port)
            self._save()
            return {"ok": True, "released": t}

    def list(self) -> list:
        with self._lock:
            return list(self.data["tunnels"].values())

    def get(self, token: str) -> dict:
        with self._lock:
            return self.data["tunnels"].get(token)


# ----------------------------------------------------------------------------
# Auth layer (JWT RS256, API-key-bound, v2 only)
# ----------------------------------------------------------------------------

class Auth:
    """JWT-based authentication, v2 (API-key-bound).

    State files (all owned by root, mode 0600):
      - jwt-private.pem: RSA private key, signs JWTs
      - jwt-public.pem:  RSA public key, validates JWTs

    Tokens are RS256 JWTs with claims:
      sub:    device_id
      name:   human-readable name (from API key)
      key_id: API key that minted this JWT (REQUIRED in v2)
      iat:    issued-at (unix)
      exp:    expires-at (unix)

    v1 device-whitelist auth was removed in commit 9. Any token
    without a 'key_id' claim is rejected as legacy.
    """

    def __init__(self, key_dir: Path, ttl: int = DEFAULT_JWT_TTL_SECONDS):
        if not HAVE_JWT:
            raise RuntimeError("PyJWT not installed; run: pip install pyjwt cryptography")
        self.key_dir = key_dir
        self.ttl = ttl
        self.priv_path = key_dir / "jwt-private.pem"
        self.pub_path = key_dir / "jwt-public.pem"
        if not self.priv_path.exists() or not self.pub_path.exists():
            raise RuntimeError(f"JWT keys missing in {key_dir}; run: openssl genrsa -out jwt-private.pem 2048 && openssl rsa -in jwt-private.pem -pubout -out jwt-public.pem")
        self.priv_pem = self.priv_path.read_bytes()
        self.pub_pem = self.pub_path.read_bytes()

    def issue_token_for_key(self, device_id: str, key_id: str,
                            key_name: str = "") -> str:
        """Sign and return a JWT bound to a specific API key.

        The 'key_id' claim is mandatory in v2 — it lets the sweeper
        trace which key created a tunnel and reaps the tunnel if the
        key is later revoked."""
        now = int(time.time())
        payload = {
            "sub": device_id,
            "name": key_name or device_id,
            "key_id": key_id,
            "iat": now,
            "exp": now + self.ttl,
        }
        return jwt.encode(payload, self.priv_pem, algorithm=DEFAULT_JWT_ALGO)

    def validate_token(self, token: str) -> dict:
        """Return the claims dict if valid, or an error string.

        v2 only: every JWT must carry a 'key_id' claim. Tokens
        without one are legacy v1 (device-whitelist) tokens and
        are rejected — no backwards compat.
        """
        try:
            claims = jwt.decode(token, self.pub_pem, algorithms=[DEFAULT_JWT_ALGO])
        except jwt.ExpiredSignatureError:
            return {"error": "expired"}
        except jwt.InvalidTokenError as e:
            return {"error": "invalid", "detail": str(e)}
        device_id = claims.get("sub")
        if not device_id:
            return {"error": "no_sub"}
        if not claims.get("key_id"):
            # v1 token (no key_id claim). Reject — v1 is gone.
            return {"error": "legacy_v1_token"}
        return {"ok": True, "device_id": device_id, "claims": claims}


# ----------------------------------------------------------------------------
# API key model (v2)
# ----------------------------------------------------------------------------
# A long-lived credential, independent of any device, that the owner
# generates from the dashboard and hands to a client. The client uses
# it once to mint a short-lived JWT, and the JWT is what /provision and
# /heartbeat actually authenticate with.
#
# Storage: only the hash is persisted. The cleartext secret is shown
# to the user exactly once (at creation time), then discarded — same
# model as GitHub PATs / AWS access keys.
#
# File: <key_dir>/api-keys.json  (mode 0600, owner root)
# Schema:
#   {
#     "keys": {
#       "<key_id>": {
#         "id":          "kt1_a1b2c3d4e5f6",   # public, displayed in dashboard
#         "name":        "macbook install",     # human, owner-supplied
#         "prefix":      "kt1_",                # identifies the type/version
#         "hash":        "<bcrypt or pbkdf2>",  # secret is never stored
#         "hash_algo":   "bcrypt" | "pbkdf2_sha256",
#         "created_at":  "2026-07-01T18:30:15Z",
#         "expires_at":  "2026-10-01T18:30:15Z" | null,   # null = never
#         "revoked_at":  null | "...",
#         "last_used_at": null | "...",
#         "tunnels_created": 0
#       }
#     }
#   }
# ----------------------------------------------------------------------------

try:
    import bcrypt  # type: ignore
    HAVE_BCRYPT = True
except ImportError:
    HAVE_BCRYPT = False


def _hash_secret(secret: str) -> tuple[str, str]:
    """Return (hash, algo). Uses bcrypt if available, else pbkdf2_sha256."""
    if HAVE_BCRYPT:
        # bcrypt has a 72-byte input limit. Take first 72 bytes; that's
        # more than enough for our 32-char random base62 secret.
        h = bcrypt.hashpw(secret.encode("utf-8")[:72], bcrypt.gensalt(rounds=12))
        return h.decode("ascii"), "bcrypt"
    # Fallback: pbkdf2_sha256 with 200_000 iterations, 32-byte output.
    import hashlib, secrets
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, 200_000, dklen=32)
    return f"pbkdf2_sha256$200000${salt.hex()}${dk.hex()}", "pbkdf2_sha256"


def _verify_secret(secret: str, stored: str, algo: str) -> bool:
    if algo == "bcrypt" and HAVE_BCRYPT:
        try:
            return bcrypt.checkpw(secret.encode("utf-8")[:72], stored.encode("ascii"))
        except Exception:
            return False
    if algo == "pbkdf2_sha256":
        import hashlib
        try:
            _, iters_s, salt_hex, dk_hex = stored.split("$")
            iters = int(iters_s)
            salt = bytes.fromhex(salt_hex)
            expected = bytes.fromhex(dk_hex)
            dk = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, iters, dklen=len(expected))
            # constant-time compare
            import hmac
            return hmac.compare_digest(dk, expected)
        except Exception:
            return False
    return False


class APIKeyStore:
    """Long-lived API credentials. Independent of any device."""

    PREFIX = "kt1_"
    # 22 chars of base62 ≈ 130 bits entropy. Plenty.
    RANDOM_LEN = 22

    def __init__(self, key_dir: Path):
        self.key_dir = key_dir
        self.path = key_dir / "api-keys.json"
        self._lock = threading.RLock()
        self._mtime = 0.0  # last-loaded mtime of the on-disk file
        self._load()

    def _maybe_reload(self):
        """Reload from disk if the file's mtime changed.

        This handles the case where an admin process (e.g. the dashboard
        or a separate `python3 -c` script) modifies api-keys.json while
        the server is running. Without this, the server's in-memory
        copy gets stale and verify() / list() return wrong data.
        """
        try:
            if not self.path.exists():
                return
            mtime = self.path.stat().st_mtime
            if mtime > self._mtime:
                self._load()
                self._mtime = mtime
        except OSError:
            pass

    def _load(self):
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
                self._mtime = self.path.stat().st_mtime
            except Exception:
                self.data = {"keys": {}}
        else:
            self.data = {"keys": {}}
        if "keys" not in self.data or not isinstance(self.data["keys"], dict):
            self.data = {"keys": {}}

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)
        # Update our cached mtime so we don't immediately re-read our own write
        self._mtime = self.path.stat().st_mtime

    @staticmethod
    def _new_random() -> str:
        import secrets, string
        alphabet = string.ascii_letters + string.digits
        return "".join(secrets.choice(alphabet) for _ in range(APIKeyStore.RANDOM_LEN))

    def create(self, name: str, ttl_seconds: Optional[int] = None) -> dict:
        """Create a new API key. Returns the cleartext secret in `secret`
        (one-time display); only the hash is persisted."""
        with self._lock:
            key_id = self.PREFIX + self._new_random()
            secret = self.PREFIX + self._new_random()
            h, algo = _hash_secret(secret)
            now = now_utc()
            exp = None
            if ttl_seconds is not None and ttl_seconds > 0:
                from datetime import datetime, timedelta, timezone
                exp = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
            self.data["keys"][key_id] = {
                "id": key_id,
                "name": name or key_id,
                "prefix": self.PREFIX,
                "hash": h,
                "hash_algo": algo,
                "created_at": now,
                "expires_at": exp,
                "revoked_at": None,
                "last_used_at": None,
                "tunnels_created": 0,
            }
            self._save()
            return {
                "id": key_id,
                "secret": secret,
                "name": name or key_id,
                "prefix": self.PREFIX,
                "created_at": now,
                "expires_at": exp,
            }

    def verify(self, secret: str) -> Optional[dict]:
        """Look up a key by its cleartext secret. Returns the key record
        (without the hash) on success, None on failure.

        Constant-time-ish: we walk all keys and try every hash. With
        <100 keys this is fine; for larger fleets, add an index by
        prefix (we already have `PREFIX` and could shard by 2 chars)."""
        if not secret or not secret.startswith(self.PREFIX):
            return None
        with self._lock:
            self._maybe_reload()
            now_ts = int(time.time())
            for key_id, rec in self.data["keys"].items():
                if rec.get("revoked_at"):
                    continue
                if not _verify_secret(secret, rec["hash"], rec.get("hash_algo", "bcrypt")):
                    continue
                # Check expiry
                exp = rec.get("expires_at")
                if exp:
                    from datetime import datetime, timezone
                    try:
                        exp_dt = datetime.strptime(exp, "%Y-%m-%dT%H:%M:%SZ").replace(
                            tzinfo=timezone.utc
                        )
                        if int(exp_dt.timestamp()) < now_ts:
                            continue
                    except Exception:
                        # If the expiry string is malformed, treat the key as
                        # valid (the owner can re-create it). Failing closed
                        # here would lock out everyone on a single typo.
                        pass
                # Update last_used_at (best-effort, don't fail the verify on write error)
                rec["last_used_at"] = now_utc()
                try:
                    self._save()
                except Exception:
                    pass
                return {k: v for k, v in rec.items() if k != "hash"}
            return None

    def revoke(self, key_id: str) -> bool:
        with self._lock:
            rec = self.data["keys"].get(key_id)
            if not rec:
                return False
            rec["revoked_at"] = now_utc()
            self._save()
            return True

    def list(self) -> list:
        with self._lock:
            self._maybe_reload()
            out = []
            for rec in self.data["keys"].values():
                out.append({k: v for k, v in rec.items() if k != "hash"})
            return out

    def is_valid(self, key_id: str) -> bool:
        """A key is valid if it exists, is not revoked, and not expired."""
        with self._lock:
            self._maybe_reload()
            rec = self.data["keys"].get(key_id)
            if not rec or rec.get("revoked_at"):
                return False
            exp = rec.get("expires_at")
            if not exp:
                return True
            from datetime import datetime, timezone
            try:
                exp_dt = datetime.strptime(exp, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc
                )
                return int(exp_dt.timestamp()) > int(time.time())
            except Exception:
                # If the expiry string is malformed, treat as valid (don't
                # lock out a key on a typo in expires_at).
                return True

    def inc_tunnels_created(self, key_id: str) -> None:
        with self._lock:
            rec = self.data["keys"].get(key_id)
            if rec:
                rec["tunnels_created"] = rec.get("tunnels_created", 0) + 1
                self._save()


# Set in main(): a Handler instance the background sweeper can use
# to call back into Handler._remove_tunnel_user() and
# Handler._caddy_reload_for_tunnel(). The sweeper runs in its own
# thread (no request), so we can't pull a Handler from the request.
_SWEEPER_HANDLER = None


def _get_active_handler():
    """Return the Handler instance the sweeper should use, or None if
    the server hasn't started yet."""
    return _SWEEPER_HANDLER


def _parse_ttl(s: str) -> Optional[int]:
    """Parse a human TTL string into seconds. Returns None for "never" or
    unparseable input. Accepted: "1h", "24h", "7d", "30d", "90d", "1y"."""
    if not s:
        return None
    s = s.strip().lower()
    if s in ("never", "0", "indefinite"):
        return None
    try:
        if s.endswith("h"):
            return int(s[:-1]) * 3600
        if s.endswith("d"):
            return int(s[:-1]) * 86400
        if s.endswith("y"):
            return int(s[:-1]) * 365 * 86400
        if s.endswith("m") and len(s) > 1 and s[:-1].isdigit():
            return int(s[:-1]) * 60
        # bare integer = seconds
        return int(s)
    except (ValueError, IndexError):
        return None


# ----------------------------------------------------------------------------
# Dashboard basicauth (commit 7)
# ----------------------------------------------------------------------------
# Admin endpoints (/api/v1/keys CRUD, force-release) are protected by
# HTTP Basic auth. The admin users live in
# /etc/klan1-tunnel/dashboard-auth.json:
#   {"users": {"admin": {"password_bcrypt": "$2b$..."}}}
# If the file is missing on first boot, the server generates a random
# password for the 'admin' user, prints it to stderr (one-time), and
# saves the file. The owner is expected to change the password
# afterwards (the file is mode 0600 so the password hash is private).
DASHBOARD_AUTH_PATH = Path("/etc/klan1-tunnel/dashboard-auth.json")
_DASHBOARD_USERS: dict = {}


def _load_dashboard_users():
    """Read /etc/klan1-tunnel/dashboard-auth.json. If the file doesn't
    exist, generate a fresh 'admin' user with a random password and
    write the file. Returns the users dict (also stored in the module
    global _DASHBOARD_USERS for the handler to read)."""
    global _DASHBOARD_USERS
    if DASHBOARD_AUTH_PATH.exists():
        try:
            data = json.loads(DASHBOARD_AUTH_PATH.read_text())
            users = data.get("users", {})
            _DASHBOARD_USERS = users
            return users
        except Exception as e:
            print(f"[admin] could not load {DASHBOARD_AUTH_PATH}: {e}", file=sys.stderr)
            _DASHBOARD_USERS = {}
            return {}
    # First boot: generate a random password for the 'admin' user
    if not HAVE_BCRYPT:
        print("[admin] FATAL: bcrypt not available, cannot create admin user",
              file=sys.stderr)
        _DASHBOARD_USERS = {}
        return {}
    import secrets, string
    alphabet = string.ascii_letters + string.digits
    random_pw = "".join(secrets.choice(alphabet) for _ in range(24))
    salt = bcrypt.gensalt(rounds=10)
    hashed = bcrypt.hashpw(random_pw.encode("utf-8"), salt).decode("ascii")
    DASHBOARD_AUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"users": {"admin": {"password_bcrypt": hashed}}}
    DASHBOARD_AUTH_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True))
    os.chmod(DASHBOARD_AUTH_PATH, 0o600)
    print("=" * 70, file=sys.stderr)
    print("[admin] FIRST BOOT: generated admin user.", file=sys.stderr)
    print(f"[admin]   username: admin", file=sys.stderr)
    print(f"[admin]   password: {random_pw}", file=sys.stderr)
    print(f"[admin]   saved to: {DASHBOARD_AUTH_PATH} (mode 0600)", file=sys.stderr)
    print("[admin]   CHANGE THIS PASSWORD if you want to keep using this",
          file=sys.stderr)
    print("[admin]   server. To rotate, edit the password_bcrypt field.", file=sys.stderr)
    print("=" * 70, file=sys.stderr)
    _DASHBOARD_USERS = payload["users"]
    return _DASHBOARD_USERS


# ----------------------------------------------------------------------------
# HTTP layer
# ----------------------------------------------------------------------------

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "klan1-tunnel-server/0.2"
    state: State = None           # set by server
    auth: Optional[Auth] = None   # set by server (None = auth disabled, all requests pass)
    api_keys: Optional[APIKeyStore] = None  # set by server (None = API keys disabled)

    def log_message(self, format, *args):
        sys.stderr.write("[%s] %s - %s\n" % (now_utc(), self.address_string(), format % args))

    def _require_auth(self):
        """Validate Bearer JWT. Returns (device_id, None) on success, (None, error_payload) on failure.
        If auth is disabled (self.auth is None), returns ("anonymous", None)."""
        if self.auth is None:
            return ("anonymous", None)
        auth_header = self.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return (None, {"error": "missing_auth", "detail": "Authorization: Bearer *** required"})
        token = auth_header[7:].strip()
        res = self.auth.validate_token(token)
        if "error" in res:
            return (None, res)
        return (res["device_id"], None)

    def _check_basicauth(self) -> bool:
        """HTTP Basic auth for the admin endpoints (keys CRUD, dashboard
        mutations). Reads users from /etc/klan1-tunnel/dashboard-auth.json
        (mode 0600). If the file is missing or malformed, returns False
        and the admin endpoints 401.

        The /api/v1/auth/login v2 path (api_key in body) does NOT use
        this — that path authenticates the client, not the admin."""
        if self.auth is None:
            # Auth disabled (--no-auth) → admin endpoints also disabled.
            # We let them through with a warning rather than 401-ing
            # forever in local dev mode.
            return True
        auth_header = self.headers.get("Authorization", "")
        if not auth_header.startswith("Basic "):
            return False
        import base64
        try:
            encoded = auth_header[6:].strip()
            decoded = base64.b64decode(encoded).decode("utf-8", "replace")
            username, _, password = decoded.partition(":")
        except Exception:
            return False
        if not username or not password:
            return False
        # Look up the user
        users = _DASHBOARD_USERS  # global, refreshed by main()
        rec = users.get(username)
        if not rec:
            return False
        stored = rec.get("password_bcrypt", "")
        if not stored:
            return False
        if not HAVE_BCRYPT:
            # bcrypt is required for basicauth; without it we can't
            # verify. Fail closed.
            print("[admin] bcrypt not available; basicauth disabled",
                  file=sys.stderr)
            return False
        try:
            return bcrypt.checkpw(password.encode("utf-8")[:72],
                                   stored.encode("ascii"))
        except Exception:
            return False

    def _send_json(self, code: int, payload):
        body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, code: int, body: str):
        body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def _read_form(self):
        """Read application/x-www-form-urlencoded body."""
        ctype = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        if "application/x-www-form-urlencoded" in ctype:
            return {k: v[0] if len(v) == 1 else v
                    for k, v in urllib.parse.parse_qs(raw).items()}
        # Fallback: try as JSON if not form-encoded
        try:
            return json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return {}

    def _dashboard_redirect(self, msg: str, kind: str = "info"):
        """303 redirect to / with flash message in query string."""
        params = urllib.parse.urlencode({"flash": msg, "kind": kind})
        self.send_response(303)
        self.send_header("Location", f"/?{params}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _remove_tunnel_user(self, port: int):
        """Best-effort cleanup of the on-demand tunnel user for this port.
        Called when a tunnel is released. Safe to call multiple times."""
        if not port:
            return
        user = f"tunnel-{port}"
        # The /api/v1/tunnels DELETE handler already cleans up the user.
        # This is a no-op safety net for the dashboard-only release path.
        try:
            import subprocess
            subprocess.run(
                ["userdel", "-r", "-f", user],
                check=False, capture_output=True, timeout=10,
            )
        except Exception:
            pass
        # Also remove the per-tunnel private key (the dashboard/provision path
        # writes /etc/klan1-tunnel/tunnel-<port>.key; nothing else does).
        try:
            for suffix in (".key", ".key.pub"):
                p = KEYS_BASE / f"{user}{suffix}"
                if p.exists():
                    p.unlink()
        except Exception:
            pass

    # ----- Caddy integration (commit 4) -----

    def _read_caddy_dns_token(self) -> Optional[str]:
        """Read the Cloudflare DNS token used by the Caddy tls.dns block.

        The token is stored at CADDY_DNS_TOKEN_PATH (mode 0600) so it
        doesn't have to live in this script. Return None if the file
        is missing — in that case the generated Caddyfile will use a
        placeholder and `caddy validate` will fail with a clear message."""
        try:
            if not CADDY_DNS_TOKEN_PATH.exists():
                return None
            return CADDY_DNS_TOKEN_PATH.read_text().strip() or None
        except OSError as e:
            print(f"[caddy] could not read DNS token: {e}", file=sys.stderr)
            return None

    def generate_caddyfile(self, tunnels: list) -> str:
        """Build the klan1-tunnel slice of the Caddyfile from the
        current tunnel list. One vhost per active tunnel:

            <device_id>.<base_domain> {
                tls { dns cloudflare <token> }
                reverse_proxy 127.0.0.1:<port>
            }

        Returns a full Caddyfile (not a fragment) so `caddy validate`
        can be run on it standalone. The shape is the same that the
        v1 hardcoded block had, just dynamic."""
        token = self._read_caddy_dns_token()
        if not token:
            token = "<no-token-set>"  # validate will catch this
        parts = []
        parts.append("# klan1-tunnel Caddyfile slice — DO NOT EDIT BY HAND")
        parts.append("# Regenerated automatically on every provision/release.")
        parts.append("# This file is `import`-ed by /etc/caddy/Caddyfile.")
        parts.append("")
        for t in tunnels:
            name = t.get("name")
            port = t.get("remote_port")
            if not name or not port:
                continue
            fqdn = f"{name}.{BASE_DOMAIN}"
            parts.append(f"{fqdn} {{")
            parts.append(f"    tls {{")
            parts.append(f"        dns {CADDY_DNS_PROVIDER} {token}")
            parts.append(f"    }}")
            parts.append(f"    reverse_proxy 127.0.0.1:{port}")
            parts.append(f"}}")
            parts.append("")
        return "\n".join(parts)

    def _caddy_validate(self, content: str) -> tuple:
        """Run `caddy validate --config <tmpfile>`. Returns (ok, stderr).

        Uses a temp file because caddy validate wants a path, not stdin."""
        import tempfile
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".Caddyfile", delete=False, dir="/tmp"
            ) as f:
                f.write(content)
                tmp = f.name
            r = subprocess.run(
                [CADDY_BIN, "validate", "--config", tmp],
                capture_output=True, text=True, timeout=15,
            )
            return (r.returncode == 0, r.stderr or r.stdout)
        except subprocess.TimeoutExpired:
            return (False, "caddy validate timed out")
        except Exception as e:
            return (False, f"caddy validate error: {e}")
        finally:
            if tmp:
                try:
                    Path(tmp).unlink()
                except OSError:
                    pass

    def _caddy_reload(self, content: str) -> tuple:
        """Write content to CADDY_TUNNELS_PATH, run caddy validate against
        the FULL Caddyfile, then caddy reload. Returns (ok, message).

        Strategy: do not touch the main Caddyfile from here. Just write
        the slice and reload the main Caddyfile (which is what
        `caddy reload --config <main>` does). The main Caddyfile is
        expected to `import` our slice (commit 9 will add that line).
        Until then, the reload sees the unchanged main Caddyfile and
        no harm done.

        If `caddy validate --config <main>` fails, abort before reload."""
        try:
            CADDY_TUNNELS_PATH.parent.mkdir(parents=True, exist_ok=True)
            # Atomic write: write to .tmp, then rename
            tmp = CADDY_TUNNELS_PATH.with_suffix(".tmp")
            tmp.write_text(content)
            os.chmod(tmp, 0o644)
            os.replace(tmp, CADDY_TUNNELS_PATH)
        except OSError as e:
            return (False, f"write {CADDY_TUNNELS_PATH} failed: {e}")

        # Validate the full main Caddyfile (which will import our slice
        # once commit 9 adds the import line). Until then, this just
        # validates the unchanged main.
        main_caddyfile = Path("/etc/caddy/Caddyfile")
        if not main_caddyfile.exists():
            return (True, f"wrote {CADDY_TUNNELS_PATH} (no main Caddyfile to validate)")
        r = subprocess.run(
            [CADDY_BIN, "validate", "--config", str(main_caddyfile)],
            capture_output=True, text=True, timeout=20,
        )
        if r.returncode != 0:
            return (False, f"caddy validate main: {r.stderr or r.stdout}")

        # Reload
        r = subprocess.run(
            [CADDY_BIN, "reload", "--config", str(main_caddyfile)],
            capture_output=True, text=True, timeout=20,
        )
        if r.returncode != 0:
            return (False, f"caddy reload: {r.stderr or r.stdout}")
        return (True, "reloaded")

    def _caddy_reload_for_tunnel(self, device_id: str, port: int, op: str) -> bool:
        """Regenerate the klan1-tunnel Caddyfile slice from current
        state and reload Caddy. Returns True on success.

        Called after every provision (op='add') and release (op='remove',
        from commit 5's sweeper). The function name is a misnomer now
        (it doesn't reload for a single tunnel — it reloads for the
        whole set), but the signature is stable for callers."""
        try:
            tunnels = self.state.list()
            content = self.generate_caddyfile(tunnels)
            ok, msg = self._caddy_reload(content)
            if not ok:
                print(f"[caddy] reload failed after {op} of {device_id}:{port}: {msg}",
                      file=sys.stderr)
            else:
                print(f"[caddy] reloaded after {op} of {device_id}:{port} "
                      f"({len(tunnels)} active vhost(s))", file=sys.stderr)
            return ok
        except Exception as e:
            print(f"[caddy] exception in _caddy_reload_for_tunnel: {e}", file=sys.stderr)
            return False

    def _provision_v2_user(self, port: int, device_id: str) -> dict:
        """v2 provision: same user+key+home+authorized_keys dance as the
        v1 path, but takes (port, device_id) instead of (subdomain).
        The key file lives at /etc/klan1-tunnel/<device_id>.key so the
        owner can find it by device name. Returns
        {ok, user, port, private_key, public_key} on success, or
        {ok: False, error: ...} on failure.

        Side effects (all idempotent): creates the tunnel-<port> unix
        user, creates .ssh/authorized_keys, writes the keypair under
        /etc/klan1-tunnel/<device_id>.key (mode 0600 root).
        """
        user = f"tunnel-{port}"
        user_home = USERS_BASE / user
        ssh_dir = user_home / ".ssh"
        auth_keys = ssh_dir / "authorized_keys"
        keyfile = KEYS_BASE / f"{device_id}.key"

        try:
            # Group
            try:
                import grp
                grp.getgrnam(TUNNELS_GROUP)
            except KeyError:
                subprocess.run(
                    ["groupadd", TUNNELS_GROUP],
                    check=True, capture_output=True,
                )

            # User
            try:
                pw = pwd.getpwnam(user)
            except KeyError:
                user_home.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.chmod(user_home.parent, 0o755)
                except OSError:
                    pass
                subprocess.run(
                    ["useradd", "-M", "-d", str(user_home),
                     "-s", TUNNEL_SHELL, "-G", TUNNELS_GROUP, user],
                    check=True, capture_output=True,
                )
                pw = pwd.getpwnam(user)

            # .ssh dir
            try:
                user_home.mkdir(parents=True, exist_ok=True)
                ssh_dir.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                return {"ok": False, "error": f"provision_failed:mkdir:{e}"}
            try:
                os.chmod(user_home, 0o700)
                os.chmod(ssh_dir, 0o700)
            except OSError:
                pass
            try:
                os.chown(user_home, pw.pw_uid, pw.pw_gid)
                os.chown(ssh_dir, pw.pw_uid, pw.pw_gid)
            except OSError as e:
                print(f"[provision v2] chown: {e}", file=sys.stderr)

            # Keypair
            private_key, public_key = generate_keypair_ed25519()

            # Save key under <device_id> (so the owner can find it)
            try:
                keyfile.parent.mkdir(parents=True, exist_ok=True)
                os.chmod(keyfile.parent, 0o700)
            except (OSError, PermissionError):
                pass
            keyfile.write_text(private_key + "\n")
            os.chmod(keyfile, 0o600)

            # authorized_keys with restrict + permitopen
            auth_line = (
                f'command="",from="*",restrict,port-forwarding,'
                f'permitopen="127.0.0.1:{port}" {public_key}'
            )
            try:
                auth_keys.write_text(auth_line + "\n")
                os.chmod(auth_keys, 0o600)
            except OSError as e:
                return {"ok": False, "error": f"provision_failed:write_auth_keys:{e}"}
            try:
                os.chown(auth_keys, pw.pw_uid, pw.pw_gid)
            except OSError as e:
                print(f"[provision v2] chown auth_keys: {e}", file=sys.stderr)

            return {
                "ok": True,
                "user": user,
                "port": port,
                "private_key": private_key,
                "public_key": public_key,
            }
        except subprocess.CalledProcessError as e:
            return {
                "ok": False,
                "error": f"provision_failed:{e.stderr.decode(errors='replace')}",
            }
        except Exception as e:
            import traceback
            return {
                "ok": False,
                "error": f"provision_failed:{type(e).__name__}:{e}",
                "trace": traceback.format_exc(),
            }

    # ----- routing -----
    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        path = url.path.rstrip("/") or "/"
        if path == "/healthz":
            self._send_json(200, {"ok": True, "time": now_utc()})
        elif path == "/dashboard/admin":
            # Admin page: API keys + devices. Requires basicauth.
            # If basicauth fails, send a 401 with WWW-Authenticate
            # so the browser pops the auth dialog.
            if not self._check_basicauth():
                self.send_response(401)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("WWW-Authenticate", 'Basic realm="klan1-tunnel admin"')
                self.end_headers()
                self.wfile.write(b"401 basicauth required\n")
                return
            self._send_html(200, render_admin_page(
                self.api_keys.list() if self.api_keys else [],
                self.state.list(),
                self.state.port_lo, self.state.port_hi,
            ))
        elif path == "/":
            # Parse query string for flash + filter
            query = urllib.parse.parse_qs(url.query)
            flash_msg = (query.get("flash") or [None])[0]
            flash_kind = (query.get("kind") or ["info"])[0]
            if flash_kind not in ("ok", "err", "info"):
                flash_kind = "info"
            filter_q = (query.get("q") or [""])[0]
            self._send_html(200, render_dashboard(
                self.state.list(),
                self.state.port_lo,
                self.state.port_hi,
                flash_msg=flash_msg,
                flash_kind=flash_kind,
                filter_q=filter_q,
            ))
        elif path == "/dashboard/ssh-command":
            # GET endpoint used by the "ssh" button JS to fetch command info.
            query = urllib.parse.parse_qs(url.query)
            token = (query.get("token") or [""])[0]
            t = self.state.data["tunnels"].get(token)
            if not t:
                return self._send_json(404, {"error": "not_found"})
            self._send_json(200, {
                "user": f"tunnel-{t.get('remote_port', '')}",
                "port": t.get("remote_port", ""),
                "host": API_HOST,
                "local_port": 8080,
            })
        elif path == "/api/v1/tunnels":
            tunnels = self.state.list()
            self._send_json(200, {"tunnels": tunnels, "count": len(tunnels)})
        elif path == "/api/v1/free-port":
            port = self.state.next_free_port()
            self._send_json(200, {"port": port, "range": [self.state.port_lo, self.state.port_hi]})
        elif path == "/api/v1/devices":
            # v1 endpoint (whitelist-based) — removed in commit 9.
            # v2 is API-key-based; create a key from the admin dashboard
            # and pass it to the installer. Returns 410 Gone per the
            # spec so any old client (still pointing here) gets a
            # clear, intentional signal.
            return self._send_json(410, {
                "error": "v1_removed",
                "use": "/dashboard/admin to create an API key",
            })
        elif path == "/api/v1/keys":
            # GET /api/v1/keys — list API keys (admin, basicauth).
            if not self._check_basicauth():
                return self._send_json(401, {"error": "basicauth_required"})
            if self.api_keys is None:
                return self._send_json(503, {"error": "api_keys_disabled"})
            self._send_json(200, {"keys": self.api_keys.list()})
        elif path.startswith("/dashboard/"):
            # GET on a /dashboard/* action (provision, release, extend, …) means
            # the browser navigated here by mistake — the action is POST-only.
            # Redirect to the dashboard root instead of returning JSON 404,
            # which would otherwise replace the HTML with a raw error blob.
            return self._dashboard_redirect(
                f"Action requires POST: {path}", "info"
            )
        else:
            self._send_json(404, {"error": "not_found", "path": path})

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        path = url.path.rstrip("/")

        # Dashboard control endpoints (NO AUTH for now — solo management).
        # Returns HTTP 303 redirect to / after action so the browser
        # re-renders the dashboard with a flash message.

        if path == "/dashboard/provision":
            # v1 endpoint (subdomain-based, on-demand user creation)
            # — removed in commit 9. v2 provisions are done by the
            # client via /api/v1/devices/<id>/provision after the
            # admin mints an API key. Returning a redirect to /
            # with a clear flash message so any bookmarked/old
            # browser action lands on the dashboard with guidance.
            return self._dashboard_redirect(
                "Dashboard provision is gone in v2 — create an API "
                "key from /dashboard/admin and run the installer with "
                "--device-id <name> --api-key <key>", "info",
            )

        if path == "/dashboard/release-bulk":
            form = self._read_form() or {}
            tokens = form.get("tokens") or []
            if isinstance(tokens, str):
                tokens = [tokens]
            if not tokens:
                return self._dashboard_redirect("No tunnels selected", "err")
            deleted = 0
            for tok in tokens:
                tok = tok.strip()
                if not tok:
                    continue
                t = self.state.data["tunnels"].get(tok)
                if t:
                    port = t.get("remote_port")
                    res = self.state.release(tok)
                    if res.get("ok"):
                        deleted += 1
                        self._remove_tunnel_user(port)
            return self._dashboard_redirect(
                f"{deleted} tunnel(s) deleted", "ok" if deleted else "err"
            )

        if path == "/dashboard/release":
            form = self._read_form() or {}
            token = _str(form.get("token")).strip()
            if not token:
                return self._send_json(400, {"error": "token_required"})
            t = self.state.data["tunnels"].get(token)
            if not t:
                return self._dashboard_redirect("Tunnel not found (already expired?)", "err")
            name = t.get("name", "?")
            port = t.get("remote_port", "?")
            res = self.state.release(token)
            if res.get("ok"):
                # Also remove the on-demand tunnel user if it exists
                self._remove_tunnel_user(port)
                return self._dashboard_redirect(
                    f"Tunnel '{name}' (port {port}) deleted", "ok"
                )
            return self._dashboard_redirect(f"Could not delete: {res}", "err")

        if path == "/dashboard/extend":
            form = self._read_form() or {}
            token = _str(form.get("token")).strip()
            ttl_str = _str(form.get("ttl") or "86400").strip()
            try:
                ttl = int(ttl_str)
            except ValueError:
                ttl = 86400
            if not token:
                return self._send_json(400, {"error": "token_required"})
            t = self.state.data["tunnels"].get(token)
            if not t:
                return self._dashboard_redirect("Tunnel not found (already expired?)", "err")
            # Same logic as /api/v1/tunnels/<tok>/heartbeat but without auth
            try:
                result = self.state.heartbeat(token, extend_ttl=ttl)
                new_exp = result.get("expires_at", "?")
                return self._dashboard_redirect(
                    f"Tunnel '{t.get('name','?')}' extended to {ttl}s (expires {new_exp})",
                    "ok",
                )
            except Exception as e:
                return self._dashboard_redirect(f"Error extending: {e}", "err")

        # v2 provision: POST /api/v1/devices/<device_id>/provision
        # Requires an API-key-bound JWT (key_id claim).
        # Returns the full bundle (device_id, tunnel_user, tunnel_port,
        # fqdn, ssh_host, ssh_user, ssh_port, private_key, ssh_command,
        # expires_at, ttl, token) so the installer can save the key and
        # open the SSH reverse tunnel in one shot.
        m_prov = re.match(r"^/api/v1/devices/([^/]+)/provision$", path)
        if m_prov:
            if self.auth is None:
                return self._send_json(503, {"error": "auth_disabled"})
            device_id, err = self._require_auth()
            if err:
                return self._send_json(401, err)
            # Path's device_id is the authoritative one (in case the
            # JWT's sub and the URL disagree — URL wins).
            url_device_id = m_prov.group(1)
            if url_device_id != device_id:
                return self._send_json(403, {
                    "error": "device_id_mismatch",
                    "detail": f"JWT sub={device_id!r} but URL device_id={url_device_id!r}",
                })
            # Validate the regex from the spec
            if not re.match(r"^[a-z][a-z0-9-]{0,30}[a-z0-9]$", device_id):
                return self._send_json(400, {
                    "error": "invalid_device_id",
                    "detail": "must match ^[a-z][a-z0-9-]{0,30}[a-z0-9]$",
                })
            # Body: optional local_port (default 8080)
            try:
                data = self._read_json() or {}
            except Exception:
                data = {}
            try:
                local_port = int(data.get("local_port") or 8080)
                if not (1 <= local_port <= 65535):
                    raise ValueError
            except (TypeError, ValueError):
                return self._send_json(400, {"error": "invalid_local_port"})

            # Reserve a port via the State machine (handles name_in_use
            # and no_free_ports). We stamp the JWT's key_id (if any)
            # onto the tunnel entry so the sweeper can drop tunnels
            # whose key was revoked.
            token_for_state = (self.headers.get("Authorization", "")[7:].strip()
                               if self.headers.get("Authorization", "").startswith("Bearer ")
                               else "")
            key_id_for_state = None
            if token_for_state and self.auth is not None:
                vres = self.auth.validate_token(token_for_state)
                if vres.get("ok"):
                    key_id_for_state = vres.get("claims", {}).get("key_id")

            res = self.state.reserve(
                name=device_id,
                requested_port=None,  # lowest free
                protocol="http",
                server_alias="primary",
                egress_ip="api",
                ttl=self.state.default_ttl,
                api_key_id=key_id_for_state,
            )
            if not res.get("ok"):
                code = 409 if res.get("error") == "name_in_use" else 503
                return self._send_json(code, res)

            port = res["remote_port"]
            token = res["token"]
            fqdn = f"{device_id}.{BASE_DOMAIN}"

            # Provision the unix user + key (idempotent side effects)
            prov = self._provision_v2_user(port, device_id)
            if not prov.get("ok"):
                # Roll back the reservation to avoid leaking a port
                self.state.release(token)
                return self._send_json(500, {
                    "error": "provision_failed",
                    "detail": prov.get("error"),
                })

            # Build the SSH command the installer will run
            ssh_host = API_HOST or "<your-server-host>"
            ssh_user = "<your-linux-user>"  # from fleet.json; hard-coded for now
            ssh_port = <your-admin-ssh-port>
            tunnel_user = prov["user"]
            ssh_cmd = (
                f"ssh -i ~/.klan1-tunnel/id_ed25519_{tunnel_user} "
                f"-N -T -R {port}:127.0.0.1:{local_port} "
                f"{tunnel_user}@{ssh_host} -p {ssh_port}"
            )

            # Side effect: caddy reload (commit 4 will implement; for
            # now we just log and flag it in the response).
            caddy_reload_ok = self._caddy_reload_for_tunnel(device_id, port, "add")

            # Bump tunnels_created on the api_key (if the JWT was bound
            # to one). We do this best-effort.
            claims = self.auth.validate_token(
                self.headers.get("Authorization", "")[7:].strip()
            )
            key_id = (claims or {}).get("claims", {}).get("key_id")
            if key_id and self.api_keys is not None:
                self.api_keys.inc_tunnels_created(key_id)

            return self._send_json(200, {
                "device_id": device_id,
                "tunnel_user": tunnel_user,
                "tunnel_port": port,
                "fqdn": fqdn,
                "ssh_host": ssh_host,
                "ssh_user": ssh_user,
                "ssh_port": ssh_port,
                "private_key": prov["private_key"],
                "ssh_command": ssh_cmd,
                "expires_at": res["expires_at"],
                "ttl": res["ttl"],
                "token": token,
                "caddy_reload_ok": caddy_reload_ok,
            })

        # Auth endpoints (no auth required for /auth/login)
        if path == "/api/v1/auth/login":
            if self.auth is None:
                return self._send_json(503, {"error": "auth_disabled"})
            data = self._read_json() or {}
            device_id = (data.get("device_id") or "").strip()
            api_key = (data.get("api_key") or "").strip()
            if not device_id:
                return self._send_json(400, {"error": "device_id_required"})
            if not api_key:
                # v1 (no api_key) path is gone — api_key is mandatory in v2.
                return self._send_json(400, {"error": "api_key_required"})

            # v2 path: login with an API key. The key is the credential.
            if self.api_keys is None:
                return self._send_json(503, {"error": "api_keys_disabled"})
            rec = self.api_keys.verify(api_key)
            if not rec:
                return self._send_json(401, {"error": "invalid_api_key"})
            # Re-check validity (revoked/expired) at the auth layer too
            if not self.api_keys.is_valid(rec["id"]):
                return self._send_json(401, {"error": "api_key_revoked"})
            token = self.auth.issue_token_for_key(
                device_id=device_id,
                key_id=rec["id"],
                key_name=rec.get("name", ""),
            )
            return self._send_json(200, {
                "ok": True,
                "token": token,
                "device_id": device_id,
                "key_id": rec["id"],
                "expires_in": self.auth.ttl,
            })

        if path == "/api/v1/auth/refresh":
            # v2: re-issue a fresh JWT bound to the same API key
            # (the key_id is in the current token's claims). v1
            # used a device-only refresh; that path is gone.
            if self.auth is None:
                return self._send_json(503, {"error": "auth_disabled"})
            auth_header = self.headers.get("Authorization", "")
            token = auth_header[7:].strip() if auth_header.startswith("Bearer ") else ""
            if not token:
                return self._send_json(401, {"error": "missing_bearer"})
            vres = self.auth.validate_token(token)
            if not vres.get("ok"):
                return self._send_json(401, vres)
            claims = vres.get("claims", {}) or {}
            device_id = claims.get("sub", "")
            key_id = claims.get("key_id", "")
            if not device_id or not key_id:
                return self._send_json(401, {"error": "legacy_v1_token"})
            # Look up the key name for the JWT 'name' claim (best-effort)
            key_name = ""
            if self.api_keys is not None:
                for rec in self.api_keys.list():
                    if rec.get("id") == key_id:
                        key_name = rec.get("name", "")
                        break
            new_token = self.auth.issue_token_for_key(
                device_id=device_id,
                key_id=key_id,
                key_name=key_name,
            )
            return self._send_json(200, {
                "ok": True,
                "token": new_token,
                "device_id": device_id,
                "key_id": key_id,
                "expires_in": self.auth.ttl,
            })

        # API key management (admin endpoints — basicauth, not JWT).
        # The dashboard is what calls these; the client never touches
        # them directly (it uses /api/v1/auth/login with a key, not
        # creates new ones).
        # Note: the GET /api/v1/keys handler lives in do_GET, not here.

        if path == "/api/v1/keys" and self.command == "POST":
            if not self._check_basicauth():
                return self._send_json(401, {"error": "basicauth_required"})
            if self.api_keys is None:
                return self._send_json(503, {"error": "api_keys_disabled"})
            data = self._read_json() or {}
            name = (data.get("name") or "").strip()
            if not name:
                return self._send_json(400, {"error": "name_required"})
            ttl_seconds = data.get("ttl_seconds")
            # Accept ttl as either seconds (int) or a human string like
            # "24h", "7d", "30d", "90d", "1y", "never"
            if isinstance(ttl_seconds, str):
                ttl_seconds = _parse_ttl(ttl_seconds)
            if ttl_seconds is not None and (not isinstance(ttl_seconds, int) or ttl_seconds <= 0):
                return self._send_json(400, {"error": "invalid_ttl"})
            created = self.api_keys.create(name, ttl_seconds=ttl_seconds)
            return self._send_json(201, created)

        # DELETE /api/v1/keys/<id>
        if path.startswith("/api/v1/keys/") and self.command == "DELETE":
            if not self._check_basicauth():
                return self._send_json(401, {"error": "basicauth_required"})
            if self.api_keys is None:
                return self._send_json(503, {"error": "api_keys_disabled"})
            key_id = path[len("/api/v1/keys/"):].strip()
            if not key_id or "/" in key_id:
                return self._send_json(400, {"error": "invalid_key_id"})
            if self.api_keys.revoke(key_id):
                return self._send_json(200, {"ok": True, "key_id": key_id, "revoked": True})
            return self._send_json(404, {"error": "key_not_found"})

        m = re.match(r"^/api/v1/tunnels/([^/]+)/heartbeat$", path)
        if path == "/api/v1/tunnels":
            # v1 POST: client chose a subdomain, server on-demand provisioned
            # the tunnel user, and the key came from devices.json. Removed
            # in commit 9. v2 clients use POST /api/v1/devices/<id>/provision
            # (which requires an API-key-bound JWT). Anything hitting this
            # bare endpoint is a legacy v1 client.
            return self._send_json(410, {
                "error": "v1_removed",
                "use": "POST /api/v1/devices/<device_id>/provision (with API key)",
            })
        elif m:
            token = m.group(1)
            device_id, err = self._require_auth()
            if err:
                return self._send_json(401, err)
            data = self._read_json() or {}
            extend = data.get("extend_ttl")
            res = self.state.heartbeat(token, extend)
            return self._send_json(200 if res.get("ok") else 404, res)
        else:
            self._send_json(404, {"error": "not_found", "path": path})

    def do_DELETE(self):
        url = urllib.parse.urlparse(self.path)
        path = url.path.rstrip("/")
        # DELETE /api/v1/tunnels/<token>  (release a tunnel, JWT-auth)
        m = re.match(r"^/api/v1/tunnels/([^/]+)$", path)
        if m:
            device_id, err = self._require_auth()
            if err:
                return self._send_json(401, err)
            res = self.state.release(m.group(1))
            return self._send_json(200 if res.get("ok") else 404, res)
        # DELETE /api/v1/keys/<id>  (admin revoke, basicauth)
        if path.startswith("/api/v1/keys/"):
            if not self._check_basicauth():
                return self._send_json(401, {"error": "basicauth_required"})
            if self.api_keys is None:
                return self._send_json(503, {"error": "api_keys_disabled"})
            key_id = path[len("/api/v1/keys/"):].strip()
            if not key_id or "/" in key_id:
                return self._send_json(400, {"error": "invalid_key_id"})
            if self.api_keys.revoke(key_id):
                return self._send_json(200, {"ok": True, "key_id": key_id, "revoked": True})
            return self._send_json(404, {"error": "key_not_found"})
        self._send_json(404, {"error": "not_found", "path": path})


# ----------------------------------------------------------------------------
# Dashboard HTML (single-file, dark theme, no JS deps)
# ----------------------------------------------------------------------------

# Heartbeat health thresholds (seconds since last heartbeat)
# Same as client-side HEARTBEAT_INTERVAL=30 * HEARTBEAT_GRACE=3 = 90s, plus a buffer.
_HEALTH_OK_SECS = 90
_HEALTH_WARN_SECS = 180


def _health_badge(secs_since_hb: int) -> str:
    if secs_since_hb < 0:
        return '<span class="badge badge-stale">never</span>'
    if secs_since_hb <= _HEALTH_OK_SECS:
        cls, label = "badge-ok", "ok"
    elif secs_since_hb <= _HEALTH_WARN_SECS:
        cls, label = "badge-warn", "stale"
    else:
        cls, label = "badge-stale", "dead"
    return f'<span class="badge {cls}">{label}</span>'


def _secs_since_hb(t: dict) -> int:
    hb = parse_iso_utc(t.get("last_heartbeat", ""))
    if not hb:
        return -1
    return int((datetime.datetime.now(datetime.timezone.utc) - hb).total_seconds())


def render_dashboard(tunnels, port_lo, port_hi, flash_msg=None, flash_kind="info",
                     filter_q="", private_key_for_token=None, ssh_command_for_token=None):
    taken = {t["remote_port"] for t in tunnels}
    free = [p for p in range(port_lo, port_hi + 1) if p not in taken]
    now = now_utc()

    # Apply filter (server-side; matches name, server, port, protocol, egress_ip)
    q = (filter_q or "").strip().lower()
    if q:
        tunnels = [
            t for t in tunnels
            if q in str(t.get("name", "")).lower()
            or q in str(t.get("server_alias", "")).lower()
            or q in str(t.get("remote_port", ""))
            or q in str(t.get("protocol", "")).lower()
            or q in str(t.get("egress_ip", "")).lower()
        ]

    rows = []
    for t in sorted(tunnels, key=lambda x: (x.get("server_alias", ""), x.get("remote_port", 0))):
        exp = parse_iso_utc(t.get("expires_at", ""))
        ttl_left = ""
        if exp:
            delta = exp - datetime.datetime.now(datetime.timezone.utc)
            secs = int(delta.total_seconds())
            if secs < 0:
                ttl_left = "EXPIRED"
            elif secs < 3600:
                ttl_left = f"{secs // 60}m{secs % 60:02d}s"
            elif secs < 86400:
                ttl_left = f"{secs // 3600}h{(secs % 3600) // 60:02d}m"
            else:
                ttl_left = f"{secs // 86400}d{(secs % 86400) // 3600:02d}h"
        token = t.get("token", "")
        token_short = token[:10] + "…" if token else ""
        hb_secs = _secs_since_hb(t)
        health = _health_badge(hb_secs)

        # Build the "Show SSH command" button — opens a modal-like alert
        ssh_btn = (
            f'<button type="button" class="btn btn-ssh" '
            f'onclick="showSshCommand({escape_html(token)!r})" '
            f'title="Ver SSH command">ssh</button>'
        )

        rows.append(f"""
        <tr>
          <td><label class="checkbox-cell"><input type="checkbox" name="tokens" value="{escape_html(token)}" form="bulk-form"> <code>{escape_html(t.get('name',''))}</code></label><br><small style="color:#6e7681">{token_short}</small></td>
          <td>{escape_html(t.get('server_alias',''))}</td>
          <td><code>{t.get('remote_port','')}</code></td>
          <td>{escape_html(t.get('protocol',''))}</td>
          <td><code>{escape_html(t.get('egress_ip',''))}</code></td>
          <td>{health} <small style="color:#6e7681">{ttl_left}</small></td>
          <td><small>{escape_html(t.get('created_at',''))}</small></td>
          <td class="actions">
            <form method="POST" action="/dashboard/extend" style="display:inline">
              <input type="hidden" name="token" value="{escape_html(token)}">
              <input type="hidden" name="ttl" value="86400">
              <button type="submit" class="btn btn-extend" title="Extender TTL 24h">+24h</button>
            </form>
            {ssh_btn}
            <form method="POST" action="/dashboard/release" style="display:inline"
                  onsubmit="return confirm('Delete tunnel {escape_html(t.get('name',''))} (port {t.get('remote_port','')})?');">
              <input type="hidden" name="token" value="{escape_html(token)}">
              <button type="submit" class="btn btn-danger" title="Delete tunnel">× Delete</button>
            </form>
          </td>
        </tr>""")
    rows_html = "\n".join(rows) if rows else '<tr><td colspan="8" style="text-align:center;color:#888">no tunnels registered</td></tr>'

    flash_html = ""
    if flash_msg:
        flash_html = f'<div class="flash flash-{flash_kind}">{escape_html(flash_msg)}</div>'

    # Optional: show SSH command / private key inline if requested
    ssh_panel_html = ""
    if ssh_command_for_token or private_key_for_token:
        parts = []
        if ssh_command_for_token:
            parts.append(f'<div class="ssh-label">SSH command:</div><pre class="ssh-block">{escape_html(ssh_command_for_token)}</pre>')
        if private_key_for_token:
            parts.append(f'<div class="ssh-label">Private key (copialo YA — solo se muestra una vez):</div><textarea class="ssh-key" rows="10" readonly>{escape_html(private_key_for_token)}</textarea>')
        ssh_panel_html = '<div class="ssh-panel">' + "".join(parts) + '</div>'

    # Provision form (v2)
    # v1 had a "Create new tunnel" form here that asked for a
    # subdomain number and on-demand provisioned the unix user. v2
    # provisions are done by the client (the installer) against
    # /api/v1/devices/<id>/provision, after the admin mints an API
    # key. This card is now a static how-to.
    provision_form = f"""
<div class="provision-card">
  <h2>Create a new tunnel</h2>
  <p style="margin:0 0 8px;color:#8b949e;font-size:13px;line-height:1.5">
    v2 is API-key based. To bring up a tunnel on a new client:
  </p>
  <ol style="margin:0 0 12px;padding-left:20px;color:#c9d1d9;font-size:13px;line-height:1.6">
    <li>Create an API key from the <a href="/dashboard/admin" class="nav" style="color:#58a6ff">admin panel</a>.</li>
    <li>On the client, run the installer with the key and a unique device-id:
      <pre style="background:#0d1117;border:1px solid #30363d;border-radius:4px;padding:8px;margin:6px 0;font-size:12px;color:#d2a8ff;overflow-x:auto">curl -sSL https://raw.githubusercontent.com/klan1/klan1-tunnel/main/install.sh \\
  | bash -s -- --device-id &lt;name&gt; --api-url &lt;api-url&gt; --api-key &lt;key&gt;</pre>
    </li>
    <li>The server assigns a free port from the 65081-65090 range and writes the Caddy vhost automatically.</li>
  </ol>
  <p style="margin:0;color:#6e7681;font-size:12px">
    Active tunnels appear in the table below. Use the action column to extend, fetch the SSH command, or release.
  </p>
</div>"""

    filter_input = f"""
<form method="GET" action="/" class="filter-form">
  <input name="q" placeholder="Filter (name, port, ip...)" value="{escape_html(filter_q)}" autofocus>
  <button type="submit">Filter</button>
  <a href="/" class="btn-link">Clear</a>
</form>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>klan1-tunnel dashboard</title>
<!--
  No <meta http-equiv="refresh"> here. It nukes:
    - the user's scroll position
    - any in-progress copy-paste from a post-provision panel
    - the focus state of an open <select> / <input>
  Instead the table body is refreshed by JS (fetch /api/v1/tunnels every
  15s) so the page chrome and any visible key/ssh-command panels stay put.
  See the inline <script> at the bottom of this file.
-->
<style>
  body {{ font-family: ui-monospace, 'SF Mono', Menlo, monospace; background: #0d1117; color: #c9d1d9; margin: 0; padding: 24px; }}
  h1 {{ color: #58a6ff; margin: 0 0 8px; font-size: 22px; }}
  .sub {{ color: #8b949e; font-size: 13px; margin-bottom: 24px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
  th {{ text-align: left; padding: 10px 12px; border-bottom: 1px solid #30363d; color: #58a6ff; font-weight: 600; }}
  td {{ padding: 10px 12px; border-bottom: 1px solid #21262d; vertical-align: middle; }}
  tr:hover td {{ background: #161b22; }}
  code {{ background: #161b22; padding: 2px 6px; border-radius: 4px; color: #d2a8ff; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; margin-bottom: 24px; }}
  .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 6px; padding: 16px; }}
  .card h2 {{ margin: 0 0 8px; font-size: 12px; text-transform: uppercase; color: #8b949e; letter-spacing: 0.5px; }}
  .card .v {{ font-size: 28px; color: #58a6ff; font-weight: 600; }}
  .footer {{ margin-top: 32px; color: #6e7681; font-size: 12px; text-align: center; }}
  small {{ color: #8b949e; }}
  td.actions {{ white-space: nowrap; }}
  .btn {{ background: #21262d; color: #c9d1d9; border: 1px solid #30363d; border-radius: 4px; padding: 4px 10px; font-size: 12px; cursor: pointer; font-family: inherit; }}
  .btn:hover {{ background: #30363d; }}
  .btn-danger {{ color: #f85149; border-color: #6e2a2a; }}
  .btn-danger:hover {{ background: #6e2a2a; color: #fff; }}
  .btn-extend {{ color: #58a6ff; border-color: #1f4d80; }}
  .btn-extend:hover {{ background: #1f4d80; color: #fff; }}
  .btn-ssh {{ color: #d2a8ff; border-color: #5a3d80; }}
  .btn-ssh:hover {{ background: #5a3d80; color: #fff; }}
  .btn-primary {{ color: #fff; background: #1f4d80; border-color: #58a6ff; padding: 6px 16px; }}
  .btn-primary:hover {{ background: #58a6ff; color: #0d1117; }}
  .btn-link {{ color: #58a6ff; text-decoration: none; margin-left: 8px; font-size: 13px; }}
  .flash {{ padding: 12px 16px; border-radius: 6px; margin-bottom: 16px; font-size: 14px; }}
  .flash-ok {{ background: #0d4429; border: 1px solid #1f6e3e; color: #56d364; }}
  .flash-err {{ background: #4a0d12; border: 1px solid #8b1a26; color: #ff7b72; }}
  .flash-info {{ background: #0d2944; border: 1px solid #1f4d80; color: #58a6ff; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }}
  .badge-ok {{ background: #0d4429; color: #56d364; }}
  .badge-warn {{ background: #5a3d00; color: #f0c674; }}
  .badge-stale {{ background: #4a0d12; color: #ff7b72; }}
  .checkbox-cell {{ display: inline-flex; align-items: center; gap: 6px; cursor: pointer; }}
  .filter-form {{ margin-bottom: 16px; display: flex; gap: 8px; align-items: center; }}
  .filter-form input {{ background: #0d1117; color: #c9d1d9; border: 1px solid #30363d; border-radius: 4px; padding: 6px 12px; font-family: inherit; font-size: 13px; flex: 1; max-width: 400px; }}
  .filter-form button {{ background: #21262d; color: #c9d1d9; border: 1px solid #30363d; border-radius: 4px; padding: 6px 14px; cursor: pointer; font-family: inherit; font-size: 13px; }}
  .provision-card {{ background: #161b22; border: 1px solid #30363d; border-radius: 6px; padding: 16px; margin-bottom: 24px; }}
  .provision-card h2 {{ margin: 0 0 12px; font-size: 12px; text-transform: uppercase; color: #8b949e; letter-spacing: 0.5px; }}
  .provision-form {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; align-items: end; }}
  .provision-form label {{ display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: #8b949e; }}
  .provision-form input, .provision-form select {{ background: #0d1117; color: #c9d1d9; border: 1px solid #30363d; border-radius: 4px; padding: 6px 10px; font-family: inherit; font-size: 13px; }}
  .bulk-bar {{ background: #161b22; border: 1px solid #30363d; border-radius: 6px; padding: 10px 16px; margin-bottom: 12px; display: flex; gap: 12px; align-items: center; font-size: 13px; }}
  .bulk-bar span {{ color: #8b949e; }}
  .ssh-panel {{ background: #161b22; border: 1px solid #58a6ff; border-radius: 6px; padding: 16px; margin-bottom: 16px; }}
  .ssh-label {{ color: #8b949e; font-size: 12px; margin-bottom: 6px; text-transform: uppercase; letter-spacing: 0.5px; }}
  .ssh-block {{ background: #0d1117; border: 1px solid #30363d; border-radius: 4px; padding: 10px; font-family: inherit; font-size: 13px; overflow-x: auto; white-space: pre-wrap; word-break: break-all; color: #d2a8ff; }}
  .ssh-key {{ width: 100%; background: #0d1117; color: #d2a8ff; border: 1px solid #30363d; border-radius: 4px; padding: 10px; font-family: ui-monospace, 'SF Mono', Menlo, monospace; font-size: 12px; resize: vertical; }}
</style>
<script>
  // Show SSH command (and private key if present in window.__lastProvision) by
  // rebuilding the panel inline. We POST to a tiny endpoint to fetch the
  // ssh_command for a given token, then display it.
  function showSshCommand(token) {{
    fetch('/dashboard/ssh-command?token=' + encodeURIComponent(token))
      .then(r => r.json())
      .then(d => {{
        if (d.error) {{ alert('Error: ' + d.error); return; }}
        const cmd = 'ssh -i ~/.klan1-tunnel/id_ed25519_' + d.user + ' -N -T -R ' + d.port + ':127.0.0.1:' + (d.local_port || 8080) + ' ' + d.user + '@' + d.host;
        prompt('Copy this command and run it on your machine:', cmd);
      }})
      .catch(e => alert('Fetch failed: ' + e));
  }}

  // Bulk actions: select all checkbox toggles all row checkboxes
  function toggleAll(cb) {{
    document.querySelectorAll('input[type=checkbox][name=tokens]').forEach(c => {{ c.checked = cb.checked; }});
  }}
</script>
</head>
<body>
<h1>klan1-tunnel dashboard</h1>
<div class="sub">self-hosted ngrok-like tunnels — refreshed {escape_html(now)} — auto-refresh 15s — <a href="/dashboard/admin" class="nav">admin</a></div>

{flash_html}

{ssh_panel_html}

{provision_form}

{filter_input}

<form id="bulk-form" method="POST" action="/dashboard/release-bulk"
      onsubmit="return confirm('Delete the selected tunnels?');">
  <div class="bulk-bar">
    <label class="checkbox-cell">
      <input type="checkbox" onclick="toggleAll(this)" title="Select all">
      <span>Bulk actions</span>
    </label>
    <button type="submit" class="btn btn-danger">× Delete selected</button>
    <span>(select with the checkboxes on the left)</span>
  </div>

<div class="grid">
  <div class="card">
    <h2>Active tunnels</h2>
    <div class="v">{len(tunnels)}</div>
  </div>
  <div class="card">
    <h2>Free ports</h2>
    <div class="v">{len(free)} <small style="color:#8b949e">of {port_hi - port_lo + 1}</small></div>
  </div>
  <div class="card">
    <h2>Port range</h2>
    <div class="v"><code>{port_lo}-{port_hi}</code></div>
  </div>
</div>

<table>
  <thead>
    <tr><th>Name</th><th>Server</th><th>Port</th><th>Proto</th><th>Egress IP</th><th>Health</th><th>Created</th><th>Actions</th></tr>
  </thead>
  <tbody>
    {rows_html}
  </tbody>
</table>
</form>

<div class="footer">klan1-tunnel-server v0.3 — dashboard interactivo + auto-refresh + filter + bulk + provision</div>
</body>
</html>"""


def render_admin_page(api_keys, tunnels, port_lo, port_hi):
    """Render the admin page: list API keys + devices, with forms
    to create/revoke keys. Requires HTTP Basic auth (the caller
    already checked).

    Uses a tiny bit of fetch() JS for create/revoke. No external
    dependencies; the auth header is reused from the page (the
    browser caches the Basic credentials per-realm)."""
    now = now_utc()
    key_rows = []
    for k in api_keys:
        kid = escape_html(k.get("id", ""))
        name = escape_html(k.get("name", ""))
        created = escape_html(k.get("created_at", ""))
        expires = escape_html(k.get("expires_at", ""))
        revoked = k.get("revoked_at")
        last_used = k.get("last_used_at") or "-"
        tunnels_created = k.get("tunnels_created", 0)
        status = "REVOKED" if revoked else ("active" if k.get("expires_at") else "active (no expiry)")
        if revoked:
            status_html = f'<span class="revoked">{status}</span>'
        else:
            status_html = f'<span class="active">{status}</span>'
        revoke_btn = (f'<button class="btn btn-danger" onclick="revokeKey(\'{kid}\')">× Revoke</button>'
                      if not revoked else '<span class="muted">—</span>')
        key_rows.append(f"""
    <tr>
      <td><code>{kid}</code></td>
      <td>{name}</td>
      <td>{status_html}</td>
      <td>{created}</td>
      <td>{expires or '—'}</td>
      <td>{last_used}</td>
      <td>{tunnels_created}</td>
      <td>{revoke_btn}</td>
    </tr>""")

    dev_rows = []
    for t in tunnels:
        name = escape_html(t.get("name", ""))
        port = t.get("remote_port", "")
        token = escape_html(t.get("token", ""))
        kid = escape_html(t.get("api_key_id", "—"))
        created = escape_html(t.get("created_at", ""))
        expires = escape_html(t.get("expires_at", ""))
        dev_rows.append(f"""
    <tr>
      <td><code>{name}</code></td>
      <td>{port}</td>
      <td><code class="small">{token[:16]}...</code></td>
      <td><code class="small">{kid}</code></td>
      <td>{created}</td>
      <td>{expires}</td>
    </tr>""")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>klan1-tunnel admin</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; margin: 24px; max-width: 1280px; }}
  h1 {{ margin-bottom: 8px; }}
  .sub {{ color: #888; font-size: 14px; margin-bottom: 16px; }}
  h2 {{ margin-top: 32px; border-bottom: 1px solid #ddd; padding-bottom: 6px; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 12px; }}
  th, td {{ padding: 8px 10px; text-align: left; border-bottom: 1px solid #eee; font-size: 14px; }}
  th {{ background: #f5f5f5; font-weight: 600; }}
  code {{ font-family: 'SF Mono', Menlo, monospace; font-size: 13px; }}
  code.small {{ font-size: 11px; color: #666; }}
  .btn {{ background: #2c7be5; color: white; border: 0; padding: 6px 12px;
         border-radius: 4px; cursor: pointer; font-size: 13px; }}
  .btn:hover {{ background: #1a68d1; }}
  .btn-danger {{ background: #c0392b; }}
  .btn-danger:hover {{ background: #a82817; }}
  .revoked {{ color: #c0392b; font-weight: 600; }}
  .active {{ color: #28a745; font-weight: 600; }}
  .muted {{ color: #aaa; }}
  .new-key {{ background: #fff3cd; padding: 12px; border-radius: 6px;
              margin: 12px 0; display: none; }}
  .new-key code {{ background: #fff; padding: 4px 6px; border-radius: 3px; display: inline-block; }}
  .flash {{ background: #d4edda; color: #155724; padding: 8px 12px;
            border-radius: 4px; margin: 12px 0; display: none; }}
  .flash.err {{ background: #f8d7da; color: #721c24; }}
  form.create {{ display: flex; gap: 8px; align-items: end; margin: 12px 0; }}
  form.create input, form.create select {{ padding: 6px 8px; border: 1px solid #ccc;
                                            border-radius: 4px; font-size: 14px; }}
  form.create label {{ display: flex; flex-direction: column; font-size: 12px;
                       color: #666; }}
  a.nav {{ color: #2c7be5; text-decoration: none; font-size: 14px; }}
</style>
</head>
<body>
<a href="/" class="nav">← Back to tunnels</a>
<h1>klan1-tunnel admin</h1>
<div class="sub">self-hosted admin panel — {escape_html(now)}</div>

<div class="flash" id="flash"></div>

<h2>API keys</h2>
<form class="create" onsubmit="return createKey(event)">
  <label>Name <input type="text" id="k-name" required placeholder="e.g. work-laptop"></label>
  <label>TTL
    <select id="k-ttl">
      <option value="24h">24 hours</option>
      <option value="7d">7 days</option>
      <option value="30d" selected>30 days</option>
      <option value="90d">90 days</option>
      <option value="1y">1 year</option>
      <option value="never">never</option>
    </select>
  </label>
  <button type="submit" class="btn">+ Create key</button>
</form>

<div class="new-key" id="new-key">
  <strong>New key created.</strong> Copy the secret now — it will not be shown again.
  <br><br>
  Key ID: <code id="new-kid"></code><br>
  Secret: <code id="new-secret"></code>
</div>

<table>
  <thead>
    <tr><th>ID</th><th>Name</th><th>Status</th><th>Created</th><th>Expires</th>
        <th>Last used</th><th>Tunnels</th><th>Action</th></tr>
  </thead>
  <tbody>
    {''.join(key_rows) if key_rows else '<tr><td colspan="8" class="muted">No keys yet. Create one above.</td></tr>'}
  </tbody>
</table>

<h2>Active devices</h2>
<table>
  <thead>
    <tr><th>Name (device_id)</th><th>Port</th><th>Token</th>
        <th>Created by API key</th><th>Created</th><th>Expires</th></tr>
  </thead>
  <tbody>
    {''.join(dev_rows) if dev_rows else '<tr><td colspan="6" class="muted">No active devices.</td></tr>'}
  </tbody>
</table>

<p class="sub" style="margin-top:32px">
  Port range: {port_lo}-{port_hi} — {len(tunnels)} of {port_hi - port_lo + 1} ports used.
</p>

<script>
async function createKey(ev) {{
  ev.preventDefault();
  const name = document.getElementById('k-name').value;
  const ttl  = document.getElementById('k-ttl').value;
  const r = await fetch('/api/v1/keys', {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{ name, ttl_seconds: ttl }})
  }});
  const data = await r.json();
  if (!r.ok) {{ showFlash('Error: ' + (data.error || r.statusText), true); return false; }}
  document.getElementById('new-kid').textContent = data.id;
  document.getElementById('new-secret').textContent = data.secret;
  document.getElementById('new-key').style.display = 'block';
  showFlash('Key ' + data.id + ' created. Copy the secret above — it won\\'t show again.', false);
  setTimeout(() => location.reload(), 200);
  return false;
}}
async function revokeKey(kid) {{
  if (!confirm('Revoke API key ' + kid + '? Tunnels created by it will be swept.')) return;
  const r = await fetch('/api/v1/keys/' + encodeURIComponent(kid), {{
    method: 'DELETE',
  }});
  const data = await r.json().catch(() => ({{}}));
  if (!r.ok) {{ showFlash('Error: ' + (data.error || r.statusText), true); return; }}
  showFlash('Key ' + kid + ' revoked.', false);
  setTimeout(() => location.reload(), 200);
}}
function showFlash(msg, isErr) {{
  const f = document.getElementById('flash');
  f.textContent = msg;
  f.className = 'flash' + (isErr ? ' err' : '');
  f.style.display = 'block';
}}
</script>
</body>
</html>"""


def _str(v) -> str:
    """Coerce form value to string. parse_qs may return lists."""
    if v is None:
        return ""
    if isinstance(v, list):
        return v[0] if v else ""
    return str(v)


def escape_html(s) -> str:
    if s is None:
        return ""
    s = str(s)
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;")
             .replace("'", "&#39;"))


# ----------------------------------------------------------------------------
# Threaded server
# ----------------------------------------------------------------------------

class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8443)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--state", default="/var/lib/klan1-tunnel/state.json")
    ap.add_argument("--key-dir", default="/etc/klan1-tunnel",
                    help="Directory holding jwt-{private,public}.pem and api-keys.json (v1 devices.json is no longer read)")
    ap.add_argument("--port-lo", type=int, default=DEFAULT_PORT_RANGE_START)
    ap.add_argument("--port-hi", type=int, default=DEFAULT_PORT_RANGE_END)
    ap.add_argument("--default-ttl", type=int, default=DEFAULT_TTL_SECONDS)
    ap.add_argument("--no-auth", action="store_true",
                    help="Disable JWT auth (DANGEROUS, only for local debugging)")
    args = ap.parse_args()

    state_path = Path(args.state)
    state_path.parent.mkdir(parents=True, exist_ok=True)

    state = State(state_path, args.port_lo, args.port_hi, args.default_ttl)
    Handler.state = state

    if not args.no_auth:
        if not HAVE_JWT:
            print("[klan1-tunnel-server] FATAL: PyJWT not installed but --no-auth not set", file=sys.stderr)
            sys.exit(2)
        try:
            Handler.auth = Auth(Path(args.key_dir))
        except RuntimeError as e:
            print(f"[klan1-tunnel-server] FATAL: {e}", file=sys.stderr)
            sys.exit(2)
        # API key store is always wired (independent of --no-auth,
        # because the keys themselves are the auth credential for the
        # v2 flow). With --no-auth the API endpoints accept everything
        # anyway, so keys are inert.
        try:
            Handler.api_keys = APIKeyStore(Path(args.key_dir))
        except Exception as e:
            print(f"[klan1-tunnel-server] FATAL: api-keys init failed: {e}", file=sys.stderr)
            sys.exit(2)
    else:
        Handler.auth = None
        Handler.api_keys = None
        print("[klan1-tunnel-server] WARNING: running with --no-auth, all endpoints are public", file=sys.stderr)

    # Load dashboard admin users. Always run so the file gets
    # created on first boot (even with --no-auth, since the dashboard
    # login form still wants a real password).
    _load_dashboard_users()

    # Wire the State and the sweeper so they can reach the Handler
    # for cleanup operations (userdel, caddy reload). We instantiate
    # a Handler with placeholder HTTP request args (it's never used
    # for a real request — only for calling the cleanup methods which
    # read self.state and self.api_keys).
    global _SWEEPER_HANDLER
    try:
        _SWEEPER_HANDLER = Handler.__new__(Handler)
        # __new__ skips __init__, so we set the class-level attrs
        # explicitly on the instance for the cleanup path.
        _SWEEPER_HANDLER.state = state
        _SWEEPER_HANDLER.api_keys = Handler.api_keys
        _SWEEPER_HANDLER.auth = Handler.auth
    except Exception as e:
        print(f"[main] WARNING: could not create sweeper Handler: {e}", file=sys.stderr)
        _SWEEPER_HANDLER = None
    state._api_keys = Handler.api_keys

    # background sweeper
    stop = threading.Event()
    t = threading.Thread(target=state.sweep_periodic, args=(stop,), daemon=True)
    t.start()

    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f"[klan1-tunnel-server] listening on {args.bind}:{args.port}", file=sys.stderr)
    print(f"[klan1-tunnel-server] state file: {state_path}", file=sys.stderr)
    print(f"[klan1-tunnel-server] port range: {args.port_lo}-{args.port_hi}", file=sys.stderr)
    print(f"[klan1-tunnel-server] default TTL: {args.default_ttl}s", file=sys.stderr)
    print(f"[klan1-tunnel-server] auth: {'enabled' if Handler.auth else 'DISABLED'}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[klan1-tunnel-server] shutting down", file=sys.stderr)
        stop.set()
        server.shutdown()


if __name__ == "__main__":
    main()
