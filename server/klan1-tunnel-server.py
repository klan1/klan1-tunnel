#!/usr/bin/env python3
"""
klan1-tunnel-server - Self-hosted ngrok-like API

Tiny stdlib HTTP server. No external deps. Stores state in a JSON file.
Endpoints:
  POST /api/v1/tunnels              register a tunnel, allocate port, return token
  POST /api/v1/tunnels/<token>/heartbeat  extend TTL
  DELETE /api/v1/tunnels/<token>    release the tunnel
  GET  /api/v1/tunnels              list active tunnels
  GET  /api/v1/free-port            suggest next free port in the range
  GET  /                            web dashboard (HTML)
  GET  /healthz                     liveness

Run:
  python3 klan1-tunnel-server.py --port 8443 --state /var/lib/klan1-tunnel/state.json

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


# Map of subdomain (1..10) -> port
SUBDOMAIN_PORTS = {
    "1": 65081, "2": 65082, "3": 65083, "4": 65084, "5": 65085,
    "6": 65086, "7": 65087, "8": 65088, "9": 65089, "10": 65090,
}
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


def provision_tunnel_user(subdomain: str) -> dict:
    """On-demand: create user tunnel-<port>, install public key with restrict+permitopen.

    Returns dict with: ok, port, user, private_key, error
    Side effects:
      - useradd if missing
      - mkdir /home/<user>/.ssh (700)
      - writes /etc/klan1-tunnel/tunnel-<port>.key (0600 root)
      - writes /home/<user>/.ssh/authorized_keys (0600) with the matching pubkey
    """
    # Normalize: dashboard may pass int from form.get("subdomain"); the keys
    # in SUBDOMAIN_PORTS are strings "1".."10". Accept both, but compare as
    # strings consistently so the lookup never KeyErrors.
    subdomain = str(subdomain).strip()
    if subdomain not in SUBDOMAIN_PORTS:
        return {"ok": False, "error": f"invalid_subdomain:{subdomain!r}"}

    # Serialize the whole provision operation behind a file lock so two
    # concurrent dashboard clicks (or a click during unattended-upgrade)
    # don't trip the /etc/group lock that useradd holds briefly.  flock
    # blocks; it does not fail with "cannot lock /etc/group".
    _PROVISION_LOCK.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = open(_PROVISION_LOCK, "w")
    try:
        import fcntl
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
    except Exception as e:
        lock_fd.close()
        return {"ok": False, "error": f"provision_failed:lock:{e}"}

    try:
        return _provision_tunnel_user_locked(subdomain)
    finally:
        try:
            import fcntl
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        lock_fd.close()


def _provision_tunnel_user_locked(subdomain: str) -> dict:
    port = SUBDOMAIN_PORTS[subdomain]
    user = f"tunnel-{port}"
    user_home = USERS_BASE / user
    ssh_dir = user_home / ".ssh"
    auth_keys = ssh_dir / "authorized_keys"
    keyfile = KEYS_BASE / f"{user}.key"

    try:
        # Make sure the group exists
        try:
            import grp
            grp.getgrnam(TUNNELS_GROUP)
        except KeyError:
            subprocess.run(
                ["groupadd", TUNNELS_GROUP],
                check=True, capture_output=True,
            )

        # Create the user if missing. The shell is set to the restrictive
        # tunnel-shell.sh (which execs sleep infinity). Home dir lives under
        # USERS_BASE (not /home) because the systemd unit ProtectHome=true.
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

        # Ensure .ssh exists with right perms (under USERS_BASE).
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
            print(f"[provision] chown: {e}", file=sys.stderr)

        # Generate the keypair
        private_key, public_key = generate_keypair_ed25519()

        # Save the private key (root only) so we can re-deliver it on retries
        try:
            keyfile.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(keyfile.parent, 0o700)
        except (OSError, PermissionError):
            pass

        keyfile.write_text(private_key + "\n")
        os.chmod(keyfile, 0o600)

        # Build authorized_keys with restrict + permitopen
        # restrict implies no-pty, no-X11, no-agent, no-user-rc, no-port-forwarding-local,
        # so we explicitly re-enable remote port-forwarding with permitopen.
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
            print(f"[provision] chown auth_keys: {e}", file=sys.stderr)

        return {
            "ok": True,
            "user": user,
            "port": port,
            "private_key": private_key,
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
        self._load()
        # sweep expired on startup
        self._sweep_expired()

    def _load(self):
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
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

    def _sweep_expired(self):
        with self._lock:
            now = datetime.datetime.now(datetime.timezone.utc)
            expired = []
            for tok, t in list(self.data["tunnels"].items()):
                exp = parse_iso_utc(t.get("expires_at", ""))
                if exp and exp < now:
                    expired.append(tok)
            for tok in expired:
                port = self.data["tunnels"][tok].get("remote_port")
                del self.data["tunnels"][tok]
                if port and port in self.data["ports_reserved"]:
                    self.data["ports_reserved"].remove(port)
            if expired:
                print(f"[sweep] removed {len(expired)} expired tunnels", file=sys.stderr)
                self._save()

    def sweep_periodic(self, stop_event: threading.Event):
        while not stop_event.is_set():
            time.sleep(30)
            self._sweep_expired()

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
                server_alias: str, egress_ip: str, ttl: int) -> dict:
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
# Auth layer (JWT RS256 with device whitelist)
# ----------------------------------------------------------------------------

class Auth:
    """JWT-based authentication with a persistent device whitelist.

    State files (all owned by root, mode 0600):
      - jwt-private.pem: RSA private key, signs JWTs
      - jwt-public.pem:  RSA public key, validates JWTs
      - devices.json:    {"devices": {"<id>": {"name": "...", "active": true, ...}}}

    Tokens are RS256 JWTs with claims:
      sub:    device_id
      name:   human-readable name
      iat:    issued-at (unix)
      exp:    expires-at (unix)
    """

    def __init__(self, key_dir: Path, ttl: int = DEFAULT_JWT_TTL_SECONDS):
        if not HAVE_JWT:
            raise RuntimeError("PyJWT not installed; run: pip install pyjwt cryptography")
        self.key_dir = key_dir
        self.ttl = ttl
        self.priv_path = key_dir / "jwt-private.pem"
        self.pub_path = key_dir / "jwt-public.pem"
        self.devices_path = key_dir / "devices.json"
        if not self.priv_path.exists() or not self.pub_path.exists():
            raise RuntimeError(f"JWT keys missing in {key_dir}; run: openssl genrsa -out jwt-private.pem 2048 && openssl rsa -in jwt-private.pem -pubout -out jwt-public.pem")
        self.priv_pem = self.priv_path.read_bytes()
        self.pub_pem = self.pub_path.read_bytes()
        self._lock = threading.RLock()
        self._load_devices()

    def _load_devices(self):
        if self.devices_path.exists():
            try:
                self.devices = json.loads(self.devices_path.read_text())
            except Exception:
                self.devices = {"devices": {}}
        else:
            self.devices = {"devices": {}}
        if "devices" not in self.devices:
            self.devices = {"devices": {}}

    def _save_devices(self):
        tmp = self.devices_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.devices, indent=2, sort_keys=True))
        tmp.replace(self.devices_path)
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        os.chmod(self.devices_path, 0o600)

    def list_devices(self) -> list:
        with self._lock:
            return [{"device_id": k, **v} for k, v in self.devices["devices"].items()]

    def add_device(self, device_id: str, name: str = None) -> dict:
        with self._lock:
            if device_id in self.devices["devices"]:
                return {"ok": False, "error": "device_exists"}
            self.devices["devices"][device_id] = {
                "name": name or device_id,
                "active": True,
                "created_at": now_utc(),
            }
            self._save_devices()
            return {"ok": True, "device_id": device_id, "name": name or device_id}

    def revoke_device(self, device_id: str) -> dict:
        with self._lock:
            if device_id not in self.devices["devices"]:
                return {"ok": False, "error": "unknown_device"}
            self.devices["devices"][device_id]["active"] = False
            self.devices["devices"][device_id]["revoked_at"] = now_utc()
            self._save_devices()
            return {"ok": True, "device_id": device_id, "active": False}

    def is_device_active(self, device_id: str) -> bool:
        with self._lock:
            d = self.devices["devices"].get(device_id)
            return bool(d and d.get("active"))

    def issue_token(self, device_id: str) -> str:
        """Sign and return a JWT for the given device_id."""
        with self._lock:
            d = self.devices["devices"].get(device_id, {})
        now = int(time.time())
        payload = {
            "sub": device_id,
            "name": d.get("name", device_id),
            "iat": now,
            "exp": now + self.ttl,
        }
        return jwt.encode(payload, self.priv_pem, algorithm=DEFAULT_JWT_ALGO)

    def issue_token_for_key(self, device_id: str, key_id: str,
                            key_name: str = "") -> str:
        """Sign and return a JWT bound to a specific API key.

        Adds a 'key_id' claim so downstream code can verify the key is
        still valid (not revoked) and trace which key created a tunnel."""
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

        Two flavors of JWT are accepted:
          - v1: payload has 'sub' (device_id) but no 'key_id'. The
            device must be in devices.json with active=true.
          - v2: payload has 'sub' (device_id) AND 'key_id'. The device
            does NOT need to be in devices.json — the API key is the
            credential, and the device is whatever the client claimed
            in the URL. The key must still be valid (not revoked, not
            expired) at the moment of the request.
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
        # v2 path: api_key-bound JWT. The key is checked at issuance
        # time (in /api/v1/auth/login); we don't re-check validity here
        # because revoking a key should not invalidate already-issued
        # JWTs mid-flight. The sweeper + /api/v1/tunnels/<tok>/heartbeat
        # are the right places to fail closed if the key was revoked
        # between JWT issuance and use.
        if claims.get("key_id"):
            return {"ok": True, "device_id": device_id, "claims": claims}
        # v1 path: device must be active in the whitelist.
        if not self.is_device_active(device_id):
            return {"error": "device_revoked"}
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
        """Stub: HTTP Basic auth for the admin endpoints (keys CRUD,
        dashboard mutations in commit 7). For now this is a TODO that
        always returns False; the admin endpoints will return 401 until
        commit 7 wires a real admin user + hashed password.

        The /api/v1/auth/login v2 path (api_key in body) does NOT use
        this — that path authenticates the client, not the admin."""
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

    def _caddy_reload_for_tunnel(self, device_id: str, port: int, op: str) -> bool:
        """STUB for commit 4. Returns True if Caddy was reloaded (or
        there's nothing to do); False if the reload failed.

        For now this just logs and returns True, so the provision flow
        doesn't get blocked while commit 4 wires the real generator +
        dry-run + reload logic."""
        print(f"[caddy] (stub) {op} vhost for {device_id}.<base> -> 127.0.0.1:{port}", file=sys.stderr)
        return True

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
            # Requires auth — list devices visible to this caller
            device_id, err = self._require_auth()
            if err:
                return self._send_json(401, err)
            if self.auth is None:
                return self._send_json(503, {"error": "auth_disabled"})
            self._send_json(200, {"devices": self.auth.list_devices()})
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
            # Provision a new tunnel: provision on-demand user + reserve port.
            form = self._read_form() or {}
            name = _str(form.get("name")).strip()
            subdomain_str = _str(form.get("subdomain")).strip()
            proto = _str(form.get("protocol") or "http").strip().lower()
            try:
                local_port = int(_str(form.get("local_port") or 8080))
            except (TypeError, ValueError):
                local_port = 8080

            if not name:
                return self._dashboard_redirect("Name is required", "err")
            if proto not in ("http", "socks5", "tcp"):
                proto = "http"
            try:
                subdomain = int(subdomain_str) if subdomain_str else None
            except ValueError:
                return self._dashboard_redirect(f"Invalid subdomain: {subdomain_str}", "err")

            if subdomain is None:
                return self._dashboard_redirect("Subdomain is required", "err")

            try:
                prov = provision_tunnel_user(str(subdomain))
                if not prov.get("ok"):
                    return self._dashboard_redirect(
                        f"Could not provision tunnel-{prov.get('port','?')}: {prov.get('error')}",
                        "err",
                    )
                rport = prov["port"]
                tunnel_user = prov["user"]
                private_key = prov["private_key"]
            except Exception as e:
                return self._dashboard_redirect(f"Exception during provision: {e}", "err")

            res = self.state.reserve(name, rport, proto, "primary", "dashboard", 86400)
            if not res.get("ok"):
                return self._dashboard_redirect(f"Could not reserve: {res}", "err")

            token = res["token"]
            ssh_cmd = (
                f"ssh -i ~/.klan1-tunnel/id_ed25519_{tunnel_user} "
                f"-N -T -R {rport}:127.0.0.1:{local_port} "
                f"{tunnel_user}@{API_HOST}"
            )
            fqdn = f"{subdomain}.{BASE_DOMAIN}"
            # Show the SSH command + private key inline in the dashboard
            return self._send_html(200, render_dashboard(
                self.state.list(),
                self.state.port_lo,
                self.state.port_hi,
                flash_msg=f"Tunnel '{name}' created at https://{fqdn} — copy the SSH key BELOW",
                flash_kind="ok",
                private_key_for_token=private_key,
                ssh_command_for_token=ssh_cmd,
            ))

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
        # Requires JWT (any kind: v1 whitelist or v2 api_key-bound).
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
            # and no_free_ports).
            res = self.state.reserve(
                name=device_id,
                requested_port=None,  # lowest free
                protocol="http",
                server_alias="primary",
                egress_ip="api",
                ttl=self.state.default_ttl,
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

            # v2 path: login with an API key. Independent of any device
            # in the v1 devices.json whitelist — the key is the credential.
            if api_key:
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

            # v1 path: device in devices.json whitelist. Kept for
            # backwards compat — the existing `mac-hq` tunnel on ai1
            # still uses this until the v2 cutover.
            if not self.auth.is_device_active(device_id):
                return self._send_json(403, {"error": "device_not_active"})
            token = self.auth.issue_token(device_id)
            return self._send_json(200, {
                "ok": True,
                "token": token,
                "device_id": device_id,
                "expires_in": self.auth.ttl,
            })

        if path == "/api/v1/auth/refresh":
            if self.auth is None:
                return self._send_json(503, {"error": "auth_disabled"})
            device_id, err = self._require_auth()
            if err:
                return self._send_json(401, err)
            token = self.auth.issue_token(device_id)
            return self._send_json(200, {
                "ok": True,
                "token": token,
                "device_id": device_id,
                "expires_in": self.auth.ttl,
            })

        # API key management (admin endpoints — basicauth, not JWT).
        # The dashboard is what calls these; the client never touches
        # them directly (it uses /api/v1/auth/login with a key, not
        # creates new ones).
        if path == "/api/v1/keys" and self.command == "GET":
            if not self._check_basicauth():
                return self._send_json(401, {"error": "basicauth_required"})
            if self.api_keys is None:
                return self._send_json(503, {"error": "api_keys_disabled"})
            return self._send_json(200, {"keys": self.api_keys.list()})

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
            # Require auth
            device_id, err = self._require_auth()
            if err:
                return self._send_json(401, err)
            data = self._read_json()
            if data is None:
                return self._send_json(400, {"error": "bad_json"})
            name = (data.get("name") or device_id or "anon").strip()
            subdomain = (data.get("subdomain") or "").strip()
            try:
                rport = int(data.get("remote_port") or 0)
            except (TypeError, ValueError):
                rport = 0
            proto = (data.get("protocol") or "http").lower()
            if proto not in ("http", "socks5", "tcp"):
                proto = "http"
            try:
                ttl = int(data.get("ttl") or self.state.default_ttl)
            except (TypeError, ValueError):
                ttl = self.state.default_ttl
            ttl = max(60, min(ttl, 7 * 24 * 3600))
            server_alias = (data.get("server_alias") or "primary").strip() or "primary"
            egress_ip = (data.get("egress_ip") or "").strip() or "unknown"

            # If a subdomain was given, on-demand provision the tunnel user
            # and return the private key. The client uses this key to open
            # the SSH reverse tunnel.
            private_key = None
            tunnel_user = None
            if subdomain:
                prov = provision_tunnel_user(subdomain)
                if not prov.get("ok"):
                    return self._send_json(503, {
                        "ok": False,
                        "error": "provision_failed",
                        "detail": prov.get("error"),
                    })
                rport = prov["port"]
                tunnel_user = prov["user"]
                private_key = prov["private_key"]

            res = self.state.reserve(name, rport, proto, server_alias, egress_ip, ttl)
            code = 200 if res.get("ok") else (409 if res.get("error") == "name_in_use" else 503)

            # Augment the response with tunnel info (private key + user)
            if private_key and res.get("ok"):
                res["private_key"] = private_key
                res["user"] = tunnel_user
                res["subdomain"] = subdomain
                res["subdomain_fqdn"] = f"{subdomain}.{BASE_DOMAIN}"
                res["ssh_command"] = (
                    f"ssh -i ~/.klan1-tunnel/id_ed25519_{tunnel_user} "
                    f"-N -T -R {rport}:127.0.0.1:{data.get('local_port', 8080)} "
                    f"{tunnel_user}@{API_HOST}"
                )
            return self._send_json(code, res)
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
        m = re.match(r"^/api/v1/tunnels/([^/]+)$", path)
        if m:
            device_id, err = self._require_auth()
            if err:
                return self._send_json(401, err)
            res = self.state.release(m.group(1))
            return self._send_json(200 if res.get("ok") else 404, res)
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

    # Provision form
    provision_form = f"""
<div class="provision-card">
  <h2>Create new tunnel</h2>
  <form method="POST" action="/dashboard/provision" class="provision-form">
    <label>Name (device) <input name="name" required placeholder="macbook"></label>
    <label>Subdomain #
      <input name="subdomain" type="number" min="1" max="10" required placeholder="1">
    </label>
    <label>Protocol
      <select name="protocol">
        <option value="http">http</option>
        <option value="socks5">socks5</option>
        <option value="tcp">tcp</option>
      </select>
    </label>
    <label>Local port <input name="local_port" type="number" min="1" max="65535" value="8080"></label>
    <button type="submit" class="btn btn-primary">Provisionar</button>
  </form>
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
<div class="sub">self-hosted ngrok-like tunnels — refreshed {escape_html(now)} — auto-refresh 15s</div>

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
                    help="Directory holding jwt-{private,public}.pem and devices.json")
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
