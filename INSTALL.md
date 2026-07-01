# Installing klan1-tunnel

A practical guide to going from "empty box" to "tunnel running" on each
supported platform. The **one-liner** at the top is the fast path; the
rest of the document explains what it does and how to fix it when it
fails on a fresh box (your Chromebook, a server with no Python, a Mac
without Homebrew, etc.).

> **TL;DR for any Linux box with `sudo` and `curl`:**
> ```bash
> curl -sSL https://raw.githubusercontent.com/klan1/klan1-tunnel/main/install.sh | \
>     bash -s -- --name mydevice --subdomain 1
> ```
> The installer will prompt for the 5 values of `fleet.json` (SSH
> host/user/port, API URL, base domain) on first run. On a non-TTY
> pipe (e.g. over SSH), pass `--fleet PATH` to a pre-built config or
> `--non-interactive` to abort cleanly. See [§1.3](#13-running-the-one-liner).

## Table of contents

- [1. Linux (Debian / Ubuntu / Fedora / Arch)](#1-linux)
  - [1.1 What you need first](#11-what-you-need-first)
  - [1.2 Installing Python 3 and pip](#12-installing-python-3-and-pip)
  - [1.3 Running the one-liner](#13-running-the-one-liner)
  - [1.4 What the one-liner does (and where things go)](#14-what-the-one-liner-does)
  - [1.5 Verification](#15-verification)
- [2. macOS (Sonoma+, with or without Homebrew)](#2-macos)
- [3. Chromebook (Crostini Linux)](#3-chromebook)
- [4. Headless / no-`sudo` Linux containers](#4-headless)
- [5. Troubleshooting](#5-troubleshooting)

---

<a id="1-linux"></a>
## 1. Linux (Debian / Ubuntu / Fedora / Arch)

This is the primary path. Tested on Debian Bullseye/Bookworm, Ubuntu
LTS, Fedora, and Arch. Works on Chromebook Crostini containers too
(they look like a normal Debian box from `apt`'s point of view).

### 1.1 What you need first

A user account with `sudo` access and a working internet connection.
That's it. The installer takes care of everything else.

If you are **not** sure whether you have `sudo`, run:

```bash
sudo -n true && echo "have sudo" || echo "no sudo"
```

If you see "no sudo" but you are the only user on the box, see
[§4 Headless / no-`sudo` Linux containers](#4-headless).

### 1.2 Installing Python 3 and pip

Pick your distribution. Each block is the minimum needed before the
`klan1-tunnel` one-liner can run. The `klan1-tunnel` installer will
also run these for you, but it's useful to know which package is
which.

**Debian / Ubuntu / Raspberry Pi OS / Linux Mint / Pop!_OS**

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv autossh openssh-client curl
```

Verify:

```bash
python3 --version      # expect Python 3.9 or newer
python3 -m pip --version
```

**Fedora / RHEL / Rocky / Alma (8+)**

```bash
sudo dnf install -y python3 python3-pip python3-virtualenv autossh openssh-clients curl
```

**Arch / Manjaro**

```bash
sudo pacman -Sy --noconfirm python python-pip autossh openssh curl
```

**Alpine (very small containers, edge cases)**

```bash
sudo apk add --no-cache python3 py3-pip py3-virtualenv autossh openssh-client curl bash
```

> **Why Python 3.9+?** The `klan1-pproxy` fork that `klan1-tunnel`
> depends on drops support for Python 3.6 and 3.7 (both are EOL).
> Older Debian (Buster, Bullseye) and Ubuntu (18.04, 20.04) ship 3.9
> in their universe repos as `python3.9` / `python3.11`. If
> `python3` resolves to 3.7 or older, the installer will tell you and
> abort. See [§5 Troubleshooting](#5-troubleshooting) for the
> pyenv-free upgrade path.

### 1.3 Running the one-liner

Once `python3`, `pip`, and `curl` exist, the one-liner takes over:

**Interactive (TTY available — desktop, laptop, first run on a server):**

```bash
curl -sSL https://raw.githubusercontent.com/klan1/klan1-tunnel/main/install.sh | \
    bash -s -- --name mydevice --subdomain 1
```

You'll be prompted for the 5 values of `fleet.json` (only on the first
run; subsequent runs on the same box reuse `~/.config/klan1-tunnel/fleet.json`):

```text
SSH host of the tunnel server: tunnel.example.com
SSH user: tunnel
SSH port: 22
API base URL: https://api.tunnel.example.com
Base domain for subdomains: tunnel.example.com
```

The example values (`tunnel.example.com`, `tunnel`, `22`) are deliberately
non-routable — replace them with your real infrastructure. They are
shown here only to illustrate the prompt shape.

The config is saved with mode `0600` so only your user can read it.

**Non-interactive (CI, `curl | bash` over SSH, scripts):**

```bash
# 1. Build fleet.json on a box with a TTY (one-time):
ssh user@ai1 "curl -sSL .../install.sh | bash -s -- --name bootstrap --subdomain 1"
scp user@ai1:.config/klan1-tunnel/fleet.json /tmp/fleet.json

# 2. Use it on the target box:
curl -sSL .../install.sh | \
    bash -s -- --name newbox --subdomain 2 --fleet /tmp/fleet.json --non-interactive
```

**Install only (no auto-start) — useful for inspecting what landed:**

```bash
curl -sSL .../install.sh | \
    bash -s -- --name mydevice --subdomain 1 --no-start
~/.local/bin/klan1-tunnel start --name mydevice --subdomain 1
```

Flags (for `install.sh`):

| Flag | What it does | Default |
|---|---|---|
| `--name NAME` | Identifier of this tunnel in the dashboard | `$(hostname -s)` |
| `--subdomain N` | Which of the 10 pilot slots (`1`–`10`); picks a fixed remote port | **required** |
| `--fleet PATH` | Use a pre-built `fleet.json` (skip interactive prompt) | search in 3 standard locations |
| `--non-interactive` | Abort with a clear error if `fleet.json` is missing (use under `curl \| bash` over SSH) | off (prompt if needed) |
| `--prefix DIR` | Where to drop `klan1-tunnel` and the venv | `~/.local` |
| `--no-start` | Install only; do not open the tunnel | off |
| `--skip-deps` | Don't `apt install` (you did it manually) | off |

The installer searches for `fleet.json` in this order, using the first
one it finds:

1. `--fleet PATH` (if given)
2. `~/.config/klan1-tunnel/fleet.json`
3. `~/.klan1-tunnel/fleet.json`
4. `/etc/klan1-tunnel/fleet.json`

If none of those exist and `--non-interactive` was passed, the installer
aborts with `refusing to prompt. Pass --fleet PATH.` Otherwise it asks
the 5 questions above and writes a fresh config to
`~/.config/klan1-tunnel/fleet.json` (mode `0600`).

The installer also fail-fasts if `fleet.json` still contains any
`<your-...>` placeholder — better than letting `ssh` return
`Bad port <your-ssh-port>` 30 seconds later.

### 1.4 What the one-liner does (and where things go)

In order, on a fresh Debian box:

1. `apt install -y autossh python3-pip python3-venv openssh-client`
2. Creates `~/.klan1-tunnel/venv` (a Python venv) and runs
   `pip install klan1-pproxy` inside it.
3. Generates `~/.ssh/id_ed25519_<server>` and prints the public key
   for you to paste into the server's `authorized_keys`.
4. Downloads `client/klan1-tunnel.sh` to `~/.local/bin/klan1-tunnel`
   and `chmod +x` it.
5. Runs `klan1-tunnel start --name <name> --server <server>` in the
   background.

The venv path matters: on modern Debian/Ubuntu (PEP 668), `pip install`
without a venv refuses to touch the system Python. The installer
sidesteps that by always using a venv at `~/.klan1-tunnel/venv`.

### 1.5 Verification

After the installer finishes, the tunnel should be reachable. Three
quick checks, in order of cheapness:

```bash
# 1. Local: did the klan1-tunnel background process start?
~/.local/bin/klan1-tunnel status

# 2. Local: is pproxy actually importable from the venv?
~/.klan1-tunnel/venv/bin/python3 -c 'import pproxy; print(pproxy.__file__)'

# 3. Remote: does the public IP respond on the remote port?
curl -x http://127.0.0.1:<remote-port> http://ifconfig.me
```

If step 3 returns an IP that matches one of the fleet servers' public
IPs (configured in `config/fleet.example.json`), the tunnel is alive.
The dashboard at the API URL from your fleet config also lists it.

---

<a id="2-macos"></a>
## 2. macOS (Sonoma+, with or without Homebrew)

**With Homebrew** (the common case):

```bash
brew install autossh
python3 -m pip install --user 'klan1-pproxy>=3.0.1'
# Make sure ~/.config/klan1-tunnel/fleet.json exists (the installer
# writes it; on macOS the only way to get it is the interactive one-liner
# from a Terminal with a TTY):
curl -sSL https://raw.githubusercontent.com/klan1/klan1-tunnel/main/install.sh | \
    bash -s -- --name mac --subdomain 1 --no-start
~/.local/bin/klan1-tunnel start --name mac --subdomain 1 --api-url https://api.<your-base-domain>
```

**Without Homebrew** (rare, but happens on locked-down work Macs):

macOS Sonoma and newer ship `python3` in
`/usr/bin/python3` (3.9.6 as of Sonoma 14.5). `pip` is not bundled;
bootstrap it with `ensurepip`:

```bash
python3 -m ensurepip --upgrade
python3 -m pip install --user 'klan1-pproxy>=3.0.1'
```

`autossh` without Homebrew is harder. You have three options:

1. **Use plain `ssh` instead of `autossh`.** The client falls back to
   `ssh` automatically; you lose automatic restart on disconnect but
   for a one-shot test it works:

   ```bash
   ~/.local/bin/klan1-tunnel start --name mac --subdomain 1 --no-autossh
   ```

   (Verify whether your client version actually has this flag; if not,
   just point `PATH` away from `autossh` for one run:
   `PATH=/usr/bin:/bin ~/.local/bin/klan1-tunnel start ...`.)

2. **Install `autossh` from source.** Trivial: `curl -L
   https://www.harding.motd.ca/autossh/autossh-1.4g.tar.gz | tar xz
   && cd autossh-1.4g && ./configure && make && sudo make install`.

3. **Install Homebrew.** Honestly the fastest path:
   <https://brew.sh>.

---

<a id="3-chromebook"></a>
## 3. Chromebook (Crostini Linux)

A Chromebook with Crostini enabled is a Debian (or Fedora) container.
The Linux section above applies verbatim. The only Chromebook-specific
quirks:

- **Enable Linux first.** Chrome OS → Settings → Advanced → Developers
  → Linux development environment → Turn on. Wait ~5 minutes for the
  container to come up.
- **Open the Terminal app** (penguin icon). Everything below happens
  there.
- **Crostini's default user has `sudo`** (passwordless, in fact).
  The `sudo` line in §1.1 works as-is.
- **Storage**: the Linux container has a quota. `klan1-tunnel`
  itself is tiny (< 5 MB), so this only matters if you also install
  big pip packages. The default 10 GB is plenty.
- **Suspend/resume**: when the Chromebook sleeps, Crostini pauses.
  The tunnel goes down with it. The client will auto-reconnect when
  you wake the machine; the `klan1-tunnel status` command will show
  the drop and recovery in the log.

The first-time install (copy-paste in the Crostini terminal):

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv autossh openssh-client curl
curl -sSL https://raw.githubusercontent.com/klan1/klan1-tunnel/main/install.sh | \
    bash -s -- --name chromebook --subdomain 1
```

> **If your Chromebook is a managed/enterprise device** that does not
> allow Linux, you are out of luck with the Crostini path. The only
> workaround is to use the device's browser as a SOCKS/HTTP client
> against the tunnel, not run the tunnel on it.

---

<a id="4-headless"></a>
## 4. Headless / no-`sudo` Linux containers

Some environments (locked-down shared servers, minimal containers,
Kubernetes pods, Codespaces) give you a user account with no `sudo`.
The `apt install` step in §1.2 will fail with "Permission denied".

The recovery path:

1. **Tell the installer to skip system packages.** You provide your
   own `python3`/`pip`/`autossh`, however weird the way you got them
   (Conda, pipx, a tarball, a pre-baked image, etc.):

   ```bash
   curl -sSL .../install.sh | \
       bash -s -- --name mydevice --subdomain 1 --skip-deps
   ```

2. **The venv path is fully user-owned**, so the rest of the
   installer (creating `~/.klan1-tunnel/venv`, running
   `pip install klan1-pproxy` inside it) will succeed.

3. **`autossh` without `sudo`**: same deal. If `autossh` is not on
   `$PATH`, the client falls back to `ssh` (with a warning in
   `~/.klan1-tunnel/tunnel.log`). You can compile `autossh` from
   source into `~/bin/` without root, since it has no dynamic
   dependencies.

4. **Codespaces / Gitpod / similar**: these ship a usable
   `python3` and `pip` already. You only need `autossh`, which you
   can install with `apt-get install -y autossh` in a `postCreateCommand`
   with the right base image.

---

<a id="5-troubleshooting"></a>
## 5. Troubleshooting

### `python3 reports 3.7; need 3.9+`

The klan1-pproxy fork requires Python 3.9 or newer. If your box ships
3.7 (Debian 10, Ubuntu 18.04, CentOS 7), you have three options:

1. **Use a backports-style newer Python** (Debian):
   ```bash
   sudo apt install -y software-properties-common
   sudo add-apt-repository -y ppa:deadsnakes/ppa   # Ubuntu only
   sudo apt install -y python3.11 python3.11-venv
   python3.11 -m venv ~/.klan1-tunnel/venv
   ~/.klan1-tunnel/venv/bin/pip install 'klan1-pproxy>=3.0.1'
   ```
   The installer will pick up `python3.11` automatically on the next
   run if `~/.klan1-tunnel/venv` already exists.

2. **Tell the installer to use a specific Python** (anywhere):
   ```bash
   PYTHON=/usr/local/bin/python3.11
   $PYTHON -m venv ~/.klan1-tunnel/venv
   $PYTHON -m pip install 'klan1-pproxy>=3.0.1'
   ```
   The installer re-uses the venv if it already has `pproxy`.

3. **Upgrade the OS.** Sometimes the cleanest answer. Debian 10 and
   CentOS 7 are EOL.

### `externally-managed-environment` from pip

On modern Debian/Ubuntu (post-PEP 668), `pip install` outside a venv
or `--user` refuses with this error. The installer always uses a venv
at `~/.klan1-tunnel/venv` and should not hit this. If you do, you
probably ran `pip install` manually — wrap it in a venv:

```bash
python3 -m venv ~/.klan1-tunnel/venv
~/.klan1-tunnel/venv/bin/pip install 'klan1-pproxy>=3.0.1'
```

### `autossh: command not found` after install

`autossh` is a separate package from `ssh`. On Debian/Ubuntu:

```bash
sudo apt install -y autossh
```

On macOS: `brew install autossh`. On Alpine:
`apk add autossh`.

The client will fall back to plain `ssh` if `autossh` is missing, but
you lose automatic reconnection on network blips.

### The tunnel starts but `curl -x` from outside returns nothing

Three usual suspects, in order:

1. **Firewall on the tunnel server.** Check that the remote port is
   actually reachable from the public internet:
   `nc -vz <your-server-host> <remote-port>`. If it times out, the
   firewall rules in your `klan1-tunnel` setup did not take effect.
   Open the port with the appropriate firewall manager
   (e.g. `ufw allow <remote-port>/tcp` or `iptables -I INPUT -p tcp --dport <remote-port> -j ACCEPT`).
2. **SSH key not in the server's `authorized_keys`.** The installer
   prints the public key for you to paste. Re-check on the server:
   `tail -1 ~/.ssh/authorized_keys` and compare to
   `cat ~/.ssh/id_ed25519_<server>.pub` on the client.
3. **The reverse tunnel died because the SSH session died.** Check
   `~/.klan1-tunnel/tunnel.log`. If you see `Connection reset by
   peer` in a loop, the server's `MaxStartups` may be saturated.
   Raise it in `/etc/ssh/sshd_config` on the server and `systemctl
   reload sshd`.

### `Permission denied (publickey)` on the very first `klan1-tunnel start`

You generated a key, pasted it into the server, but the server still
rejects. The two usual causes:

- The server's `~/.ssh/authorized_keys` is not mode 600, or the home
  dir is not mode 700. `sshd` is strict about this.
- You pasted the wrong key (e.g. the `id_ed25519_ai1` key from your
  Mac instead of the freshly generated `id_ed25519_ws1`).

Both are fixed in 10 seconds with `chmod 700 ~ && chmod 600
~/.ssh/authorized_keys` and re-pasting the right `.pub` file.

### Where to look when nothing makes sense

In order of signal:

1. `~/.klan1-tunnel/tunnel.log` — the klan1-tunnel client log.
2. `~/.klan1-tunnel/<name>.log` — the per-tunnel log (one per
   `--name`).
3. The dashboard at your `api_url` (see `config/fleet.example.json`) —
   shows registered tunnels, their egress IPs, and their TTL.
4. `journalctl -u klan1-tunnel-server -n 200` on the API server — server-side
   log.
