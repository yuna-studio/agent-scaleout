#!/usr/bin/env python3
"""agent-scaleout orchestrator.

Spread coding-agent jobs (Claude Code, Codex, Gemini CLI, or any command) across the machines on your
network. One orchestrator keeps a job queue, watches every node's memory/disk/load, dispatches jobs to
the least-loaded healthy node, collects results, detects stalls, retries elsewhere, and reclaims disk on
nodes before they fill up.

Python 3.9+ standard library only. Nodes need nothing but SSH and a shell (macOS/Linux) or PowerShell
(Windows). See README.md.
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import io
import json
import os
import platform
import random
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

VERSION = "0.4.0"
HOME = Path.home()
CONF_DIR = Path(os.environ.get("SCALEOUT_CONF", HOME / ".config" / "scaleout"))
STATE_DIR = Path(os.environ.get("SCALEOUT_STATE", HOME / ".local" / "state" / "scaleout"))
CONFIG = CONF_DIR / "config.json"
KEY = Path(os.environ.get("SCALEOUT_KEY", HOME / ".ssh" / "scaleout_ed25519"))
SSH_CONFIG = HOME / ".ssh" / "config"
AUTH_KEYS = HOME / ".ssh" / "authorized_keys"
KNOWN_HOSTS = HOME / ".ssh" / "known_hosts"
PKG_ROOT = Path(__file__).resolve().parent.parent  # repo or installed copy: has node/, agents/

E_OK, E_JOB_FAILED, E_RUNNING, E_UNREACHABLE, E_USAGE, E_NOT_FOUND = 0, 1, 2, 3, 4, 5

DEFAULTS: Dict[str, Any] = {
    "min_free_disk_gb": 5,       # do not dispatch to a node with less free disk
    "min_free_mem_pct": 15,      # ... or less free memory
    "cleanup_below_disk_gb": 8,  # ask the node to reclaim disk below this
    "job_timeout_min": 180,
    "stall_min": 5,              # running job whose heartbeat is older than this is considered dead
    "node_wait_min": 10,
    "max_artifacts_mb": 1024,    # a job's screenshots/videos/logs larger than this stay on the node
    "auto_tune": True,           # daily: set each node's max_jobs from the memory its real jobs used (not for --max-jobs nodes)         # job handed to a node but not started there (node short on memory) -> move it
    "offline_grace_min": 10,     # node unreachable this long -> its jobs are retried elsewhere
    "retries": 2,
    "keep_job_days": 3,
    "metrics_ttl_s": 15,
    "port_base": 2201,
}
SKILL_PATHS = [".claude/skills", ".claude/agents", ".claude/commands"]


# ── small utils ──────────────────────────────────────────────────────────────
def now() -> int:
    return int(time.time())


def die(code: int, msg: str) -> "NoReturn":  # type: ignore[name-defined]
    print(f"scaleout: {msg}", file=sys.stderr)
    sys.exit(code)


def say(msg: str = "") -> None:
    print(msg, flush=True)


def read_json(p: Path, default: Any) -> Any:
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(p: Path, data: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.replace(tmp, p)


def load_cfg() -> Dict[str, Any]:
    cfg = read_json(CONFIG, {})
    cfg.setdefault("version", 2)
    cfg.setdefault("orchestrator", {})
    cfg.setdefault("defaults", {})
    cfg.setdefault("nodes", {})
    cfg.setdefault("skills", {"paths": SKILL_PATHS, "auto": True})
    for k, v in DEFAULTS.items():
        cfg["defaults"].setdefault(k, v)
    return cfg


def save_cfg(cfg: Dict[str, Any]) -> None:
    write_json(CONFIG, cfg)


def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return ""


def run(cmd: List[str], input: Optional[bytes] = None, timeout: Optional[float] = None, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=check)


# ── ssh plumbing ─────────────────────────────────────────────────────────────
SSH_BASE = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", "-o", "ServerAliveInterval=20",
            "-o", "StrictHostKeyChecking=accept-new",
            # reuse one connection per node: a fresh SSH handshake (through a tunnel) costs ~1 s on every call
            "-o", "ControlMaster=auto", "-o", "ControlPath=~/.ssh/scaleout-cm-%C", "-o", "ControlPersist=120"]


def ssh_block_put(name: str, body: str) -> None:
    SSH_CONFIG.parent.mkdir(mode=0o700, exist_ok=True)
    text = SSH_CONFIG.read_text() if SSH_CONFIG.exists() else ""
    start, end = f"# >>> {name}", f"# <<< {name}"
    text = re.sub(rf"\n?{re.escape(start)}\n.*?{re.escape(end)}\n?", "\n", text, flags=re.S)
    text = text.rstrip("\n") + f"\n\n{start}\n{body.rstrip()}\n{end}\n"
    SSH_CONFIG.write_text(text)
    os.chmod(SSH_CONFIG, 0o600)


def ssh_block_del(name: str) -> None:
    if not SSH_CONFIG.exists():
        return
    start, end = f"# >>> {name}", f"# <<< {name}"
    SSH_CONFIG.write_text(re.sub(rf"\n?{re.escape(start)}\n.*?{re.escape(end)}\n?", "\n", SSH_CONFIG.read_text(), flags=re.S))


def lines_without(path: Path, pred) -> None:
    if path.exists():
        path.write_text("".join(l for l in path.read_text().splitlines(True) if not pred(l)))


def ensure_key() -> None:
    if not KEY.exists():
        KEY.parent.mkdir(mode=0o700, exist_ok=True)
        host = socket.gethostname().split(".")[0]
        run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(KEY), "-C", f"scaleout-orchestrator@{host}"], check=True)


def write_node_ssh(name: str, node: Dict[str, Any]) -> None:
    parts = []
    common = f"  User {node['user']}\n  HostKeyAlias scaleout-{name}\n  IdentityFile {KEY}\n  IdentitiesOnly yes\n  StrictHostKeyChecking accept-new\n  ConnectTimeout 6"
    if node.get("port"):
        parts.append(f"Host scaleout-{name}\n  HostName localhost\n  Port {node['port']}\n{common}")
    if node.get("lan"):
        parts.append(f"Host scaleout-{name}-lan\n  HostName {node['lan']}\n  Port {node.get('ssh_port', 22)}\n{common}")
    ssh_block_put(f"scaleout {name}", "\n".join(parts))


def record_hostkey(name: str, hostkey: str) -> None:
    if not hostkey:
        return
    KNOWN_HOSTS.touch()
    lines_without(KNOWN_HOSTS, lambda l: l.startswith(f"scaleout-{name} "))
    with KNOWN_HOSTS.open("a") as f:
        f.write(f"scaleout-{name} {hostkey}\n")


def authorize_tunnel(name: str, port: int, pubkey: str) -> None:
    AUTH_KEYS.parent.mkdir(mode=0o700, exist_ok=True)
    AUTH_KEYS.touch(mode=0o600)
    lines_without(AUTH_KEYS, lambda l: l.rstrip().endswith(f" scaleout-{name}"))
    with AUTH_KEYS.open("a") as f:
        # the node key may only open its reverse tunnel: no shell, no other forwarding
        f.write(f'restrict,port-forwarding,permitlisten="localhost:{port}",command="echo scaleout tunnel only; exit 1" {pubkey.strip()}\n')


class Node:
    """A node and how to reach it."""

    _route_cache: Dict[str, Tuple[str, float]] = {}

    def __init__(self, name: str, cfg: Dict[str, Any]):
        if name not in cfg["nodes"]:
            die(E_NOT_FOUND, f"no node '{name}' (scaleout nodes)")
        self.name, self.cfg, self.n = name, cfg, cfg["nodes"][name]

    @property
    def windows(self) -> bool:
        return self.n.get("os", "").lower().startswith("win")

    def aliases(self) -> List[str]:
        out = []
        if self.n.get("port"):
            out.append(f"scaleout-{self.name}")
        if self.n.get("lan"):
            out.append(f"scaleout-{self.name}-lan")
        return out

    def route(self, fresh: bool = False) -> Optional[str]:
        hit = Node._route_cache.get(self.name)
        if hit and not fresh and time.time() - hit[1] < 60:
            return hit[0] or None
        last = read_json(node_state_path(self.name), {}).get("route")
        order = sorted(self.aliases(), key=lambda x: x != last)   # the route that worked last time first
        for a in order:
            try:
                if run(SSH_BASE + ["-n", "-o", "ConnectTimeout=4", a, "exit"], timeout=12).returncode == 0:
                    Node._route_cache[self.name] = (a, time.time())
                    return a
            except subprocess.TimeoutExpired:
                pass
        Node._route_cache[self.name] = ("", time.time())
        return None

    def ctl_cmd(self, verb: str, *args: str) -> str:
        if self.windows:
            q = " ".join('"' + a.replace('"', '\\"') + '"' for a in args)
            return f'powershell -NoProfile -ExecutionPolicy Bypass -File "%USERPROFILE%\\.scaleout\\bin\\ctl.ps1" {verb} {q}'.strip()
        return "~/.scaleout/bin/ctl " + " ".join([verb] + [shlex.quote(a) for a in args])

    def ctl(self, verb: str, *args: str, input: Optional[bytes] = None, timeout: float = 60) -> subprocess.CompletedProcess:
        r = self.route()
        if not r:
            return subprocess.CompletedProcess([], 255, b"", b"unreachable")
        cmd = SSH_BASE + ([] if input is not None else ["-n"]) + [r, self.ctl_cmd(verb, *args)]
        try:
            res = run(cmd, input=input, timeout=timeout)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(cmd, 124, b"", b"timeout")
        if res.returncode == 255:  # connection dropped: forget the route
            Node._route_cache.pop(self.name, None)
        return res

    def shell(self, command: str, input: Optional[bytes] = None, timeout: Optional[float] = None) -> subprocess.CompletedProcess:
        r = self.route()
        if not r:
            die(E_UNREACHABLE, f"node '{self.name}' is unreachable (asleep? off? scaleout status)")
        return run(SSH_BASE + ([] if input is not None else ["-n"]) + [r, command], input=input, timeout=timeout)


# ── node package ─────────────────────────────────────────────────────────────
def node_package(os_kind: str) -> bytes:
    """tar.gz of the node files for 'unix' or 'windows', version stamped."""
    src = PKG_ROOT / "node" / ("windows" if os_kind == "windows" else "unix")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        files = sorted(src.iterdir()) + [PKG_ROOT / "node" / "claude-settings.json"]
        for f in files:
            if not f.is_file():
                continue
            data = f.read_bytes().replace(b"__VERSION__", VERSION.encode())
            info = tarfile.TarInfo(f.name)
            info.size, info.mode, info.mtime = len(data), 0o755, now()
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def parse_setup(out: str) -> Dict[str, str]:
    d: Dict[str, str] = {}
    for line in out.splitlines():
        m = re.match(r"^SCALEOUT_(\w+) ?(.*)$", line.strip())
        if m:
            d.setdefault(m.group(1).lower(), m.group(2).strip())
    return d


def next_port(cfg: Dict[str, Any]) -> int:
    used = {n.get("port") for n in cfg["nodes"].values()}
    p = int(cfg["defaults"]["port_base"])
    while p in used or socket_busy(p):
        p += 1
    return p


def socket_busy(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def tunnel_args(cfg: Dict[str, Any], port: int) -> List[str]:
    t = cfg["orchestrator"].get("tunnel") or {}
    if not t.get("host"):
        return []
    a = ["--tunnel-host", t["host"], "--tunnel-user", cfg["orchestrator"]["user"], "--tunnel-port", str(port),
         "--tunnel-ssh-port", str(t.get("ssh_port", 22))]
    if t.get("proxy"):
        a += ["--tunnel-proxy", t["proxy"]]
    return a


def finish_register(cfg: Dict[str, Any], name: str, info: Dict[str, str], port: Optional[int], extra: Dict[str, Any]) -> Dict[str, Any]:
    node = cfg["nodes"].get(name, {})
    node.update(extra)
    node.update({
        "os": info.get("os", node.get("os", "")),
        "user": info.get("user", node.get("user", "")),
        "lan": extra.get("lan") or info.get("lan") or node.get("lan", ""),
        "agents": json.loads(info["agents"]) if info.get("agents", "").startswith("[") else node.get("agents", []),
        "added": node.get("added") or time.strftime("%F %T"),
    })
    if info.get("pubkey") and port:
        authorize_tunnel(name, port, info["pubkey"])
        node["port"] = port
    elif not node.get("port"):
        node.pop("port", None)
    node.pop("pending", None)
    node.pop("expires", None)
    cfg["nodes"][name] = node
    write_node_ssh(name, node)
    record_hostkey(name, info.get("hostkey", ""))
    save_cfg(cfg)
    return node


# ── commands: setup ──────────────────────────────────────────────────────────
def cmd_init(a) -> None:
    cfg = load_cfg()
    o = cfg["orchestrator"]
    o["user"] = a.user or o.get("user") or os.environ.get("USER", "")
    o["lan"] = a.lan or o.get("lan") or lan_ip()
    o["ssh_port"] = a.ssh_port or o.get("ssh_port", 22)
    if a.tunnel_host is not None:
        o["tunnel"] = {"host": a.tunnel_host, "proxy": a.tunnel_proxy or "", "ssh_port": a.tunnel_ssh_port or 22} if a.tunnel_host else {}
    elif "tunnel" not in o:
        o["tunnel"] = detect_cloudflare_tunnel()
    ensure_key()
    save_cfg(cfg)
    agents = HOME / ".claude" / "agents"
    if (HOME / ".claude").is_dir():
        agents.mkdir(parents=True, exist_ok=True)
        shutil.copy(PKG_ROOT / "agents" / "claude" / "scaleout.md", agents / "scaleout.md")
        say("Claude Code subagent 'scaleout' installed: ~/.claude/agents/scaleout.md")
    say(f"orchestrator ready (agent-scaleout {VERSION}) · user {o['user']} · LAN {o['lan'] or '?'}")
    t = o.get("tunnel") or {}
    say(f"  reverse tunnel for off-LAN nodes: {t['host']}" if t.get("host") else "  nodes connect over the LAN (add a tunnel later with: scaleout init --tunnel-host HOST [--tunnel-proxy CMD])")
    if run(["nc", "-z", "-G", "2", "127.0.0.1", str(o["ssh_port"])]).returncode != 0 and run(["bash", "-c", f"</dev/tcp/127.0.0.1/{o['ssh_port']}"]).returncode != 0:
        say("  ! this machine's SSH server is off — invites and tunnels need it (macOS: System Settings > General > Sharing > Remote Login)")
    say("next: scaleout add NAME user@host   or   scaleout invite NAME")


def detect_cloudflare_tunnel() -> Dict[str, Any]:
    cfgf = HOME / ".cloudflared" / "config.yml"
    if not cfgf.exists():
        return {}
    host = ""
    for line in cfgf.read_text().splitlines():
        m = re.search(r"hostname:\s*(\S+)", line)
        if m:
            host = m.group(1)
        if re.search(r"service:\s*ssh://(localhost|127\.0\.0\.1):22", line) and host:
            return {"host": host, "proxy": "cloudflared access ssh --hostname %h", "ssh_port": 22}
    return {}


def cmd_add(a) -> None:
    cfg = load_cfg()
    name = a.name
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        die(E_USAGE, "node names: lowercase letters, digits, '-'")
    ensure_key()
    if a.ssh:
        boot = SSH_BASE + [a.ssh]
        pub = KEY.with_suffix(".pub").read_text().strip()
        r = run(boot + ["-n", f"mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && (grep -qF '{pub.split()[1]}' ~/.ssh/authorized_keys || echo '{pub}' >> ~/.ssh/authorized_keys)"])
        if r.returncode != 0:
            die(E_UNREACHABLE, f"cannot reach '{a.ssh}': {r.stderr.decode().strip()}")
        lan = a.lan or ""
    else:
        target = a.target or die(E_USAGE, "add NAME user@host  |  add NAME --ssh ALIAS")
        boot = SSH_BASE + ["-i", str(KEY), "-o", "IdentitiesOnly=yes", target]
        if run(boot + ["-n", "exit"], timeout=20).returncode != 0:
            say(f"copying the orchestrator key to {target} — enter that machine's login password once")
            if subprocess.call(["ssh-copy-id", "-i", str(KEY.with_suffix(".pub")), "-o", "StrictHostKeyChecking=accept-new", target]) != 0:
                die(E_UNREACHABLE, "could not install the key — is SSH/Remote Login on there? For Windows nodes use: scaleout invite NAME --os windows")
        lan = a.lan or target.split("@")[-1]
    uname = run(boot + ["-n", "uname -s"], timeout=20).stdout.decode().strip()
    if not uname:
        die(E_USAGE, "the node does not look like macOS/Linux — for Windows use: scaleout invite NAME --os windows")
    port = cfg["nodes"].get(name, {}).get("port") or (next_port(cfg) if (cfg["orchestrator"].get("tunnel") or {}).get("host") and not a.no_tunnel else None)
    say(f"installing node '{name}' ({uname}) …")
    if run(boot + ["mkdir -p ~/.scaleout/pkg && tar -xzf - -C ~/.scaleout/pkg"], input=node_package("unix"), timeout=120).returncode != 0:
        die(E_UNREACHABLE, "could not upload the node package")
    auto_jobs = a.max_jobs is None
    a.max_jobs = a.max_jobs or 2
    args = ["--name", name, "--dir", a.dir, "--max-jobs", str(a.max_jobs), "--agent", a.agent] + (tunnel_args(cfg, port) if port else [])
    res = run(boot + ["-n", "bash ~/.scaleout/pkg/setup.sh " + " ".join(shlex.quote(x) for x in args)], timeout=600)
    out = res.stdout.decode()
    if res.returncode != 0:
        die(E_UNREACHABLE, f"node setup failed:\n{out}\n{res.stderr.decode()}")
    info = parse_setup(out)
    node = finish_register(cfg, name, info, port, {"lan": lan, "dir": a.dir, "max_jobs": a.max_jobs, "agent": a.agent, "tags": a.tags.split(",") if a.tags else [],
                                                   **({"price": a.price} if a.price else {}), **({} if auto_jobs else {"max_jobs_fixed": True})})
    say(f"  user {node['user']} · LAN {node.get('lan') or '-'} · tunnel port {node.get('port') or '-'} · agents {', '.join(node.get('agents') or []) or 'none'}")
    for note in [l for l in out.splitlines() if l.startswith("SCALEOUT_NOTE")]:
        say("  ! " + note[len("SCALEOUT_NOTE "):])
    wait_reachable(Node(name, cfg), node.get("port"))
    if not node.get("agents"):
        say("  ! no coding agent found on the node — install and sign in to one (e.g. Claude Code) there")
    elif not a.no_test:
        selftest(name)
    if not a.no_test:
        say("  benchmark ...")
        run_bench([name], apply=auto_jobs, quiet_table=True)
    say(f"node '{name}' ready")


def wait_reachable(n: Node, port: Optional[int]) -> None:
    if port:
        for _ in range(24):
            if socket_busy(int(port)):
                break
            time.sleep(5)
        say("  tunnel open" if socket_busy(int(port)) else "  ! tunnel not up yet (LAN still works) — see ~/.scaleout/tunnel.log on the node")
    if not n.route(fresh=True):
        die(E_UNREACHABLE, f"installed, but cannot reach '{n.name}' again — scaleout status {n.name}")


def selftest(name: str) -> None:
    say("  test job …")
    jid = submit_job(load_cfg(), "Connection test. Reply with this machine's hostname only, on one line. Do not modify files.", node=name)
    rc, text = wait_job(jid, 300)
    say(f"  test passed: {text.strip()[:120]}" if rc == E_OK else f"  ! test failed (rc={rc}): {text.strip()[:300]}")


INVITE_UNIX = r'''set -e
N=__N__; ROUTE_LAN=__LAN__; ROUTE_HOST=__HOST__; ROUTE_PROXY=__PROXY__; OU=__OU__; OSP=__OSP__
K="$(mktemp)"; trap 'rm -f "$K"' EXIT
printf '%s' __KEY__ | base64 --decode > "$K"; chmod 600 "$K"
echo "[1/4] SSH server (how the orchestrator reaches this machine)"
if ! (nc -z -w 2 127.0.0.1 22 || nc -z -G 2 127.0.0.1 22) >/dev/null 2>&1; then
  echo "  turning on the SSH server — your password may be asked once"
  if [ "$(uname -s)" = Darwin ]; then sudo launchctl enable system/com.openssh.sshd 2>/dev/null || true; sudo launchctl bootstrap system /System/Library/LaunchDaemons/ssh.plist 2>/dev/null || sudo systemsetup -setremotelogin on
  else (command -v apt-get >/dev/null && sudo apt-get install -y openssh-server >/dev/null) || (command -v dnf >/dev/null && sudo dnf install -y openssh-server >/dev/null) || true; sudo systemctl enable --now ssh 2>/dev/null || sudo systemctl enable --now sshd; fi
fi
echo "[2/4] route to the orchestrator"
EN=(ssh -i "$K" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=15 -p "$OSP")
if [ -n "$ROUTE_LAN" ] && (nc -z -w 3 "$ROUTE_LAN" "$OSP" || nc -z -G 3 "$ROUTE_LAN" "$OSP") >/dev/null 2>&1; then T="$OU@$ROUTE_LAN"; echo "  LAN ($ROUTE_LAN)"
elif [ -n "$ROUTE_HOST" ]; then
  if [ -n "$ROUTE_PROXY" ]; then P="${ROUTE_PROXY%% *}"; command -v "$P" >/dev/null || { command -v brew >/dev/null && brew install "$P" >/dev/null; }; EN+=(-o "ProxyCommand=$(command -v "$P") ${ROUTE_PROXY#* }"); fi
  T="$OU@$ROUTE_HOST"; echo "  $ROUTE_HOST"
else echo "  cannot reach the orchestrator at $ROUTE_LAN" >&2; exit 3; fi
echo "[3/4] install"
mkdir -p ~/.scaleout/pkg
"${EN[@]}" -n "$T" pkg-unix | tar -xzf - -C ~/.scaleout/pkg
"${EN[@]}" -n "$T" args > ~/.scaleout/pkg/args
eval "bash ~/.scaleout/pkg/setup.sh $(cat ~/.scaleout/pkg/args)" > ~/.scaleout/setup.out
echo "[4/4] register + exchange keys"
"${EN[@]}" "$T" register < ~/.scaleout/setup.out > ~/.scaleout/orchestrator.pub
mkdir -p ~/.ssh; chmod 700 ~/.ssh; touch ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys
grep -qF "$(cut -d' ' -f2 ~/.scaleout/orchestrator.pub)" ~/.ssh/authorized_keys || cat ~/.scaleout/orchestrator.pub >> ~/.ssh/authorized_keys
grep -q '^SCALEOUT_AGENTS \[\]' ~/.scaleout/setup.out && echo "  ! no coding agent found — install and sign in to one (e.g. Claude Code: curl -fsSL https://claude.ai/install.sh | bash)"
echo "done — check it on the orchestrator: scaleout status"
'''

INVITE_WIN = r'''$ErrorActionPreference = "Stop"
$N = "__N__"; $LanHost = "__LAN__"; $OU = "__OU__"; $OSP = "__OSP__"
$K = Join-Path $env:TEMP "scaleout-enroll"; [IO.File]::WriteAllBytes($K, [Convert]::FromBase64String("__KEY__"))
icacls $K /inheritance:r /grant:r "$($env:USERNAME):(R)" | Out-Null
Write-Host "[1/4] SSH server (how the orchestrator reaches this machine)"
if (-not (Get-Service sshd -ErrorAction SilentlyContinue)) {
  $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
  if (-not $admin) { Write-Host "  please run this in PowerShell as Administrator (needed once to install OpenSSH Server)"; exit 1 }
  Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 | Out-Null
}
Set-Service sshd -StartupType Automatic; Start-Service sshd
New-ItemProperty -Path "HKLM:\SOFTWARE\OpenSSH" -Name DefaultShell -Value "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -PropertyType String -Force -ErrorAction SilentlyContinue | Out-Null
Write-Host "[2/4] route to the orchestrator ($LanHost)"
$EN = @("-i", $K, "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes", "-p", $OSP, "$OU@$LanHost")
Write-Host "[3/4] install"
$P = Join-Path $env:USERPROFILE ".scaleout\pkg"; New-Item -ItemType Directory -Force $P | Out-Null
& ssh @EN pkg-windows > (Join-Path $P "pkg.tgz"); tar -xzf (Join-Path $P "pkg.tgz") -C $P
$args2 = (& ssh @EN args-windows)
$out = & powershell -NoProfile -ExecutionPolicy Bypass -Command "& '$P\setup.ps1' $args2"
Write-Host "[4/4] register + exchange keys"
$pub = ($out | & ssh @EN register)
$isAdmin = (Get-LocalGroupMember -Group Administrators -ErrorAction SilentlyContinue | Where-Object { $_.Name -like "*\$env:USERNAME" })
if ($isAdmin) { $ak = "$env:ProgramData\ssh\administrators_authorized_keys" } else { $ak = Join-Path $env:USERPROFILE ".ssh\authorized_keys"; New-Item -ItemType Directory -Force (Split-Path $ak) | Out-Null }
if (-not (Test-Path $ak) -or -not (Select-String -Path $ak -SimpleMatch ($pub.Split(" ")[1]) -Quiet)) { Add-Content -Path $ak -Value $pub }
if ($isAdmin) { icacls $ak /inheritance:r /grant "Administrators:F" /grant "SYSTEM:F" | Out-Null }
Remove-Item $K -Force
Write-Host "done - check it on the orchestrator: scaleout status"
'''


def cmd_invite(a) -> None:
    cfg = load_cfg()
    name = a.name
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        die(E_USAGE, "node names: lowercase letters, digits, '-'")
    o = cfg["orchestrator"]
    if not o.get("user"):
        die(E_USAGE, "run scaleout init first")
    win = a.os == "windows"
    t = {} if win else (o.get("tunnel") or {})
    port = cfg["nodes"].get(name, {}).get("port") or (next_port(cfg) if t.get("host") else None)
    inv = CONF_DIR / "invites" / name
    shutil.rmtree(inv, ignore_errors=True)
    inv.mkdir(parents=True)
    run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(inv / "enroll"), "-C", f"scaleout-invite-{name}"], check=True)
    node = cfg["nodes"].get(name, {})
    a.max_jobs = a.max_jobs or 2
    if a.price:
        node["price"] = a.price
    node.update({"pending": True, "expires": now() + a.hours * 3600, "dir": a.dir, "max_jobs": a.max_jobs, "agent": a.agent,
                 "tags": a.tags.split(",") if a.tags else node.get("tags", []), "os": "windows" if win else node.get("os", "")})
    if port:
        node["port"] = port
    cfg["nodes"][name] = node
    save_cfg(cfg)
    exe = shutil.which("scaleout") or str(HOME / ".local" / "bin" / "scaleout")
    AUTH_KEYS.parent.mkdir(mode=0o700, exist_ok=True)
    AUTH_KEYS.touch(mode=0o600)
    lines_without(AUTH_KEYS, lambda l: l.rstrip().endswith(f" scaleout-invite-{name}"))
    with AUTH_KEYS.open("a") as f:
        f.write(f'restrict,command="{exe} _enroll {name}" {(inv / "enroll.pub").read_text().strip()}\n')
    key = base64.b64encode((inv / "enroll").read_bytes()).decode()
    q = shlex.quote
    if win:
        script = (INVITE_WIN.replace("__N__", name).replace("__LAN__", o.get("lan", "")).replace("__OU__", o["user"])
                  .replace("__OSP__", str(o.get("ssh_port", 22))).replace("__KEY__", key))
        b64 = base64.b64encode(script.encode()).decode()
        line = f"iex ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{b64}')))"
        where = "Paste this into PowerShell on the Windows node (run as Administrator the first time):"
    else:
        script = (INVITE_UNIX.replace("__N__", q(name)).replace("__LAN__", q(o.get("lan", ""))).replace("__HOST__", q(t.get("host", "")))
                  .replace("__PROXY__", q(t.get("proxy", ""))).replace("__OU__", q(o["user"])).replace("__OSP__", q(str(o.get("ssh_port", 22))))
                  .replace("__KEY__", q(key)))
        b64 = base64.b64encode(script.encode()).decode()
        line = f'bash -c "$(echo {b64} | base64 --decode)"'
        where = "Paste this into a terminal on the node (macOS / Linux):"
    say(f"invite for '{name}' — single use, valid {a.hours} h{f' · tunnel port {port}' if port else ''}")
    say(where)
    say()
    say(line)
    say()
    say("The SSH server is turned on if needed (password asked once). The node's coding agent must be signed in.")


def cmd_enroll(a) -> None:
    """Forced command of an invite key (never called by hand)."""
    cfg = load_cfg()
    name = a.name
    node = cfg["nodes"].get(name)
    inv = CONF_DIR / "invites" / name
    if not node or not node.get("pending") or not inv.is_dir():
        die(E_NOT_FOUND, "no open invite (already used?)")
    if now() > node.get("expires", 0):
        die(E_NOT_FOUND, f"invite expired — run: scaleout invite {name}")
    verb = os.environ.get("SSH_ORIGINAL_COMMAND", "").strip()
    port = node.get("port")
    if verb == "pkg-unix":
        sys.stdout.buffer.write(node_package("unix"))
    elif verb == "pkg-windows":
        sys.stdout.buffer.write(node_package("windows"))
    elif verb == "args":
        args = ["--name", name, "--dir", node.get("dir", "~/scaleout-work"), "--max-jobs", str(node.get("max_jobs", 2)), "--agent", node.get("agent", "claude")]
        if port:
            args += tunnel_args(cfg, port)
        print(" ".join(shlex.quote(x) for x in args))
    elif verb == "args-windows":
        print(f"-Name {name} -Dir '{node.get('dir', '~/scaleout-work')}' -MaxJobs {node.get('max_jobs', 2)} -Agent '{node.get('agent', 'claude')}'")
    elif verb == "register":
        info = parse_setup(sys.stdin.read())
        if not info.get("user"):
            die(E_USAGE, "empty node report")
        finish_register(cfg, name, info, port, {"lan": info.get("lan", "")})
        lines_without(AUTH_KEYS, lambda l: l.rstrip().endswith(f" scaleout-invite-{name}"))
        shutil.rmtree(inv, ignore_errors=True)
        print(KEY.with_suffix(".pub").read_text().strip())
    else:
        die(E_USAGE, "invite keys only allow: pkg-unix, pkg-windows, args, args-windows, register")


def cmd_remove(a) -> None:
    cfg = load_cfg()
    n = Node(a.name, cfg)
    if n.route():
        if n.windows:
            n.shell('powershell -NoProfile -ExecutionPolicy Bypass -File "%USERPROFILE%\\.scaleout\\bin\\setup.ps1" -Uninstall', timeout=120)
        else:
            n.shell("bash ~/.scaleout/bin/setup.sh --uninstall", timeout=120)
    ssh_block_del(f"scaleout {a.name}")
    lines_without(AUTH_KEYS, lambda l: l.rstrip().endswith(f" scaleout-{a.name}") or l.rstrip().endswith(f" scaleout-invite-{a.name}"))
    lines_without(KNOWN_HOSTS, lambda l: l.startswith(f"scaleout-{a.name} "))
    del cfg["nodes"][a.name]
    save_cfg(cfg)
    shutil.rmtree(CONF_DIR / "invites" / a.name, ignore_errors=True)
    say(f"node '{a.name}' removed")


# ── metrics + health ─────────────────────────────────────────────────────────
def node_state_path(name: str) -> Path:
    return STATE_DIR / "nodes" / f"{name}.json"


def refresh_metrics(cfg: Dict[str, Any], names: Optional[List[str]] = None, max_age: Optional[int] = None) -> Dict[str, Dict[str, Any]]:
    max_age = cfg["defaults"]["metrics_ttl_s"] if max_age is None else max_age
    names = names or [n for n, v in cfg["nodes"].items() if not v.get("pending")]
    out: Dict[str, Dict[str, Any]] = {}

    def one(name: str) -> None:
        st = read_json(node_state_path(name), {})
        if max_age > 0 and st.get("ts", 0) >= now() - max_age:
            out[name] = st
            return
        try:
            res = Node(name, cfg).ctl("metrics", timeout=20)
        except Exception as e:  # never let one node break the round
            res = subprocess.CompletedProcess([], 1, b"", str(e).encode())
        if res.returncode == 0:
            try:
                m = json.loads(res.stdout.decode().strip().splitlines()[-1])
            except (ValueError, IndexError):
                m = {}
            st.update(m)
            st.update({"online": True, "ts": now(), "last_ok": now(), "route": Node._route_cache.get(name, ("",))[0]})
            if int(m.get("running", 1) or 0) == 0 and m.get("mem_total_gb"):
                st["idle_used_gb"] = round(float(m["mem_total_gb"]) * (100 - float(m.get("mem_free_pct", 0))) / 100, 1)
        else:
            st.update({"online": False, "ts": now(), "error": res.stderr.decode().strip()[-200:]})
        write_json(node_state_path(name), st)
        out[name] = st

    threads = [threading.Thread(target=one, args=(n,)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return out


def node_health(cfg: Dict[str, Any], name: str, st: Dict[str, Any]) -> str:
    d = cfg["defaults"]
    if not st.get("online"):
        return "offline"
    if st.get("disk_free_gb", 0) < d["min_free_disk_gb"]:
        return "disk-full"
    if st.get("mem_free_pct", 0) < d["min_free_mem_pct"]:
        return "mem-full"
    if st.get("runner") is False:
        return "runner-down"
    return "ok"


# ── jobs ─────────────────────────────────────────────────────────────────────
def jobs_dir() -> Path:
    return STATE_DIR / "jobs"


def job_path(jid: str) -> Path:
    return jobs_dir() / f"{jid}.json"


def load_job(jid: str) -> Dict[str, Any]:
    j = read_json(job_path(jid), None)
    if j is None:
        die(E_NOT_FOUND, f"no job '{jid}'")
    return j


def save_job(j: Dict[str, Any]) -> None:
    write_json(job_path(j["id"]), j)


def all_jobs() -> List[Dict[str, Any]]:
    return sorted((read_json(p, {}) for p in jobs_dir().glob("*.json")), key=lambda j: j.get("created", 0))


def session_file(sid: str) -> Optional[Path]:
    hits = sorted((HOME / ".claude" / "projects").glob(f"*/{sid}.jsonl"), key=lambda p: -p.stat().st_mtime)
    return hits[0] if hits else None


def submit_job(cfg: Dict[str, Any], prompt: str, node: Optional[str] = None, agent: str = "default", workdir: str = "",
               sync: str = "", tags: Optional[List[str]] = None, timeout: Optional[int] = None, pull: bool = True,
               resume: str = "", force: bool = False) -> str:
    if resume:
        resume = re.sub(r"[^A-Za-z0-9-]", "", resume)
        f = session_file(resume)
        if f and now() - f.stat().st_mtime < 120 and not force:
            die(E_USAGE, f"session {resume} was written {int(now() - f.stat().st_mtime)}s ago — close it here first (or --force); "
                         "two machines must never write one session at the same time")
    jid = time.strftime("j%Y%m%d-%H%M%S-") + "%04x" % random.randrange(65536)
    (STATE_DIR / "prompts").mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "prompts" / f"{jid}.md").write_text(prompt)
    if node and node not in cfg["nodes"]:
        die(E_NOT_FOUND, f"no node '{node}'")
    if sync:
        sync = str(Path(sync).expanduser().resolve())
        if not Path(sync).is_dir():
            die(E_USAGE, f"--sync folder not found: {sync}")
    save_job({"id": jid, "status": "queued", "created": now(), "node": None, "want_node": node, "agent": agent,
              "workdir": workdir, "sync": sync, "pull": pull, "tags": tags or [], "attempts": 0, "resume": resume,
              "timeout": timeout or cfg["defaults"]["job_timeout_min"], "history": []})
    return jid


DEVICE_TAGS = ("android-device", "ios-device")


def pick_node(cfg: Dict[str, Any], j: Dict[str, Any], metrics: Dict[str, Dict[str, Any]], load: Dict[str, int],
              dev_load: Optional[Dict[Tuple[str, str], int]] = None) -> Optional[str]:
    dev_load = dev_load or {}
    best, best_score = None, -1e9
    for name, n in cfg["nodes"].items():
        if n.get("pending") or (j.get("want_node") and name != j["want_node"]):
            continue
        st = metrics.get(name, {})
        if node_health(cfg, name, st) != "ok":
            continue
        if not j.get("want_node") and (j.get("avoid") or {}).get(name, 0) > now():
            continue
        devs = st.get("devices") or {}
        have = set(n.get("tags") or []) | {st.get("os", "").lower()}
        have |= {f"{k}-device" for k, v in devs.items() if int(v or 0) > 0}
        if not set(j.get("tags") or []) <= have:
            continue
        # one job per attached phone: a job tagged android-device / ios-device needs a free device of that kind
        if any(int(devs.get(t[:-7], 0) or 0) <= dev_load.get((name, t), 0) for t in (j.get("tags") or []) if t in DEVICE_TAGS):
            continue
        agent = j.get("agent") or "default"
        if agent == "bench" and st.get("os", "").lower() == "windows":
            continue
        if agent not in ("default", "custom", "bench") and agent not in (st.get("agents") or []):
            continue
        if agent == "default" and not st.get("agents"):
            continue
        busy = load.get(name, 0)
        if busy >= int(n.get("max_jobs", 2)):
            continue
        score = st.get("mem_free_pct", 0) + min(st.get("disk_free_gb", 0), 50) / 5 - 30 * busy - 10 * float(st.get("load", 0)) / max(int(st.get("cpus", 1)), 1)
        if score > best_score:
            best, best_score = name, score
    return best


def skills_tar(cfg: Dict[str, Any]) -> Tuple[bytes, str]:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for rel in cfg["skills"].get("paths", SKILL_PATHS):
            p = HOME / rel
            if p.exists():
                tar.add(str(p), arcname=rel, filter=lambda ti: None if "/." in ti.name and ti.name.split("/")[-1].startswith(".git") else ti)
    data = buf.getvalue()
    return data, hashlib.sha256(data).hexdigest()[:16]


def push_skills(cfg: Dict[str, Any], name: str, force: bool = False) -> bool:
    data, h = skills_tar(cfg)
    st = read_json(node_state_path(name), {})
    if not force and st.get("skills_hash") == h:
        return False
    n = Node(name, cfg)
    if n.ctl("recv", "~", input=data, timeout=300).returncode == 0 and n.ctl("skills-hash", h).returncode == 0:
        st["skills_hash"] = h
        write_json(node_state_path(name), st)
        return True
    return False


def dispatch(cfg: Dict[str, Any], j: Dict[str, Any], name: str) -> bool:
    n = Node(name, cfg)
    node = cfg["nodes"][name]
    if cfg["skills"].get("auto", True):
        try:
            push_skills(cfg, name)
        except Exception:  # skills are best effort
            pass
    wd = j.get("workdir") or node.get("dir") or "~/scaleout-work"
    if j.get("sync"):
        wd = ("~/.scaleout/work/" if not n.windows else "~\\.scaleout\\work\\") + j["id"]
        tar = run(["tar", "-cf", "-", "-C", j["sync"], "--exclude", "node_modules", "--exclude", ".gradle", "--exclude", "build", "--exclude", ".dart_tool", "."]).stdout
        if n.ctl("recv", wd, input=tar, timeout=900).returncode != 0:
            return False
    prompt = (STATE_DIR / "prompts" / f"{j['id']}.md").read_bytes()
    if j.get("agent") != "bench":
        prompt += ("\n\n---\n(agent-scaleout) Save every screenshot, screen recording, log or report you produce into the folder "
                   "given by the SCALEOUT_ARTIFACTS environment variable; it is sent back to the requester automatically. "
                   "List those file names in your final answer.\n").encode()
    res = n.ctl("submit", j["id"], j.get("agent") or "default", str(j["timeout"]), wd, j.get("resume") or "", input=prompt, timeout=60)
    if res.returncode != 0:
        return False
    j.update({"status": "dispatched", "node": name, "dispatched_at": now(), "last_seen": now(), "node_workdir": wd})
    j["history"].append({"t": now(), "event": "dispatched", "node": name})
    return True


def collect(cfg: Dict[str, Any], j: Dict[str, Any], metrics: Dict[str, Dict[str, Any]]) -> None:
    d = cfg["defaults"]
    name = j["node"]
    if name not in cfg["nodes"]:
        return requeue(cfg, j, "node removed")
    st = metrics.get(name, {})
    if not st.get("online"):
        if now() - j.get("last_seen", now()) > d["offline_grace_min"] * 60:
            requeue(cfg, j, f"node {name} offline")
        return
    n = Node(name, cfg)
    res = n.ctl("state", j["id"], timeout=20)
    if res.returncode != 0:  # went away between the health check and now
        if now() - j.get("last_seen", now()) > d["offline_grace_min"] * 60:
            requeue(cfg, j, f"node {name} offline")
        return
    try:
        s = json.loads(res.stdout.decode().strip().splitlines()[-1])
    except (ValueError, IndexError):
        return
    j["last_seen"] = now()
    state = s.get("state")
    if state == "running":
        j["status"] = "running"
        if s.get("heartbeat_age", 0) > d["stall_min"] * 60:
            n.ctl("cancel", j["id"])
            requeue(cfg, j, f"stalled on {name} (no heartbeat {s['heartbeat_age']}s)")
    elif state == "done":
        out = n.ctl("result", j["id"], timeout=120).stdout
        (STATE_DIR / "results").mkdir(parents=True, exist_ok=True)
        (STATE_DIR / "results" / f"{j['id']}.out").write_bytes(out)
        rc = int(s.get("rc", 1))
        if s.get("mem_mb") is not None:
            j["mem_mb"] = int(s.get("mem_mb") or 0)
        if rc != 0:
            (STATE_DIR / "results" / f"{j['id']}.err").write_bytes(n.ctl("err", j["id"], "60").stdout)
        if int(s.get("artifacts", 0) or 0) > 0:
            adir = STATE_DIR / "results" / j["id"]
            if int(s.get("artifacts_kb", 0) or 0) > d.get("max_artifacts_mb", 1024) * 1024:
                j["artifacts_note"] = f"{s['artifacts']} files ({int(s['artifacts_kb']) // 1024} MB) left on {name}: larger than max_artifacts_mb"
            else:
                data = n.ctl("artifacts", j["id"], timeout=1800).stdout
                if data:
                    adir.mkdir(parents=True, exist_ok=True)
                    subprocess.run(["tar", "-xf", "-", "-C", str(adir)], input=data)
                    j["artifacts"] = sorted(str(p.relative_to(adir)) for p in adir.rglob("*") if p.is_file())
                    j["artifacts_dir"] = str(adir)
        if j.get("sync") and j.get("pull", True):
            data = n.ctl("send", j["node_workdir"], timeout=900).stdout
            if data:
                subprocess.run(["tar", "-xf", "-", "-C", j["sync"]], input=data)
        n.ctl("rm", j["id"])
        j.update({"status": "done" if rc == 0 else ("cancelled" if rc == 130 else "failed"), "rc": rc, "finished_at": now()})
        j["history"].append({"t": now(), "event": j["status"], "node": name, "rc": rc})
    elif state == "queued" and now() - j.get("dispatched_at", now()) > d.get("node_wait_min", 10) * 60:
        # the node accepted it but never started it (its runner holds jobs while memory is short): try another node
        n.ctl("rm", j["id"])
        j.setdefault("avoid", {})[name] = now() + 30 * 60
        j["history"].append({"t": now(), "event": "moved", "node": name, "why": f"not started on {name} in {d.get('node_wait_min', 10)} min"})
        j.update({"status": "queued", "node": None})
    elif state in ("missing", "lost"):
        requeue(cfg, j, f"job vanished on {name}")


def requeue(cfg: Dict[str, Any], j: Dict[str, Any], why: str) -> None:
    j["attempts"] = j.get("attempts", 0) + 1
    j["history"].append({"t": now(), "event": "retry", "node": j.get("node"), "why": why})
    if j.get("node"):
        orphans = read_json(STATE_DIR / "orphans.json", [])
        orphans.append([j["node"], j["id"]])
        write_json(STATE_DIR / "orphans.json", orphans)
    if j["attempts"] > cfg["defaults"]["retries"]:
        j.update({"status": "failed", "rc": -1, "error": why, "finished_at": now()})
    else:
        j.update({"status": "queued", "node": None})


def maintain(cfg: Dict[str, Any], metrics: Dict[str, Dict[str, Any]]) -> None:
    """Resource recovery and self-healing on nodes."""
    d = cfg["defaults"]
    for name, st in metrics.items():
        if not st.get("online"):
            continue
        n = Node(name, cfg)
        if st.get("disk_free_gb", 99) < d["cleanup_below_disk_gb"] and now() - st.get("last_cleanup", 0) > 3600:
            res = n.ctl("cleanup", "aggressive" if st.get("disk_free_gb", 99) < d["min_free_disk_gb"] else "", timeout=900)
            st["last_cleanup"] = now()
            st["cleanup_note"] = res.stdout.decode().strip()[-120:]
            write_json(node_state_path(name), st)
        if st.get("runner") is False and now() - st.get("last_repair", 0) > 600:
            n.ctl("repair", timeout=120)
            st["last_repair"] = now()
            write_json(node_state_path(name), st)
        if now() - st.get("last_gc", 0) > 6 * 3600:
            n.ctl("gc", str(d["keep_job_days"]))
            st["last_gc"] = now()
            write_json(node_state_path(name), st)
    if d.get("auto_tune", True):
        last = read_json(STATE_DIR / "tune.json", {}).get("t", 0)
        if now() - last > 24 * 3600:
            try:
                tune(cfg, apply=True, quiet=True)
            finally:
                write_json(STATE_DIR / "tune.json", {"t": now()})
    orphans = read_json(STATE_DIR / "orphans.json", [])
    left = []
    for name, jid in orphans:
        st = metrics.get(name, {})
        if name in cfg["nodes"] and st.get("online"):
            Node(name, cfg).ctl("cancel", jid)
            Node(name, cfg).ctl("rm", jid)
        elif name in cfg["nodes"]:
            left.append([name, jid])
    write_json(STATE_DIR / "orphans.json", left)


class TickLock:
    def __init__(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.f = open(STATE_DIR / "tick.lock", "w")
        self.ok = False

    def __enter__(self) -> bool:
        try:
            fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.ok = True
        except OSError:
            self.ok = False
        return self.ok

    def __exit__(self, *exc: Any) -> None:
        if self.ok:
            fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def tick(verbose: bool = False) -> None:
    with TickLock() as got:
        if not got:
            return
        cfg = load_cfg()
        metrics = refresh_metrics(cfg)
        jobs = all_jobs()
        for j in jobs:
            if j.get("status") in ("dispatched", "running"):
                collect(cfg, j, metrics)
                save_job(j)
        load: Dict[str, int] = {}
        dev_load: Dict[Tuple[str, str], int] = {}
        for j in jobs:
            if j.get("status") in ("dispatched", "running") and j.get("node"):
                load[j["node"]] = load.get(j["node"], 0) + 1
                for t in j.get("tags") or []:
                    if t in DEVICE_TAGS:
                        dev_load[(j["node"], t)] = dev_load.get((j["node"], t), 0) + 1
        for name, st in metrics.items():  # work the node already has (other orchestrators, manual jobs)
            reported = int(st.get("running", 0)) + int(st.get("queued", 0))
            reported -= sum(1 for j in jobs if j.get("node") == name and j.get("finished_at", 0) >= st.get("ts", 0))
            load[name] = max(load.get(name, 0), reported)
        for j in jobs:
            if j.get("status") != "queued":
                continue
            name = pick_node(cfg, j, metrics, load, dev_load)
            if name and dispatch(cfg, j, name):
                load[name] = load.get(name, 0) + 1
                for t in j.get("tags") or []:
                    if t in DEVICE_TAGS:
                        dev_load[(name, t)] = dev_load.get((name, t), 0) + 1
                save_job(j)
                if verbose:
                    say(f"dispatched {j['id']} -> {name}")
        maintain(cfg, metrics)


def wait_job(jid: str, max_s: int) -> Tuple[int, str]:
    start = time.time()
    while True:
        tick()
        j = load_job(jid)
        if j["status"] in ("done", "failed", "cancelled"):
            return job_output(j)
        if time.time() - start >= max_s:
            return E_RUNNING, f"still {j['status']}{' on ' + j['node'] if j.get('node') else ''} — run: scaleout wait {jid}"
        time.sleep(5)


def job_output(j: Dict[str, Any]) -> Tuple[int, str]:
    out_f = STATE_DIR / "results" / f"{j['id']}.out"
    raw = out_f.read_text(errors="replace") if out_f.exists() else ""
    text = raw
    try:  # Claude Code JSON output
        obj = json.loads(raw)
        if isinstance(obj, dict) and "result" in obj:
            text = str(obj.get("result") or "")
            if obj.get("is_error"):
                return E_JOB_FAILED, text
    except ValueError:
        pass
    if j["status"] == "done":
        return E_OK, text
    err_f = STATE_DIR / "results" / f"{j['id']}.err"
    err = err_f.read_text(errors="replace") if err_f.exists() else j.get("error", "")
    return E_JOB_FAILED, (text + ("\n" if text else "") + err).strip()


# ── commands: jobs ───────────────────────────────────────────────────────────
def read_prompt(a) -> str:
    text = Path(a.file).read_text() if a.file else sys.stdin.read()
    if not text.strip():
        die(E_USAGE, "empty prompt (pass -f FILE or pipe it in)")
    return text


def cmd_submit(a) -> None:
    cfg = load_cfg()
    jid = submit_job(cfg, read_prompt(a), node=None if a.node in (None, "auto") else a.node, agent=a.agent,
                     workdir=a.dir or "", sync=a.sync or "", tags=a.tags.split(",") if a.tags else [], timeout=a.timeout, pull=not a.no_pull, resume=a.resume, force=a.force)
    tick()
    print(jid)


def cmd_run(a) -> None:
    cfg = load_cfg()
    jid = submit_job(cfg, read_prompt(a), node=None if a.node in (None, "auto") else a.node, agent=a.agent,
                     workdir=a.dir or "", sync=a.sync or "", tags=a.tags.split(",") if a.tags else [], timeout=a.timeout, pull=not a.no_pull, resume=a.resume, force=a.force)
    print(jid, file=sys.stderr)
    rc, text = wait_job(jid, a.max)
    print(text + artifacts_text(load_job(jid)))
    sys.exit(rc)


def cmd_wait(a) -> None:
    rc, text = wait_job(a.job, a.max)
    print(text + artifacts_text(load_job(a.job)))
    sys.exit(rc)


def artifacts_text(j: Dict[str, Any]) -> str:
    if j.get("artifacts"):
        return f"\n\n[artifacts] {j['artifacts_dir']}\n" + "\n".join(f"  {x}" for x in j["artifacts"][:50])
    return f"\n\n[artifacts] {j['artifacts_note']}" if j.get("artifacts_note") else ""


def cmd_artifacts(a) -> None:
    j = load_job(a.job)
    if not j.get("artifacts"):
        die(E_NOT_FOUND, j.get("artifacts_note") or "no artifacts for this job")
    src = Path(j["artifacts_dir"])
    if a.to:
        dst = Path(a.to).expanduser(); dst.mkdir(parents=True, exist_ok=True)
        for rel in j["artifacts"]:
            (dst / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src / rel, dst / rel)
        src = dst
    for rel in j["artifacts"]:
        print(src / rel)


def cmd_result(a) -> None:
    j = load_job(a.job)
    rc, text = job_output(j) if j["status"] in ("done", "failed", "cancelled") else (E_RUNNING, f"still {j['status']}")
    print(text + artifacts_text(j))
    sys.exit(rc)


def cmd_logs(a) -> None:
    j = load_job(a.job)
    for h in j.get("history", []):
        say(time.strftime("%F %T", time.localtime(h["t"])) + "  " + " ".join(f"{k}={v}" for k, v in h.items() if k != "t"))
    if j.get("node") and j["status"] in ("dispatched", "running"):
        say("--- node stderr")
        say(Node(j["node"], load_cfg()).ctl("err", j["id"], "40").stdout.decode())
    err_f = STATE_DIR / "results" / f"{j['id']}.err"
    if err_f.exists():
        say("--- stderr")
        say(err_f.read_text(errors="replace"))


def cmd_cancel(a) -> None:
    j = load_job(a.job)
    if j.get("node") and j["status"] in ("dispatched", "running"):
        Node(j["node"], load_cfg()).ctl("cancel", j["id"])
    j.update({"status": "cancelled", "finished_at": now()})
    j["history"].append({"t": now(), "event": "cancelled"})
    save_job(j)
    say("cancelled")


def cmd_jobs(a) -> None:
    jobs = all_jobs()[-a.limit:]
    if not jobs:
        say("no jobs")
    for j in jobs:
        age = time.strftime("%m-%d %H:%M", time.localtime(j.get("created", 0)))
        say(f"{j['id']}  {j['status']:<10} {j.get('node') or '-':<14} tries={j.get('attempts', 0)}  {age}")


def cmd_gc(a) -> None:
    cfg = load_cfg()
    cut = now() - cfg["defaults"]["keep_job_days"] * 86400
    n = 0
    for j in all_jobs():
        if j.get("status") in ("done", "failed", "cancelled") and j.get("finished_at", now()) < cut:
            for p in (job_path(j["id"]), STATE_DIR / "prompts" / f"{j['id']}.md", STATE_DIR / "results" / f"{j['id']}.out", STATE_DIR / "results" / f"{j['id']}.err"):
                p.unlink(missing_ok=True)
            n += 1
    say(f"removed {n} old jobs")


# ── commands: nodes ──────────────────────────────────────────────────────────
def cmd_nodes(a) -> None:
    cfg = load_cfg()
    if not cfg["nodes"]:
        say("no nodes — scaleout add NAME user@host  |  scaleout invite NAME")
    for name, n in cfg["nodes"].items():
        say(f"{name:<14} {n.get('os', '?'):<8} user={n.get('user', '?')} lan={n.get('lan') or '-'} tunnel={n.get('port') or '-'} "
            f"agent={n.get('agent', 'claude')} max={n.get('max_jobs', 2)} tags={','.join(n.get('tags') or []) or '-'}{' (invite pending)' if n.get('pending') else ''}")


def fmt_devices(st: Dict[str, Any]) -> str:
    d = st.get("devices") or {}
    parts = [f"{k[0].upper()}{v}" for k, v in (("android", d.get("android", 0)), ("ios", d.get("ios", 0))) if int(v or 0)]
    return " ".join(parts) or "-"


def cmd_status(a) -> None:
    cfg = load_cfg()
    names = a.names or None
    metrics = refresh_metrics(cfg, names, max_age=0)
    say(f"orchestrator {socket.gethostname().split('.')[0]} · agent-scaleout {VERSION}")
    say(f"{'node':<14}{'state':<12}{'route':<8}{'mem free':>9}{'disk free':>11}{'load':>8}{'jobs':>7}  {'devices':<9}agents")
    for name in sorted(metrics):
        st = metrics[name]
        h = node_health(cfg, name, st)
        route = "-" if not st.get("online") else ("lan" if str(st.get("route", "")).endswith("-lan") else "tunnel")
        say(f"{name:<14}{h:<12}{route:<8}{str(st.get('mem_free_pct', '-')) + '%':>9}{str(st.get('disk_free_gb', '-')) + 'GB':>11}"
            f"{str(st.get('load', '-')):>8}{str(st.get('running', 0)) + '+' + str(st.get('queued', 0)):>7}  {fmt_devices(st):<9}{','.join(st.get('agents') or []) or '-'}")
    q = [j for j in all_jobs() if j.get("status") in ("queued", "dispatched", "running")]
    say(f"queue: {sum(1 for j in q if j['status'] == 'queued')} waiting · {sum(1 for j in q if j['status'] != 'queued')} running")


def cmd_doctor(a) -> None:
    cfg = load_cfg()
    print(Node(a.name, cfg).ctl("doctor", timeout=60).stdout.decode())


def cmd_exec(a) -> None:
    cfg = load_cfg()
    n = Node(a.name, cfg)
    cmd = " ".join(a.command)
    if not cmd:
        die(E_USAGE, "exec NAME -- COMMAND")
    if not n.windows:
        cmd = f"export PATH=$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH; {cmd}"
    r = n.shell(cmd)
    sys.stdout.write(r.stdout.decode())
    sys.stderr.write(r.stderr.decode())
    sys.exit(r.returncode)


def cmd_cleanup(a) -> None:
    cfg = load_cfg()
    for name in a.names or list(cfg["nodes"]):
        r = Node(name, cfg).ctl("cleanup", "deep" if a.deep else ("aggressive" if a.aggressive else ""), timeout=900)
        say(f"{name}: {r.stdout.decode().strip() or r.stderr.decode().strip()}")


def cmd_sync_skills(a) -> None:
    cfg = load_cfg()
    data, h = skills_tar(cfg)
    say(f"skills bundle {len(data) // 1024} KB ({', '.join(p for p in cfg['skills']['paths'] if (HOME / p).exists())}) · {h}")
    for name in a.names or [n for n, v in cfg["nodes"].items() if not v.get("pending")]:
        try:
            say(f"{name}: {'sent' if push_skills(cfg, name, force=True) else ('failed' if Node(name, cfg).route() else 'unreachable')}")
        except Exception as e:  # noqa: BLE001
            say(f"{name}: failed ({e})")


def cmd_update(a) -> None:
    cfg = load_cfg()
    for name in a.names or list(cfg["nodes"]):
        n = Node(name, cfg)
        node = cfg["nodes"][name]
        if n.windows:
            r = n.ctl("update", input=node_package("windows"), timeout=300)
        else:
            up = n.shell("mkdir -p ~/.scaleout/pkg && tar -xzf - -C ~/.scaleout/pkg", input=node_package("unix"), timeout=300)
            args = ["--name", name, "--dir", node.get("dir", "~/scaleout-work"), "--max-jobs", str(node.get("max_jobs", 2)), "--agent", node.get("agent", "claude")]
            if node.get("port"):
                args += tunnel_args(cfg, node["port"])
            r = n.shell("bash ~/.scaleout/pkg/setup.sh " + " ".join(shlex.quote(x) for x in args), timeout=300) if up.returncode == 0 else up
        say(f"{name}: {'updated' if r.returncode == 0 else 'failed: ' + r.stderr.decode().strip()[-200:]}")


def cmd_config(a) -> None:
    cfg = load_cfg()
    if a.key is None:
        print(json.dumps(cfg["defaults"], indent=2))
        return
    if a.node:
        target = cfg["nodes"][a.node] if a.node in cfg["nodes"] else die(E_NOT_FOUND, f"no node '{a.node}'")
    else:
        target = cfg["defaults"]
    if a.value is None:
        print(target.get(a.key))
        return
    v: Any = a.value
    if re.fullmatch(r"-?\d+", v):
        v = int(v)
    elif a.key == "tags":
        v = [t for t in v.split(",") if t]
    target[a.key] = v
    save_cfg(cfg)
    say(f"{a.node + '.' if a.node else ''}{a.key} = {v}")


# ── commands: daemon ─────────────────────────────────────────────────────────
def cmd_tick(a) -> None:
    tick(verbose=True)


def cmd_daemon(a) -> None:
    say(f"agent-scaleout orchestrator daemon {VERSION} · every {a.interval}s")
    while True:
        try:
            tick(verbose=True)
        except Exception as e:  # keep running whatever happens
            say(f"tick error: {e}")
        time.sleep(a.interval)


def cmd_install_daemon(a) -> None:
    exe = shutil.which("scaleout") or str(HOME / ".local" / "bin" / "scaleout")
    log = STATE_DIR / "daemon.log"
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if platform.system() == "Darwin":
        p = HOME / "Library" / "LaunchAgents" / "dev.scaleout.orchestrator.plist"
        p.write_text(f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>dev.scaleout.orchestrator</string>
<key>ProgramArguments</key><array><string>{exe}</string><string>daemon</string></array>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
<key>EnvironmentVariables</key><dict><key>PATH</key><string>{HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
<key>StandardOutPath</key><string>{log}</string><key>StandardErrorPath</key><string>{log}</string>
</dict></plist>
''')
        uid = os.getuid()
        run(["launchctl", "bootout", f"gui/{uid}/dev.scaleout.orchestrator"])
        r = run(["launchctl", "bootstrap", f"gui/{uid}", str(p)])
        say("daemon installed (launchd: dev.scaleout.orchestrator)" if r.returncode == 0 else f"failed: {r.stderr.decode()}")
    else:
        d = HOME / ".config" / "systemd" / "user"
        d.mkdir(parents=True, exist_ok=True)
        (d / "scaleout-orchestrator.service").write_text(f"[Unit]\nDescription=agent-scaleout orchestrator\n[Service]\nExecStart={exe} daemon\nRestart=always\nRestartSec=10\n[Install]\nWantedBy=default.target\n")
        run(["systemctl", "--user", "daemon-reload"])
        r = run(["systemctl", "--user", "enable", "--now", "scaleout-orchestrator.service"])
        say("daemon installed (systemd --user: scaleout-orchestrator)" if r.returncode == 0 else f"failed: {r.stderr.decode()}")


# ── commands: GitHub runners ─────────────────────────────────────────────────
def gh_scope(a) -> Tuple[str, str]:
    if a.org:
        return f"orgs/{a.org}", f"https://github.com/{a.org}"
    if a.repo:
        return f"repos/{a.repo}", f"https://github.com/{a.repo}"
    die(E_USAGE, "--org ORG or --repo OWNER/REPO")
    return "", ""


def gh_token(scope: str, kind: str) -> str:
    r = run(["gh", "api", "-X", "POST", f"{scope}/actions/runners/{kind}", "-q", ".token"])
    if r.returncode != 0:
        die(E_USAGE, f"gh api failed: {r.stderr.decode().strip()} (needs `gh auth login` with admin rights on the org/repo)")
    return r.stdout.decode().strip()


def cmd_runner(a) -> None:
    cfg = load_cfg()
    scope, url = gh_scope(a)
    if a.action == "ls":
        r = run(["gh", "api", f"{scope}/actions/runners", "-q", '.runners[] | "\\(.name)\\t\\(.status)\\t\\(.busy)\\t\\([.labels[].name] | join(","))"'])
        print(r.stdout.decode() or r.stderr.decode())
        return
    if not a.name:
        die(E_USAGE, f"runner {a.action} NODE ...")
    n = Node(a.name, cfg)
    rname = a.runner_name or f"{a.name}-scaleout"
    if a.action == "add":
        st = refresh_metrics(cfg, [a.name], max_age=0).get(a.name, {})
        labels = a.labels or ",".join(["scaleout", a.name, str(st.get("os", "")).lower(), str(st.get("arch", ""))])
        r = n.ctl("runner-install", url, gh_token(scope, "registration-token"), rname, labels, timeout=900)
    else:
        r = n.ctl("runner-remove", rname, gh_token(scope, "remove-token"), timeout=300)
    print((r.stdout + r.stderr).decode().strip())
    sys.exit(0 if r.returncode == 0 else E_JOB_FAILED)


# ── commands: bench / stats ──────────────────────────────────────────────────
# Reference machine = Apple M1 (8 cores), measured with `ctl bench` / `ctl loadjob`.
# Score 100 = one M1 Mac mini with 8 GB running one mobile job (agent + build + test) at a time.
REF_SINGLE_MBPS = 55.0
REF_MULTI_MBPS = 230.0
REF_LOAD_S = 54.0
JOB_GB, BASE_GB = 3.5, 3.0   # memory one mobile job needs (agent + build, phone attached) / what the OS keeps


def bench_score(b: Dict[str, Any]) -> Dict[str, Any]:
    single = float(b.get("single_mbps", 0)) / REF_SINGLE_MBPS
    multi = float(b.get("multi_mbps", 0)) / REF_MULTI_MBPS
    mem_slots = max(0, int((float(b.get("mem_total_gb", 0)) - BASE_GB) // JOB_GB))
    cpu_slots = max(1, int(2 * multi + 0.25))
    slots = min(mem_slots, cpu_slots)
    speed = 1 / (0.6 + 0.4 / max(single, 0.1))   # ~60 % of a job is waiting for the model, ~40 % is CPU work
    return {"single_x": round(single, 2), "multi_x": round(multi, 2), "mem_slots": mem_slots, "cpu_slots": cpu_slots,
            "slots": slots, "score": int(round(slots * speed * 100))}


def bench_history() -> List[Dict[str, Any]]:
    return read_json(STATE_DIR / "bench.json", [])


def per10k(score: int, price: Any) -> str:
    return f"{score / (int(price) / 10000):.1f}" if price else "-"


def run_bench(names: List[str], apply: bool = False, quiet_table: bool = False) -> None:
    cfg = load_cfg()
    res: Dict[str, Any] = {}

    def one(name: str) -> None:
        try:
            r = Node(name, cfg).ctl("bench", timeout=300)
            res[name] = json.loads(r.stdout.decode().strip().splitlines()[-1])
        except Exception as e:  # unreachable, not supported ...
            res[name] = {"error": str(e)[:80] or "unreachable"}
    threads = [threading.Thread(target=one, args=(n,)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    hist = [h for h in bench_history() if "nodes" in h]
    prev = dict(hist[-1]["nodes"]) if hist else {}
    cur = {k: v for k, v in prev.items() if k in cfg["nodes"]}
    for name in names:
        b = res.get(name) or {}
        if "error" in b or "single_mbps" not in b:
            say(f"  {name}: no benchmark ({b.get('error', 'no answer')})")
            continue
        sc = bench_score(b)
        cur[name] = {**sc, "model": b.get("model", ""), "mem_gb": b.get("mem_total_gb"), "cpus": b.get("cpus"),
                     "disk_mbps": b.get("disk_write_mbps"), "price": cfg["nodes"][name].get("price")}
        if apply and sc["slots"] >= 1 and cfg["nodes"][name].get("max_jobs") != sc["slots"] and not cfg["nodes"][name].get("max_jobs_fixed"):
            cfg["nodes"][name]["max_jobs"] = sc["slots"]
            Node(name, cfg).ctl("conf", "MAX_JOBS", str(sc["slots"]))
            save_cfg(cfg)
            say(f"  {name}: max_jobs set to {sc['slots']} (measured)")
    total = sum(v["score"] for v in cur.values())
    before = sum(v["score"] for k, v in prev.items() if k in cfg["nodes"])
    full = bench_history()
    full.append({"t": now(), "nodes": cur, "total": total})
    write_json(STATE_DIR / "bench.json", full[-200:])
    if not quiet_table:
        say(f"{'node':<14}{'model':<30}{'mem':>5}{'cores':>6}{'1-core':>8}{'all':>7}{'jobs':>6}{'score':>7}{'price':>11}{'per 10k':>9}")
        for name in sorted(cur):
            v = cur[name]
            say(f"{name:<14}{str(v.get('model', ''))[:29]:<30}{str(v.get('mem_gb', '?')) + 'G':>5}{str(v.get('cpus', '?')):>6}"
                f"{v['single_x']:>7.2f}x{v['multi_x']:>6.2f}x{v['slots']:>6}{v['score']:>7}"
                f"{(format(v['price'], ',') if v.get('price') else '-'):>11}{per10k(v['score'], v.get('price')):>9}")
    for name in names:
        if name in cur and name not in prev:
            v = cur[name]
            paid = f" for {v['price']:,} -> {per10k(v['score'], v['price'])} points per 10k" if v.get("price") else ""
            say(f"  {name} added {v['score']} points{paid}")
    x = f" ({total / before:.2f}x)" if before else ""
    say(f"pool score {total}  (was {before}, {total - before:+d}{x}) · 100 = one M1 8GB Mac mini doing one mobile job")


def run_load(cfg: Dict[str, Any], jobs: int, max_s: int) -> None:
    names = [n for n, v in cfg["nodes"].items() if not v.get("pending") and v.get("os", "").lower() != "windows"]
    slots = sum(int(cfg["nodes"][n].get("max_jobs", 2)) for n in names)
    jobs = jobs or max(4, 3 * slots)
    say(f"load test: {jobs} synthetic build jobs (~2.5 GB, 4 busy cores, 512 MB written each) on {len(names)} node(s), {slots} slot(s)")
    ids = []
    for _ in range(jobs):
        jid = submit_job(cfg, "load test", agent="bench", timeout=20)
        j = load_job(jid)
        j["bench"] = True
        save_job(j)
        ids.append(jid)
    start = time.time()
    while True:
        tick()
        js = [load_job(i) for i in ids]
        if all(j["status"] in ("done", "failed", "cancelled") for j in js):
            break
        if time.time() - start > max_s:
            for j in js:
                if j["status"] not in ("done", "failed", "cancelled"):
                    cmd_cancel(type("A", (), {"job": j["id"]})())
            break
        time.sleep(2)
    wall = time.time() - start
    per: Dict[str, List[int]] = {}
    for jid in ids:
        j = load_job(jid)
        if j["status"] == "done":
            try:
                ms = json.loads((STATE_DIR / "results" / f"{jid}.out").read_text().strip().splitlines()[-1]).get("ms", 0)
            except (ValueError, IndexError, OSError):
                ms = 0
            per.setdefault(j["node"], []).append(int(ms))
        for f in (job_path(jid), STATE_DIR / "prompts" / f"{jid}.md", STATE_DIR / "results" / f"{jid}.out", STATE_DIR / "results" / f"{jid}.err"):
            try:
                f.unlink()
            except OSError:
                pass
    done = sum(len(v) for v in per.values())
    score = int(round(done * REF_LOAD_S / wall * 100)) if wall else 0
    for n in sorted(per):
        say(f"  {n:<14}{len(per[n]):>3} jobs · avg {sum(per[n]) / len(per[n]) / 1000:.0f}s each")
    say(f"{done}/{jobs} done in {wall / 60:.1f} min · {done / wall * 3600:.0f} jobs/hour")
    hist = bench_history()
    last = next((h["load"] for h in reversed(hist) if "load" in h), None)
    hist.append({"t": now(), "load": {"jobs": jobs, "done": done, "wall_s": round(wall), "score": score, "nodes": sorted(per)}})
    write_json(STATE_DIR / "bench.json", hist[-200:])
    was = f" (last load test {last['score']}, {score - last['score']:+d})" if last else ""
    say(f"load score {score}{was} · 100 = one M1 8GB Mac mini working through the same jobs one at a time")


def cmd_bench(a) -> None:
    cfg = load_cfg()
    if a.history:
        for h in bench_history():
            t = time.strftime("%Y-%m-%d %H:%M", time.localtime(h["t"]))
            if "load" in h:
                L = h["load"]
                say(f"{t}  load  score {L['score']:>5}  {L['done']}/{L['jobs']} jobs in {L['wall_s']}s on {','.join(L['nodes'])}")
            else:
                say(f"{t}  pool  score {h['total']:>5}  " + " ".join(f"{k}={v['score']}" for k, v in sorted(h["nodes"].items())))
        return
    if a.load:
        return run_load(cfg, a.jobs, a.max)
    names = a.names or [n for n, v in cfg["nodes"].items() if not v.get("pending")]
    run_bench(names, apply=a.apply)


def p90(xs: List[float]) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(0.9 * (len(xs) - 1))))]


def tune(cfg: Dict[str, Any], apply: bool = False, quiet: bool = False, days: int = 14) -> None:
    """max_jobs from measured memory: (total - idle use - 1 GB headroom) / p90 of the memory one real job added,
    capped by the CPU slots `scaleout bench` measured."""
    since = now() - days * 86400
    per: Dict[str, List[float]] = {}
    for j in all_jobs():
        if j.get("bench") or j.get("created", 0) < since or j.get("status") != "done" or not j.get("node") or not j.get("mem_mb"):
            continue
        per.setdefault(j["node"], []).append(j["mem_mb"] / 1024)
    hist = [h for h in bench_history() if "nodes" in h]
    bench = hist[-1]["nodes"] if hist else {}
    if not quiet:
        say(f"{'node':<14}{'jobs':>5}{'mem/job p90':>13}{'idle use':>10}{'total':>7}{'cpu slots':>11}{'max_jobs':>10}  ->")
    for name, n in cfg["nodes"].items():
        st = read_json(node_state_path(name), {})
        xs = per.get(name, [])
        cur = int(n.get("max_jobs", 2))
        if len(xs) < 5 or not st.get("mem_total_gb"):
            if not quiet:
                say(f"{name:<14}{len(xs):>5}{'-':>13}{'-':>10}{str(st.get('mem_total_gb', '-')) + 'G':>7}{'-':>11}{cur:>10}  (needs 5 finished jobs)")
            continue
        job_gb = max(0.5, p90(xs))
        idle = float(st.get("idle_used_gb") or BASE_GB)
        mem_slots = max(1, int((float(st["mem_total_gb"]) - idle - 1) // job_gb))
        cpu = int((bench.get(name) or {}).get("cpu_slots") or 99)
        rec = max(1, min(mem_slots, cpu, 8))
        fixed = n.get("max_jobs_fixed")
        if not quiet:
            say(f"{name:<14}{len(xs):>5}{job_gb:>12.1f}G{idle:>9.1f}G{str(st['mem_total_gb']) + 'G':>7}{(cpu if cpu != 99 else '-'):>11}{cur:>10}  {rec}{' (fixed by --max-jobs)' if fixed else ''}")
        if apply and not fixed and rec != cur:
            n["max_jobs"] = rec
            if Node(name, cfg).ctl("conf", "MAX_JOBS", str(rec)).returncode == 0:
                save_cfg(cfg)
                if not quiet:
                    say(f"  {name}: max_jobs {cur} -> {rec}")


def cmd_tune(a) -> None:
    tune(load_cfg(), apply=a.apply, days=a.days)


def cmd_stats(a) -> None:
    since = now() - a.days * 86400
    rows: Dict[str, Dict[str, List[float]]] = {}
    for j in all_jobs():
        if j.get("bench") or j.get("created", 0) < since or not j.get("node"):
            continue
        r = rows.setdefault(j["node"], {"done": [], "failed": [], "run": [], "wait": [], "mem": []})
        if j.get("mem_mb"):
            r["mem"].append(j["mem_mb"] / 1024)
        if j.get("status") in ("done", "failed"):
            r[j["status"]].append(1)
            disp = [h["t"] for h in j.get("history", []) if h.get("event") == "dispatched"]
            if disp and j.get("finished_at"):
                r["run"].append(j["finished_at"] - disp[-1])
                r["wait"].append(disp[0] - j["created"])

    def avg(xs: List[float]) -> str:
        return f"{sum(xs) / len(xs) / 60:.1f}m" if xs else "-"
    say(f"last {a.days} day(s)")
    say(f"{'node':<14}{'done':>6}{'failed':>8}{'avg run':>10}{'avg wait':>10}{'mem/job p90':>13}")
    for n in sorted(rows):
        r = rows[n]
        mem = f"{p90(r['mem']):.1f}G" if r["mem"] else "-"
        say(f"{n:<14}{len(r['done']):>6}{len(r['failed']):>8}{avg(r['run']):>10}{avg(r['wait']):>10}{mem:>13}")
    allw = [w for r in rows.values() for w in r["wait"]]
    say(f"pool: {sum(len(r['done']) for r in rows.values())} done · average wait before a node took a job: {avg(allw)}" if allw else "pool: no finished jobs yet")


# ── cli ──────────────────────────────────────────────────────────────────────
def main(argv: Optional[List[str]] = None) -> None:
    p = argparse.ArgumentParser(prog="scaleout", description="Spread coding-agent jobs across the machines on your network.")
    p.add_argument("--version", action="version", version=f"agent-scaleout {VERSION}")
    sp = p.add_subparsers(dest="cmd", required=True)

    s = sp.add_parser("init", help="set up this machine as the orchestrator")
    s.add_argument("--user"); s.add_argument("--lan"); s.add_argument("--ssh-port", type=int)
    s.add_argument("--tunnel-host", help="public SSH host of this orchestrator for off-LAN nodes ('' disables)")
    s.add_argument("--tunnel-proxy", help="ProxyCommand nodes use to reach --tunnel-host, e.g. 'cloudflared access ssh --hostname %%h'")
    s.add_argument("--tunnel-ssh-port", type=int)
    s.set_defaults(fn=cmd_init)

    def node_opts(s: argparse.ArgumentParser) -> None:
        s.add_argument("--dir", default="~/scaleout-work", help="default working folder on the node")
        s.add_argument("--max-jobs", type=int, default=None, help="parallel jobs (default: 2, or what `scaleout bench` measures)")
        s.add_argument("--price", type=int, help="what the machine cost you (any currency) - bench shows score per 10k")
        s.add_argument("--agent", default="claude", help="claude | codex | gemini | any command reading the prompt on stdin")
        s.add_argument("--tags", default="", help="comma separated, e.g. xcode,gpu")

    s = sp.add_parser("add", help="attach a macOS/Linux node you can already SSH to (password asked once)")
    s.add_argument("name"); s.add_argument("target", nargs="?", help="user@host")
    s.add_argument("--ssh", help="use an existing ssh alias instead of user@host"); s.add_argument("--lan")
    s.add_argument("--no-tunnel", action="store_true"); s.add_argument("--no-test", action="store_true")
    node_opts(s); s.set_defaults(fn=cmd_add)

    s = sp.add_parser("invite", help="print a one-line installer to paste on a new node")
    s.add_argument("name"); s.add_argument("--os", choices=["unix", "windows"], default="unix"); s.add_argument("--hours", type=int, default=24)
    node_opts(s); s.set_defaults(fn=cmd_invite)

    s = sp.add_parser("_enroll", help=argparse.SUPPRESS); s.add_argument("name"); s.set_defaults(fn=cmd_enroll)
    s = sp.add_parser("remove", help="detach a node and undo its setup"); s.add_argument("name"); s.set_defaults(fn=cmd_remove)
    s = sp.add_parser("nodes", help="list nodes"); s.set_defaults(fn=cmd_nodes)
    s = sp.add_parser("status", help="live health of nodes and the queue"); s.add_argument("names", nargs="*"); s.set_defaults(fn=cmd_status)
    s = sp.add_parser("doctor", help="tools and services on a node"); s.add_argument("name"); s.set_defaults(fn=cmd_doctor)

    def job_opts(s: argparse.ArgumentParser) -> None:
        s.add_argument("-f", "--file"); s.add_argument("-w", "--node", default="auto")
        s.add_argument("-a", "--agent", default="default"); s.add_argument("-d", "--dir", help="working folder on the node")
        s.add_argument("--sync", help="orchestrator folder copied to the node before the job and copied back after")
        s.add_argument("--no-pull", action="store_true", help="with --sync: do not copy results back")
        s.add_argument("--tags", default=""); s.add_argument("--timeout", type=int, help="minutes")
        s.add_argument("--resume", default="", help="continue this Claude Code session on the node (needs shared folders, see README)")
        s.add_argument("--force", action="store_true", help="with --resume: even if the session was just written here")

    s = sp.add_parser("submit", help="queue a job and print its id"); job_opts(s); s.set_defaults(fn=cmd_submit)
    s = sp.add_parser("run", help="queue a job and wait for its result"); job_opts(s); s.add_argument("-m", "--max", type=int, default=540); s.set_defaults(fn=cmd_run)
    s = sp.add_parser("wait", help="wait for a job (exit 2 if still running)"); s.add_argument("job"); s.add_argument("-m", "--max", type=int, default=540); s.set_defaults(fn=cmd_wait)
    s = sp.add_parser("result", help="print a finished job's answer"); s.add_argument("job"); s.set_defaults(fn=cmd_result)
    s = sp.add_parser("artifacts", help="screenshots/videos/logs a job saved (paths; --to DIR copies them)")
    s.add_argument("job"); s.add_argument("--to"); s.set_defaults(fn=cmd_artifacts)
    s = sp.add_parser("logs", help="history and stderr of a job"); s.add_argument("job"); s.set_defaults(fn=cmd_logs)
    s = sp.add_parser("cancel", help="cancel a job"); s.add_argument("job"); s.set_defaults(fn=cmd_cancel)
    s = sp.add_parser("jobs", help="recent jobs"); s.add_argument("-n", "--limit", type=int, default=20); s.set_defaults(fn=cmd_jobs)
    s = sp.add_parser("gc", help="forget old finished jobs on the orchestrator"); s.set_defaults(fn=cmd_gc)

    s = sp.add_parser("exec", help="run a shell command on a node"); s.add_argument("name"); s.add_argument("command", nargs=argparse.REMAINDER); s.set_defaults(fn=cmd_exec)
    s = sp.add_parser("cleanup", help="reclaim disk on nodes"); s.add_argument("names", nargs="*"); s.add_argument("--aggressive", action="store_true", help="also empty caches under the node's home")
    s.add_argument("--deep", action="store_true", help="--aggressive plus machine-wide: docker prune, unavailable simulators"); s.set_defaults(fn=cmd_cleanup)
    s = sp.add_parser("sync-skills", help="send ~/.claude skills/agents/commands to nodes"); s.add_argument("names", nargs="*"); s.set_defaults(fn=cmd_sync_skills)
    s = sp.add_parser("update", help="push this version to nodes"); s.add_argument("names", nargs="*"); s.set_defaults(fn=cmd_update)
    s = sp.add_parser("config", help="show/set defaults (or --node NAME settings)"); s.add_argument("key", nargs="?"); s.add_argument("value", nargs="?"); s.add_argument("--node"); s.set_defaults(fn=cmd_config)

    s = sp.add_parser("bench", help="score nodes (100 = one M1 8GB Mac mini) and track how the pool grows; --load runs a load test")
    s.add_argument("names", nargs="*"); s.add_argument("--apply", action="store_true", help="set each node's max_jobs to the measured slots")
    s.add_argument("--load", action="store_true", help="push synthetic build jobs through the real scheduler and time them")
    s.add_argument("--jobs", type=int, default=0, help="with --load: how many (default 3 per slot)")
    s.add_argument("--max", type=int, default=1800, help="with --load: give up after this many seconds")
    s.add_argument("--history", action="store_true"); s.set_defaults(fn=cmd_bench)
    s = sp.add_parser("tune", help="suggest (or --apply) max_jobs per node from the memory real jobs used")
    s.add_argument("--apply", action="store_true"); s.add_argument("--days", type=int, default=14); s.set_defaults(fn=cmd_tune)
    s = sp.add_parser("stats", help="real work done per node: jobs, failures, run and wait times, memory per job")
    s.add_argument("--days", type=int, default=7); s.set_defaults(fn=cmd_stats)
    s = sp.add_parser("tick", help="run one scheduling round"); s.set_defaults(fn=cmd_tick)
    s = sp.add_parser("daemon", help="schedule forever (installed by install-daemon)"); s.add_argument("--interval", type=int, default=15); s.set_defaults(fn=cmd_daemon)
    s = sp.add_parser("install-daemon", help="run the scheduler in the background (launchd / systemd --user)"); s.set_defaults(fn=cmd_install_daemon)

    s = sp.add_parser("runner", help="GitHub Actions self-hosted runners on nodes")
    s.add_argument("action", choices=["add", "remove", "ls"]); s.add_argument("name", nargs="?")
    s.add_argument("--org"); s.add_argument("--repo"); s.add_argument("--labels"); s.add_argument("--runner-name")
    s.set_defaults(fn=cmd_runner)

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
