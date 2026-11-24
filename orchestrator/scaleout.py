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
import calendar
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
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

VERSION = "0.9.12"
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
    "cleanup_below_disk_gb": 12,  # ask the node to reclaim disk below this; above min_free_disk_gb + what a heavy job fills (4),
                                  # or a node refuses heavy jobs and nothing frees the disk for them
    "cleanup_target_disk_gb": 20,  # ... and to drop idle repo work trees (least recently used first) until this much is free
    "job_timeout_min": 180,
    "check_model": "",           # the model for jobs that only check and report (is_check); empty: the node's own model
    "check_effort": "high",      # the effort of a job on another model than the node's: a machine may set its effort for its own model only
    "stall_min": 5,              # running job whose heartbeat is older than this is considered dead
    "node_wait_min": 10,
    "mem_reserve_gb": 1.5,
    "max_load_per_cpu": 2.0,     # a busy node whose 1-minute load is above this × cores gets no new job (simulators are CPU-heavy)
    "reap_idle_min": 20,         # simulators/emulators with no test running this long are shut down (0 = never), here and on nodes       # memory a node always keeps free when the scheduler fits jobs into it
    "max_artifacts_mb": 1024,    # a job's screenshots/videos/logs larger than this stay on the node
    "auto_tune": True,           # daily: set each node's max_jobs from its measured CPU slots (not for --max-jobs nodes)         # job handed to a node but not started there (node short on memory) -> move it
    "offline_grace_min": 10,     # node unreachable this long -> its jobs are retried elsewhere
    "retries": 2,
    "keep_job_days": 3,
    "log_days": 90,              # the job log (one line per finished job) for `scaleout insights`
    "history_days": 30,          # the 5-minute samples
    "metrics_ttl_s": 15,
    "port_base": 2201,
}
SKILL_PATHS = [".claude/skills", ".claude/agents", ".claude/commands"]
# Harness Studio skills run ~/harness_workspace/.bin/run.py on their folder in ~/harness_workspace/my: sent with the skills
# (without run logs), so those skills work on nodes too
SKILL_EXTRA = ["harness_workspace/.bin", "harness_workspace/my"]
SKILL_SKIP = {"log", "runs", ".harness-runs", "node_modules", ".git", ".dart_tool", "build", ".gradle"}


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


LOCAL = "orchestrator"   # this machine, when it runs jobs itself (`scaleout local on`)


def local_home() -> Path:
    return STATE_DIR / "local"


def local_env(n: Dict[str, Any]) -> Dict[str, str]:
    return dict(os.environ, SCALEOUT_NODE_HOME=str(local_home()))


LOCAL_DENY = ["Bash(git push:*)", "Bash(git branch -D:*)", "Bash(git branch -d:*)", "Bash(git branch --delete:*)",
              "Bash(git update-ref:*)", "Bash(git worktree:*)", "Bash(git tag -d:*)", "Bash(scaleout:*)", "Edit(~/.claude/**)"]


def local_install(force: bool = False) -> None:
    """The orchestrator's own executor: the node scripts, run from ~/.local/state/scaleout/local (no SSH, no copies).
    Files are swapped by rename, so running jobs keep their scripts."""
    b, src = local_home(), PKG_ROOT / "node" / "unix"
    stamp = hashlib.sha1(b"".join((src / f).read_bytes() for f in ("ctl.sh", "runner.sh", "token-rules.md")) + VERSION.encode()).hexdigest()
    if not force and (b / "bin" / ".stamp").exists() and (b / "bin" / ".stamp").read_text() == stamp:
        return
    for d in ("bin", "queue", "jobs", "work", "settings"):
        (b / d).mkdir(parents=True, exist_ok=True)
    for f, dst in (("ctl.sh", "ctl"), ("runner.sh", "runner.sh"), ("token-rules.md", "token-rules.md")):
        tmp = b / "bin" / f"{dst}.new"
        tmp.write_bytes((src / f).read_bytes().replace(b"__VERSION__", VERSION.encode()))
        tmp.chmod(0o755)
        os.replace(tmp, b / "bin" / dst)
    if not (b / "node.conf").exists():
        # KEEP_DEVICES: simulators the sessions here started are theirs (the daemon still stops idle ones after reap_idle_min)
        (b / "node.conf").write_text("NAME=orchestrator\nRUNNER=manual\nKEEP_DEVICES=1\nMAX_JOBS=2\nMAX_LOAD_PER_CPU=2\n")
    s = b / "settings" / "claude.json"
    if not s.exists():
        d = json.loads((PKG_ROOT / "node" / "claude-settings.json").read_text())
        deny = d.setdefault("permissions", {}).setdefault("deny", [])
        deny += [r for r in LOCAL_DENY if r not in deny]   # the work tree shares refs with the user's repository
        s.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
    (b / "bin" / ".stamp").write_text(stamp)


def local_runner_alive() -> bool:
    try:
        os.kill(int((local_home() / "runner.lock" / "pid").read_text().strip()), 0)
        return True
    except (OSError, ValueError):
        return False


def start_local_runner(n: Dict[str, Any]) -> None:
    """Start the runner for this machine's own jobs (it exits by itself when the queue stays empty).
    Its own session: restarting the daemon (launchd stops the daemon's process group) never stops running jobs."""
    if local_runner_alive():
        return
    with open(local_home() / "runner.log", "ab") as log:
        subprocess.Popen(["/bin/bash", str(local_home() / "bin" / "runner.sh")], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         env=local_env(n), cwd=str(HOME), start_new_session=True)


def local_ctl(n: Dict[str, Any], verb: str, *args: str, input: Optional[bytes] = None, timeout: float = 60) -> subprocess.CompletedProcess:
    local_install()
    cmd = ["bash", str(local_home() / "bin" / "ctl"), verb, *args]
    try:
        r = subprocess.run(cmd, input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=local_env(n), timeout=timeout,
                           **({} if input is not None else {"stdin": subprocess.DEVNULL}))
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, b"", b"timeout")
    if verb == "submit":
        start_local_runner(n)
    return r


def job_cap(name: str, node: Dict[str, Any], st: Optional[Dict[str, Any]] = None) -> int:
    """How many jobs a machine may run at once. max_jobs is its share (it weighs the even spread); the hard cap is its
    thread count, so load (2× cores) and memory decide when it is full. A machine whose max_jobs was fixed by hand
    (--max-jobs, `scaleout local on`) keeps it as the hard cap."""
    share = max(int(node.get("max_jobs", 2) or 2), 1)
    if node.get("max_jobs_fixed") or node.get("local"):
        return share
    st = st if st is not None else read_json(node_state_path(name), {})
    return max(share, int(st.get("cpus") or 0))


def lan_answers(host: str, port: int, timeout: float = 0.6) -> bool:
    """A quick TCP probe: is the node's SSH port on the LAN reachable from here right now?"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class Node:
    """A node and how to reach it."""

    _route_cache: Dict[str, Tuple[str, float]] = {}

    def __init__(self, name: str, cfg: Dict[str, Any]):
        if name not in cfg["nodes"]:
            die(E_NOT_FOUND, f"no node '{name}' (scaleout nodes)")
        self.name, self.cfg, self.n = name, cfg, cfg["nodes"][name]

    @property
    def local(self) -> bool:
        return bool(self.n.get("local"))

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
        if self.local:
            return "local"
        hit = Node._route_cache.get(self.name)
        if hit and not fresh and time.time() - hit[1] < 60:
            return hit[0] or None
        last = read_json(node_state_path(self.name), {}).get("route")
        order = sorted(self.aliases(), key=lambda x: x != last)   # the route that worked last time first
        lan = f"scaleout-{self.name}-lan"
        if lan in order and order[0] != lan and lan_answers(self.n["lan"], int(self.n.get("ssh_port", 22))):
            order = [lan] + [x for x in order if x != lan]   # a direct LAN connection beats a tunnel whenever it answers
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
        if self.local:
            return local_ctl(self.n, verb, *args, input=input, timeout=timeout)
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
        if self.local:
            return subprocess.run(["bash", "-c", command], input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  env=local_env(self.n), timeout=timeout)
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


def free_stale_tunnel(port: int) -> List[int]:
    """Kill this user's sshd session still listening on a node's reverse-tunnel port.

    A node that sleeps (a laptop lid) or loses its network leaves that session behind: it keeps the port, nothing
    answers behind it (ssh times out in the banner exchange), and the node's tunnel cannot bind the port again when
    it comes back, so the node stays offline until someone kills the session."""
    if not shutil.which("lsof"):
        return []
    out = run(["lsof", "-nP", "-a", "-t", f"-iTCP:{port}", "-sTCP:LISTEN", "-u", str(os.getuid())], timeout=10).stdout.decode()
    killed: List[int] = []
    for pid in sorted({int(p) for p in out.split() if p.isdigit()}):
        if "sshd" not in run(["ps", "-o", "comm=", "-p", str(pid)], timeout=5).stdout.decode():
            continue   # only a tunnel's sshd session, never another program on that port
        try:
            os.kill(pid, 15)
            killed.append(pid)
        except OSError:
            pass
    return killed


def tunnel_args(cfg: Dict[str, Any], port: int) -> List[str]:
    t = cfg["orchestrator"].get("tunnel") or {}
    if not t.get("host"):
        return []
    a = ["--tunnel-host", t["host"], "--tunnel-user", cfg["orchestrator"]["user"], "--tunnel-port", str(port),
         "--tunnel-ssh-port", str(t.get("ssh_port", 22))]
    if t.get("proxy"):
        a += ["--tunnel-proxy", t["proxy"]]
    if cfg["orchestrator"].get("lan"):   # straight to the orchestrator whenever the node can reach it on the LAN (no proxy needed)
        a += ["--tunnel-lan", f"{cfg['orchestrator']['lan']}:{cfg['orchestrator'].get('ssh_port', 22)}"]
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
    if (HOME / ".claude").is_dir():
        say("Claude Code: run `scaleout claude-setup` to install the scaleout plugin (offload hook, rules, subagent, commands)")
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
    if name == LOCAL:
        die(E_USAGE, f"'{LOCAL}' is this machine: `scaleout local on` lets it run jobs itself")
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
N=__N__; ROUTE_LAN=__LAN__; ROUTE_HOST=__HOST__; ROUTE_PROXY=__PROXY__; OU=__OU__; OSP=__OSP__; SO_BREW=__BREW__; SO_CLAUDE=__CLAUDE__
SO_PREP=__PREP__; SO_AUTOLOGIN=__AUTOLOGIN__
K="$(mktemp)"; trap 'rm -f "$K"' EXIT
printf '%s' __KEY__ | base64 --decode > "$K"; chmod 600 "$K"
if [ "$(uname -s)" = Linux ] && [ "$SO_PREP" = 1 ] && command -v apt-get >/dev/null; then
  echo "[0/5] Linux node basics — your password is asked once"
  echo "  packages: SSH server, curl, git, build tools, JDK 17 (waits if automatic updates hold the package lock; a few minutes)"
  sudo -v
  sudo apt-get -o DPkg::Lock::Timeout=900 update -q
  sudo env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=900 install -y -q \
    openssh-server curl git netcat-openbsd unzip zip xz-utils build-essential clang cmake ninja-build pkg-config libgtk-3-dev openjdk-17-jdk-headless
  echo "  never sleeps (a node that sleeps takes no jobs)"
  sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target >/dev/null
  echo "  services run without anyone logged in (loginctl enable-linger)"
  sudo loginctl enable-linger "$(whoami)"
  if [ "$SO_AUTOLOGIN" = 1 ] && [ -f /etc/gdm3/custom.conf ]; then
    echo "  logs in to the desktop by itself after a restart"
    sudo sed -i "s/^#\? *AutomaticLoginEnable.*/AutomaticLoginEnable=true/; s/^#\? *AutomaticLogin *=.*/AutomaticLogin=$(whoami)/" /etc/gdm3/custom.conf
    grep -q '^AutomaticLogin=' /etc/gdm3/custom.conf || sudo sed -i "/^\[daemon\]/a AutomaticLoginEnable=true\nAutomaticLogin=$(whoami)" /etc/gdm3/custom.conf
  fi
fi
echo "[1/5] SSH server (how the orchestrator reaches this machine)"
if ! (nc -z -w 2 127.0.0.1 22 || nc -z -G 2 127.0.0.1 22) >/dev/null 2>&1; then
  echo "  turning on the SSH server — your password may be asked once"
  if [ "$(uname -s)" = Darwin ]; then sudo launchctl enable system/com.openssh.sshd 2>/dev/null || true; sudo launchctl bootstrap system /System/Library/LaunchDaemons/ssh.plist 2>/dev/null || sudo systemsetup -setremotelogin on
  else (command -v apt-get >/dev/null && sudo apt-get install -y openssh-server >/dev/null) || (command -v dnf >/dev/null && sudo dnf install -y openssh-server >/dev/null) || true; sudo systemctl enable --now ssh 2>/dev/null || sudo systemctl enable --now sshd; fi
fi
echo "[2/5] Homebrew + Claude Code"
NEW_CLAUDE=0
if [ "$SO_BREW" = 1 ] && [ "$(uname -s)" = Darwin ] && ! command -v brew >/dev/null && [ ! -x /opt/homebrew/bin/brew ]; then
  echo "  installing Homebrew (the Xcode command line tools come with it, a few minutes; your password may be asked)"
  sudo -v && NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" </dev/null \
    || echo "  ! Homebrew did not install — later: /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
fi
if [ -x /opt/homebrew/bin/brew ]; then
  eval "$(/opt/homebrew/bin/brew shellenv)"
  grep -q 'brew shellenv' ~/.zprofile 2>/dev/null || echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
  echo "  Homebrew ok"
fi
export PATH="$HOME/.local/bin:$PATH"
if [ "$SO_CLAUDE" = 1 ] && ! command -v claude >/dev/null; then
  echo "  installing Claude Code"
  if curl -fsSL https://claude.ai/install.sh | bash >/dev/null 2>&1 && command -v claude >/dev/null; then NEW_CLAUDE=1; echo "  Claude Code ok"
  else echo "  ! Claude Code did not install — later: curl -fsSL https://claude.ai/install.sh | bash"; fi
elif command -v claude >/dev/null; then echo "  Claude Code ok"; fi
# ~/.local/bin (Claude Code, and later fvm / node) on the PATH of new terminals: shells only add it when it existed at login
for rc in ~/.bashrc ~/.zshrc; do
  { [ -f "$rc" ] || [ "$rc" = ~/.bashrc -a "$(uname -s)" = Linux ] || [ "$rc" = ~/.zshrc -a "$(uname -s)" = Darwin ]; } || continue
  grep -q '\.local/bin' "$rc" 2>/dev/null || printf '\n# Claude Code and node tools (agent-scaleout)\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$rc"
done
echo "[3/5] route to the orchestrator"
EN=(ssh -i "$K" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=15 -p "$OSP")
if [ -n "$ROUTE_LAN" ] && (nc -z -w 3 "$ROUTE_LAN" "$OSP" || nc -z -G 3 "$ROUTE_LAN" "$OSP") >/dev/null 2>&1; then T="$OU@$ROUTE_LAN"; echo "  LAN ($ROUTE_LAN)"
elif [ -n "$ROUTE_HOST" ]; then
  if [ -n "$ROUTE_PROXY" ]; then P="${ROUTE_PROXY%% *}"; command -v "$P" >/dev/null || { command -v brew >/dev/null && brew install "$P" >/dev/null; } || true
    if ! command -v "$P" >/dev/null && [ "$P" = cloudflared ] && [ "$(uname -s)" = Linux ]; then
      case "$(uname -m)" in x86_64) A=amd64 ;; aarch64) A=arm64 ;; *) A=arm ;; esac; mkdir -p ~/.local/bin
      curl -fsSL -o ~/.local/bin/cloudflared "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$A" && chmod 755 ~/.local/bin/cloudflared
    fi
    EN+=(-o "ProxyCommand=$(command -v "$P") ${ROUTE_PROXY#* }"); fi
  T="$OU@$ROUTE_HOST"; echo "  $ROUTE_HOST"
else echo "  cannot reach the orchestrator at $ROUTE_LAN" >&2; exit 3; fi
echo "[4/5] install"
mkdir -p ~/.scaleout/pkg
"${EN[@]}" -n "$T" pkg-unix | tar -xzf - -C ~/.scaleout/pkg
"${EN[@]}" -n "$T" args > ~/.scaleout/pkg/args
eval "bash ~/.scaleout/pkg/setup.sh $(cat ~/.scaleout/pkg/args)" > ~/.scaleout/setup.out
echo "[5/5] register + exchange keys"
"${EN[@]}" "$T" register < ~/.scaleout/setup.out > ~/.scaleout/orchestrator.pub
mkdir -p ~/.ssh; chmod 700 ~/.ssh; touch ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys
grep -qF "$(cut -d' ' -f2 ~/.scaleout/orchestrator.pub)" ~/.ssh/authorized_keys || cat ~/.scaleout/orchestrator.pub >> ~/.ssh/authorized_keys
grep -q '^SCALEOUT_AGENTS \[\]' ~/.scaleout/setup.out && echo "  ! no coding agent found — install and sign in to one (e.g. Claude Code: curl -fsSL https://claude.ai/install.sh | bash)"
[ "$NEW_CLAUDE" = 1 ] && echo "  ! one more step on this machine: run  claude  once and sign in (the node runs jobs with that login)"
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
if ("__CLAUDE__" -eq "1" -and -not (Get-Command claude -ErrorAction SilentlyContinue)) {
  Write-Host "  installing Claude Code"; try { Invoke-RestMethod https://claude.ai/install.ps1 | Invoke-Expression; Write-Host "  ! run  claude  once here and sign in" } catch { Write-Host "  ! Claude Code did not install: irm https://claude.ai/install.ps1 | iex" }
}
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
    long_line = ""   # the paste-anywhere line, shown too when a short LAN line is printed
    if win:
        script = (INVITE_WIN.replace("__N__", name).replace("__LAN__", o.get("lan", "")).replace("__OU__", o["user"])
                  .replace("__OSP__", str(o.get("ssh_port", 22))).replace("__KEY__", key).replace("__CLAUDE__", "0" if a.no_claude else "1"))
        b64 = base64.b64encode(script.encode()).decode()
        line = f"iex ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{b64}')))"
        where = "Paste this into PowerShell on the Windows node (run as Administrator the first time):"
    else:
        script = (INVITE_UNIX.replace("__N__", q(name)).replace("__LAN__", q(o.get("lan", ""))).replace("__HOST__", q(t.get("host", "")))
                  .replace("__PROXY__", q(t.get("proxy", ""))).replace("__OU__", q(o["user"])).replace("__OSP__", q(str(o.get("ssh_port", 22))))
                  .replace("__KEY__", q(key)).replace("__BREW__", "0" if a.no_brew else "1").replace("__CLAUDE__", "0" if a.no_claude else "1")
                  .replace("__PREP__", "0" if a.no_prep else "1").replace("__AUTOLOGIN__", "1" if a.autologin else "0"))
        b64 = base64.b64encode(script.encode()).decode()
        line = f'bash -c "$(echo {b64} | base64 --decode)"'
        where = "Paste this into a terminal on the node (macOS / Linux):"
        if a.serve and o.get("lan"):   # short enough to type: this machine serves the script on the LAN until it is used
            token = base64.urlsafe_b64encode(os.urandom(9)).decode()
            sp_ = inv / "serve"; sp_.mkdir(exist_ok=True)
            (sp_ / token).write_text("#!/bin/bash\n" + script)
            with socket.socket() as probe:   # a free port on the LAN address
                probe.bind((o["lan"], 0))
                port_ = probe.getsockname()[1]
            subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "_serve", str(sp_), o["lan"], str(port_), str(a.serve), token],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            url = f"http://{o['lan']}:{port_}/{token}"
            long_line = line
            line = f'bash <(curl -fsSL {url} 2>/dev/null || wget -qO- {url})'
            where = f"Type this in a terminal on the node (same network; served for {a.serve} min or until used):"
    say(f"invite for '{name}' — single use, valid {a.hours} h{f' · tunnel port {port}' if port else ''}")
    say(where)
    say()
    say(line)
    say()
    if long_line:
        say("Not on the same network? Paste this instead:")
        say()
        say(long_line)
        say()
    say("The SSH server is turned on if needed (password asked once). The node's coding agent must be signed in.")
    if not win and not a.no_prep:
        say("On Linux (apt) it also installs the build tools, keeps the machine awake and lets the node run while nobody is logged in"
            + (" and logs in to the desktop by itself" if a.autologin else "") + ".")


def cmd_serve(a) -> None:
    """`scaleout invite --serve`: hand out one invite script on the LAN, then stop (never called by hand)."""
    import http.server
    folder, token = Path(a.folder), a.token
    end = time.time() + int(a.minutes) * 60

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            f = folder / token
            if self.path.strip("/") != token or not f.exists():
                self.send_response(404); self.end_headers(); return
            body = f.read_bytes()
            self.send_response(200); self.send_header("Content-Type", "text/plain"); self.send_header("Content-Length", str(len(body))); self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass
    srv = http.server.HTTPServer((a.host, int(a.port)), H)
    srv.timeout = 5
    while time.time() < end and (folder / token).exists():   # the invite used (or redone) removes the file: stop early
        srv.handle_request()
    srv.server_close()
    shutil.rmtree(folder, ignore_errors=True)


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
        args = ["--name", name, "--dir", node.get("dir", "~/scaleout-work"), "--max-jobs", str(job_cap(name, node)), "--agent", node.get("agent", "claude")]
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


def cmd_local(a) -> None:
    """This machine runs jobs itself while it has room (memory, CPU); the rest go to nodes or wait in the queue."""
    cfg = load_cfg()
    if a.action == "off":
        cfg["nodes"].pop(LOCAL, None)
        save_cfg(cfg)
        node_state_path(LOCAL).unlink(missing_ok=True)
        say("this machine no longer takes jobs (running ones finish)")
        return
    node = cfg["nodes"].get(LOCAL, {})
    if a.action == "on" or not node:
        darwin = platform.system() == "Darwin"
        node.update({"local": True, "os": platform.system(), "agent": "claude", "max_jobs_fixed": True})
        node.setdefault("added", time.strftime("%F %T"))
        node.setdefault("tags", ["mac", "xcode"] if darwin and shutil.which("xcodebuild") else [platform.system().lower()])
        node.setdefault("max_jobs", 2)
        node.setdefault("cpu_slots", 2)
        node["score_bias"] = min(float(node.get("score_bias", -40)), -40)   # last choice: it orchestrates and takes what only it can or overflow
    if a.max_jobs:
        node["max_jobs"] = node["cpu_slots"] = a.max_jobs
    node.pop("dir", None)
    node.pop("wt_dir", None)   # work stays on the internal disk (see dispatch)
    cfg["nodes"][LOCAL] = node
    save_cfg(cfg)
    local_install(force=True)
    say(f"this machine runs up to {node['max_jobs']} job(s) itself while it has room · work in {local_home()}"
        " · `scaleout local off` to stop")


def cmd_remove(a) -> None:
    cfg = load_cfg()
    if a.name == LOCAL:
        return cmd_local(argparse.Namespace(action="off", max_jobs=0))
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
            st.pop("stale_tunnel_since", None)
            st.pop("slow", None)
            if int(m.get("running", 1) or 0) == 0 and m.get("mem_total_gb"):
                st["idle_used_gb"] = round(float(m["mem_total_gb"]) * (100 - float(m.get("mem_free_pct", 0))) / 100, 1)
        elif res.returncode == 124 and st.get("online") and now() - int(st.get("last_ok", 0) or 0) < 60 * int(cfg["defaults"].get("offline_grace_min", 10)):
            # it answered a moment ago and only measuring timed out (load far above its cores, e.g. an emulator plus a CI
            # build): the machine is busy, not gone. It takes no new job until it answers in time again.
            st.update({"slow": now(), "ts": now(), "error": "metrics timed out: busy"})
        else:
            err = res.stderr.decode()
            st.update({"online": False, "ts": now(), "error": err.strip()[-200:]})
            port = cfg["nodes"].get(name, {}).get("port")
            dead = "banner exchange" in err or "timed out" in err or err.strip() == "unreachable"
            if port and dead and socket_busy(int(port)):
                # the tunnel port is open but nothing behind it answers (ssh times out, or no route works at all):
                # give a slow node a minute, then free the port
                st.setdefault("stale_tunnel_since", now())
                if now() - int(st["stale_tunnel_since"]) >= 60:
                    pids = free_stale_tunnel(int(port))
                    if pids:
                        say(f"{name}: freed its stale reverse tunnel on port {port} (pid {', '.join(map(str, pids))}) so it can reconnect")
                    st.pop("stale_tunnel_since", None)
            else:
                st.pop("stale_tunnel_since", None)
        write_json(node_state_path(name), st)
        out[name] = st

    threads = [threading.Thread(target=one, args=(n,)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return out


TEST_RE = r"maestro|xcodebuild .*test|flutter (test|drive)|flutter_tester|gradlew.*(connected|AndroidTest)|instrument|playwright|simctl (launch|install|io|spawn)|detox|appium"


_REAP = {"t": 0.0}


def local_reap(idle_min: int) -> Optional[str]:
    """Shut down simulators/emulators on this machine when no test has run for idle_min minutes."""
    marker = STATE_DIR / "reap.active"
    if now() - _REAP["t"] < 60:   # once a minute is plenty
        return None
    _REAP["t"] = now()
    sims = re.findall(r"\(([0-9A-F-]{36})\) \(Booted\)", run(["xcrun", "simctl", "list", "devices", "booted"], timeout=30).stdout.decode()) \
        if shutil.which("xcrun") else []
    emus = re.findall(r"^(emulator-\d+)\s", run(["adb", "devices"], timeout=30).stdout.decode(), re.M) if shutil.which("adb") else []
    if not sims and not emus:
        marker.unlink(missing_ok=True)
        return None
    if run(["pgrep", "-f", TEST_RE], timeout=10).returncode == 0:   # extended regex already; macOS pgrep has no -E
        marker.touch()
        return None
    if not marker.exists():
        marker.touch()
        return None
    if now() - marker.stat().st_mtime < idle_min * 60:
        return None
    for u in sims:
        run(["xcrun", "simctl", "shutdown", u], timeout=60)
    for e in emus:
        run(["adb", "-s", e, "emu", "kill"], timeout=30)
    if run(["pgrep", "-x", "Simulator"], timeout=10).returncode == 0:
        run(["osascript", "-e", 'quit app "Simulator"'], timeout=30)
    marker.unlink(missing_ok=True)
    note = f"{time.strftime('%F %T')} stopped {len(sims)} simulator(s), {len(emus)} emulator(s) idle for {idle_min} min"
    with open(STATE_DIR / "reap.log", "a") as f:
        f.write(note + "\n")
    return note


AGENT_BLOCKS = STATE_DIR / "agent-blocks.json"   # {node: {agent: {"until": t, "why": text}}}
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def agent_trouble(text: str) -> Optional[Tuple[str, int]]:
    """(why, until) when a job's output says its agent cannot work on that machine: a usage limit (until its reset, or an
    hour) or a lost login (a day: a person has to sign in again). None for an ordinary failure."""
    low = text.lower()
    if "usage_limit_reached" in low or "hit your weekly limit" in low or "hit your limit" in low or "rate_limit" in low and "limit" in low:
        until = now() + 3600
        m = re.search(r"resets\s+([a-z]{3})[a-z]*\s+(\d{1,2})(?:\s+at)?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)", low)
        if m and m.group(1) in MONTHS:
            hour = int(m.group(3)) % 12 + (12 if m.group(5) == "pm" else 0)
            lt = time.localtime()
            try:
                t = time.mktime((lt.tm_year, MONTHS[m.group(1)], int(m.group(2)), hour, int(m.group(4) or 0), 0, 0, 0, -1))
                if t < now() - 86400:
                    t = time.mktime((lt.tm_year + 1, MONTHS[m.group(1)], int(m.group(2)), hour, int(m.group(4) or 0), 0, 0, 0, -1))
                until = int(t) + 120
            except (OverflowError, ValueError):
                pass
        return "usage limit", until
    if any(x in low for x in ("refresh token has expired", "not logged in", "please run /login", "invalid api key",
                              "authentication_error", "oauth token has expired", "please log out and sign in again")):
        return "signed out", now() + 86400
    return None


def agent_blocked(name: str, agent: str) -> Optional[Dict[str, Any]]:
    b = (read_json(AGENT_BLOCKS, {}).get(name) or {}).get(agent)
    return b if b and b.get("until", 0) > now() else None


def block_agent(name: str, agent: str, why: str, until: int) -> None:
    data = read_json(AGENT_BLOCKS, {})
    data.setdefault(name, {})[agent] = {"until": int(until), "why": why}
    write_json(AGENT_BLOCKS, data)


def node_health(cfg: Dict[str, Any], name: str, st: Dict[str, Any]) -> str:
    d = cfg["defaults"]
    if not st.get("online"):
        return "offline"
    if st.get("slow"):
        return "busy"      # answered late last time: reachable, no new job until it answers in time
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


JOB_LOG = STATE_DIR / "log" / "jobs.jsonl"   # one line per finished job, kept log_days (job files themselves: keep_job_days)


def log_job(j: Dict[str, Any]) -> None:
    node, fin, start = j.get("node"), float(j.get("finished_at") or now()), j.get("dispatched_at")
    u = j.get("usage") or {}
    row = {"id": j["id"], "t": int(fin), "node": node, "tags": j.get("tags") or [], "agent": j.get("agent") or "default",
           "kind": job_kind(j), "repo": bool(j.get("repo")), "status": j.get("status"), "rc": j.get("rc"),
           "wait_s": int(start - j["created"]) if start and j.get("created") else None,
           "run_s": int(fin - start) if start else None, "attempts": j.get("attempts", 0),
           "mem_mb": j.get("mem_mb"), "mem_gb_est": round(job_mem_gb(j, node), 1) if node else None,
           "tokens": sum(int(u.get(k, 0) or 0) for k in ("in", "out", "cache_read", "cache_write")) or None,
           "start_tokens": j.get("start_tokens"),
           "events": [[h.get("event"), (h.get("why") or "")[:100], h.get("node")] for h in j.get("history", [])
                      if h.get("event") not in ("dispatched", None)],
           "error": (j.get("error") or "")[:160] or None}
    JOB_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(JOB_LOG, "a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def save_job(j: Dict[str, Any]) -> None:
    if j.get("status") in ("done", "failed", "cancelled") and not j.get("logged") and not j.get("bench"):
        j["logged"] = True
        try:
            log_job(j)
        except Exception:   # the log never stops a job from being saved
            pass
    write_json(job_path(j["id"]), j)


def all_jobs() -> List[Dict[str, Any]]:
    # first in, first out: jobs made in the same second keep their order (created is whole seconds)
    return sorted((read_json(p, {}) for p in jobs_dir().glob("*.json")), key=lambda j: (j.get("created", 0), j.get("created_ns", 0)))


def session_file(sid: str) -> Optional[Path]:
    hits = sorted((HOME / ".claude" / "projects").glob(f"*/{sid}.jsonl"), key=lambda p: -p.stat().st_mtime)
    return hits[0] if hits else None


GIT_SSH = " ".join(shlex.quote(x) for x in SSH_BASE)


def git(root: str, *args: str, env: Optional[Dict[str, str]] = None, input: Optional[bytes] = None,
        timeout: float = 600) -> subprocess.CompletedProcess:
    e = dict(os.environ, GIT_SSH_COMMAND=GIT_SSH, **(env or {}))
    return subprocess.run(["git", "-C", root, *args], input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=e, timeout=timeout)


def repo_snapshot(root: str, jid: str) -> str:
    """Commit the working tree as it is now (uncommitted and untracked files included) without touching the
    user's index, branch or files. Kept alive by refs/scaleout/base/<job>."""
    tmp = STATE_DIR / "tmp"; tmp.mkdir(parents=True, exist_ok=True)
    idx = tmp / f"{jid}.index"
    real = git(root, "rev-parse", "--git-path", "index").stdout.decode().strip()
    real_p = Path(real) if Path(real).is_absolute() else Path(root) / real
    if real_p.exists():
        shutil.copy(real_p, idx)   # reuse the stat cache: only changed files are hashed
    env = {"GIT_INDEX_FILE": str(idx), "GIT_AUTHOR_NAME": "agent-scaleout", "GIT_AUTHOR_EMAIL": "scaleout@localhost",
           "GIT_COMMITTER_NAME": "agent-scaleout", "GIT_COMMITTER_EMAIL": "scaleout@localhost"}
    try:
        head = git(root, "rev-parse", "HEAD").stdout.decode().strip()
        if git(root, "add", "-A", env=env, timeout=1800).returncode != 0:
            die(E_USAGE, f"git add failed in {root}")
        tree = git(root, "write-tree", env=env).stdout.decode().strip()
        commit = git(root, "commit-tree", tree, *(["-p", head] if head else []), "-m", f"scaleout snapshot {jid}", env=env).stdout.decode().strip()
    finally:
        idx.unlink(missing_ok=True)
    if not commit:
        die(E_USAGE, f"could not snapshot {root}")
    git(root, "update-ref", f"refs/scaleout/base/{jid}", commit)
    return commit


def node_git_url(alias: str, path: str) -> str:
    return path if alias == "local" else f"ssh://{alias}{path}"   # "local" = this machine (and the test harness)


def repo_name(root: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", Path(root).name) + "-" + hashlib.sha1(root.encode()).hexdigest()[:8]


def submit_job(cfg: Dict[str, Any], prompt: str, node: Optional[str] = None, agent: str = "default", workdir: str = "",
               sync: str = "", tags: Optional[List[str]] = None, timeout: Optional[int] = None, pull: bool = True,
               resume: str = "", force: bool = False, repo: str = "", apply: bool = False, mem_gb: float = 0, cpu: int = 0,
               model: str = "auto", check: bool = False, effort: str = "") -> str:
    model, why = pick_model(cfg, prompt, agent, resume, model, check)
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
    repo_fields: Dict[str, Any] = {}
    if repo:
        here = Path(repo).expanduser().resolve()
        top = run(["git", "-C", str(here), "rev-parse", "--show-toplevel"]).stdout.decode().strip()
        if not top:
            die(E_USAGE, f"--repo: not inside a git repository: {here}")
        sub = os.path.relpath(str(here), top)
        repo_fields = {"repo": top, "repo_sub": "" if sub == "." else sub, "repo_name": repo_name(top),
                       "repo_base": repo_snapshot(top, jid), "apply": apply}
    save_job({**repo_fields, "id": jid, "status": "queued", "created": now(), "created_ns": time.time_ns(), "node": None, "want_node": node, "agent": agent,
              "workdir": workdir, "sync": sync, "pull": pull, "tags": tags or [], "attempts": 0, "resume": resume, "mem_gb": mem_gb, "cpu": cpu,
              **({"model": model, "model_why": why, "effort": pick_effort(cfg, effort)} if model else {"effort": pick_effort(cfg, effort)} if effort else {}),
              "timeout": timeout or cfg["defaults"]["job_timeout_min"], "history": []})
    return jid


# A job that only runs something and reports (tests, builds, E2E): it says no code is changed, names such work at its start,
# and asks neither for something to be made nor for a judgement (play tests, reviews). Anything unclear stays on the node's model.
CHECK_NOEDIT = re.compile(r"(코드|소스|파일|제품|레포|저장소|흐름)[^.\n]{0,24}?(고치지|바꾸지|수정하지|건드리지) ?(않|말|마)|"
                          r"(보고|검사|확인|실행|테스트|시험|빌드|돌리기)만 ?(하|해|돌|한|합|$)|"
                          r"\b(do not|don't|never|without) (edit|chang|modif|fix|touch)\w*|no code changes|report only", re.I | re.M)
CHECK_WORK = re.compile(r"e2e|에뮬|시뮬|maestro|playwright|테스트|시험|빌드|커버리지|analyze|\blint|\bapk\b|\btests?\b|\bbuilds?\b|emulator|simulator", re.I)
CHECK_MAKE = re.compile(r"만들어|만드(는|세요)|구현|고쳐(?!도)|고치(세요|십시오|는 (일|작업)|고 )|고칩|추가(해|하|합)|바꿔|바꾸(세요|고 )|옮겨|넓혀|리팩터|다듬|붙여|개발|"
                        r"충돌|생성 파일|마무리|맞추기|고침|\bimplement|\brefactor|\bfix(es)?\b(?!/)|\badd\b|\bcreate\b", re.I)
CHECK_JUDGE = re.compile(r"플레이 ?테스트|페르소나|play ?test|게임성|재미|소감|솔직|첫인상|아무 설명도|평가|리뷰|검토|화면 점검|균형|조사|리서치|분석해|"
                         r"root cause|\breview|\bevaluat|\bresearch", re.I)
CHECK_RULES = """
(agent-scaleout) This job is a check. Before you report a failure, run that case once more and rule out the test itself and
the environment (device not ready, another branch's build, slow network, leftover data, another job on the same device).
Report a failure only when it happened twice, with what you did, what was expected, what happened, and the evidence (file name
of the screenshot or the log lines), and say whether it is the app, the test or the environment. A case you could not run is
blocked, never passed. End your answer with exactly one line, `fail` counting only failures of the app:
SCALEOUT-CHECK: pass=<n> fail=<n> blocked=<n>
"""
CHECK_LINE = re.compile(r"SCALEOUT-CHECK:\W*pass\s*=\s*(\d+)\W+fail\s*=\s*(\d+)\W+blocked\s*=\s*(\d+)", re.I)


CHECK_NO_PRODUCT = re.compile(r"(앱|제품|서버)[^.\n]{0,60}?(고치지|바꾸지|수정하지|건드리지) ?(않|말|마)|(app|product|server) code[^.\n]{0,40}(is not|not to be|do not)", re.I)


def is_check(prompt: str) -> bool:
    plain = re.sub(r"[^.\n]{0,40}?((고치|바꾸|수정하|건드리|만들|커밋하|추가하)지|do not|don't|never)[^.\n]*", " ", prompt[:900], flags=re.I)
    if CHECK_MAKE.search(plain[:250]) or CHECK_JUDGE.search(prompt[:600]):
        return False
    if re.search(r"e2e", prompt[:160], re.I):    # an E2E run may repair its own flows; the product stays as it is
        return bool(CHECK_NOEDIT.search(prompt[:12000]) or CHECK_NO_PRODUCT.search(prompt[:12000]))
    if not CHECK_WORK.search(prompt[:900]):
        return False
    return bool(CHECK_NOEDIT.search(prompt[:500]) or (CHECK_NOEDIT.search(prompt[:3000]) and not CHECK_MAKE.search(plain)))


def pick_model(cfg: Dict[str, Any], prompt: str, agent: str, resume: str, model: str, check: bool) -> Tuple[str, str]:
    """The model of a Claude Code job and why ("" = the node's own). No model call: the rule reads the task text."""
    if agent not in ("default", "claude") or resume:
        return "", ""
    if model and model != "auto":
        m = re.sub(r"[^A-Za-z0-9._-]", "", model)
        return ("", "") if m == "default" else (m, "check" if check else "asked")
    m = re.sub(r"[^A-Za-z0-9._-]", "", str(cfg["defaults"].get("check_model") or ""))
    return (m, "check") if m and (check or is_check(prompt)) else ("", "")


EFFORTS = ("low", "medium", "high", "xhigh", "max")


def pick_effort(cfg: Dict[str, Any], effort: str = "") -> str:
    e = effort or str(cfg["defaults"].get("check_effort") or "")
    if e and e not in EFFORTS:
        die(E_USAGE, f"effort '{e}': use {' '.join(EFFORTS)}")
    return e


DEVICE_TAGS = ("android-device", "ios-device")
# what one job needs when nothing better is known (agent + build + test target)
DEFAULT_JOB_GB = {"emulator": 7.5, "simulator": 4.5, "ios-simulator": 4.5, "android-device": 3.5, "ios-device": 3.5}
DEFAULT_GB = 3.0


HEAVY_TAGS = {"emulator", "simulator", "ios-simulator", "android-device", "ios-device"}
HEAVY_WORDS = re.compile(r"e2e|maestro|emulator|simulator|에뮬|시뮬|xcodebuild|flutter (build|test|drive)|gradlew|playwright|"
                         r"integration_test|전체 테스트|커버리지|coverage", re.I)


def job_text(j: Dict[str, Any]) -> str:
    """The start of the job's prompt (what kind of work it is)."""
    try:
        return (STATE_DIR / "prompts" / f"{j['id']}.md").read_text(errors="replace")[:4000]
    except (OSError, KeyError):
        return ""


def job_cpu(j: Dict[str, Any]) -> int:
    """CPU units a job takes on a node: 2 = heavy (builds, test suites, simulators, emulators), 1 = light.
    A node has as many units as `scaleout bench` measured CPU slots (M1: 2, M4: 3, M4 Pro: 5)."""
    if j.get("cpu"):
        return int(j["cpu"])
    if set(j.get("tags") or []) & HEAVY_TAGS:
        return 2
    return 2 if HEAVY_WORDS.search(job_text(j)) else 1


MAC_TAGS = {"mac", "macos", "darwin", "xcode", "simulator", "ios-simulator", "ios-device"}
ANDROID_TAGS = {"emulator", "android-device"}
IOS_WORDS = re.compile(r"\bios\b|xcode|testflight|cocoapods|pod install|\.ipa\b|iphone|ipad|시뮬|simulator", re.I)
ANDROID_WORDS = re.compile(r"android|안드로이드|에뮬|emulator|gradle|\.apk\b|\.aab\b|\badb\b", re.I)


def job_platform(j: Dict[str, Any]) -> str:
    """'ios' = needs a Mac, 'android' = Android work, '' = any machine. Tags decide; untagged jobs are read from the prompt
    (a prompt that names both, like a Flutter app tested on both, is '')."""
    tags = set(j.get("tags") or [])
    if tags & MAC_TAGS:
        return "ios"
    if tags & ANDROID_TAGS:
        return "android"
    text = job_text(j)
    ios, android = bool(IOS_WORDS.search(text)), bool(ANDROID_WORDS.search(text))
    return "ios" if ios and not android else "android" if android and not ios else ""


def job_disk_gb(j: Dict[str, Any]) -> float:
    """Disk a job fills on the node: the work tree with its build output, and on a Mac the Xcode build cache (4-8 GB)."""
    if j.get("disk_gb"):
        return float(j["disk_gb"])
    heavy, plat = job_cpu(j) >= 2, job_platform(j)
    if plat == "ios":
        return 10.0 if heavy else 3.0
    if plat == "android":
        return 6.0 if heavy else 2.0
    return 4.0 if heavy else 1.0


_PERF: Dict[str, Any] = {"t": 0, "v": {}}


def node_perf(name: str, st: Dict[str, Any]) -> float:
    """All-core speed of a machine, 1.0 = an M1 MacBook: its last `scaleout bench` (multi_x), else a guess from its threads."""
    if now() - _PERF["t"] > 60:
        v: Dict[str, float] = {}
        for h in bench_history():
            for k, b in (h.get("nodes") or {}).items():
                if isinstance(b, dict) and b.get("multi_x"):
                    v[k] = float(b["multi_x"])
        _PERF.update(t=now(), v=v)
    return _PERF["v"].get(name) or max(int(st.get("cpus", 1) or 1), 1) / 6.5


def job_kind(j: Dict[str, Any]) -> str:
    return ",".join(sorted(t for t in (j.get("tags") or []) if t in DEFAULT_JOB_GB)) or "default"


_LEARNED: Dict[str, Any] = {"t": 0, "v": {}, "linux": set()}


def learned_mem(node: str, kind: str) -> Optional[float]:
    """p90 of the memory recent finished jobs of this kind took on this node (needs 3 samples). Where a node measures a job's own
    processes only those samples count: the growth of the whole machine also holds what the jobs beside it took."""
    if now() - _LEARNED["t"] > 60:
        v: Dict[Tuple[str, str], List[float]] = {}
        since = now() - 14 * 86400
        xj = [x for x in all_jobs() if x.get("status") == "done" and x.get("mem_mb") and x.get("node") and x.get("created", 0) >= since and not x.get("bench")]
        own = {x["node"] for x in xj if x.get("mem_own")}
        linux = {k for k, nd in load_cfg()["nodes"].items() if nd.get("os") == "Linux"}
        own |= linux      # measured there from now on
        for x in xj:
            if x.get("mem_own") or x["node"] not in own:
                v.setdefault((x["node"], job_kind(x)), []).append(x["mem_mb"] / 1024)
        _LEARNED.update(t=now(), v=v, linux=linux)
    xs = sorted(_LEARNED["v"].get((node, kind), []))
    return xs[min(len(xs) - 1, int(round(0.9 * (len(xs) - 1))))] if len(xs) >= 3 else None


def job_mem_gb(j: Dict[str, Any], node: str) -> float:
    if j.get("mem_gb"):
        return float(j["mem_gb"])
    kind = job_kind(j)
    got = learned_mem(node, kind)
    base = max([DEFAULT_JOB_GB[t] for t in kind.split(",") if t in DEFAULT_JOB_GB] or [DEFAULT_GB])
    if node in _LEARNED["linux"]:   # only what a job's own processes took: jobs differ more than that, so leave room above and below
        return max(base, got) if got else base + 3.0
    return max(1.0, got) if got else base


def pick_node(cfg: Dict[str, Any], j: Dict[str, Any], metrics: Dict[str, Dict[str, Any]], load: Dict[str, int],
              dev_load: Optional[Dict[Tuple[str, str], int]] = None, planned_gb: Optional[Dict[str, float]] = None,
              cpu_used: Optional[Dict[str, int]] = None, why: Optional[Dict[str, str]] = None,
              planned_disk: Optional[Dict[str, float]] = None) -> Optional[str]:
    """Best node for a job, or None. `why` collects, per node, the reason it was passed over (shown while a job waits)."""
    why = why if why is not None else {}
    dev_load = dev_load or {}
    planned_gb = planned_gb if planned_gb is not None else {}
    cpu_used = cpu_used if cpu_used is not None else {}
    planned_disk = planned_disk if planned_disk is not None else {}
    plat, disk_need = job_platform(j), job_disk_gb(j)
    min_disk = float(cfg["defaults"].get("min_free_disk_gb", 5))
    perf = {k: node_perf(k, metrics.get(k, {})) for k in cfg["nodes"]}
    top_perf = max(perf.values() or [1.0]) or 1.0
    best, best_score = None, -1e9
    for name, n in cfg["nodes"].items():
        if n.get("pending") or (j.get("want_node") and name != j["want_node"]) or (j.get("only") and name not in j["only"]):
            continue
        st = metrics.get(name, {})
        h = node_health(cfg, name, st)
        if h != "ok":
            why[name] = h
            continue
        if not j.get("want_node") and (j.get("avoid") or {}).get(name, 0) > now():
            why[name] = "failed this job recently"
            continue
        devs = st.get("devices") or {}
        have = set(n.get("tags") or []) | {st.get("os", "").lower()} | OS_TAGS.get(st.get("os", "").lower(), set())
        have |= {f"{k}-device" for k, v in devs.items() if int(v or 0) > 0}
        have |= set(st.get("caps") or [])   # emulator / simulator / maestro, as the node found them
        if not set(j.get("tags") or []) <= have:
            why[name] = "lacks " + ",".join(sorted(set(j.get("tags") or []) - have))
            continue
        # one job per attached phone: a job tagged android-device / ios-device needs a free device of that kind
        if any(int(devs.get(t[:-7], 0) or 0) <= dev_load.get((name, t), 0) for t in (j.get("tags") or []) if t in DEVICE_TAGS):
            why[name] = "no free phone"
            continue
        agent = j.get("agent") or "default"
        if (agent == "bench" or j.get("repo")) and st.get("os", "").lower() == "windows":
            continue
        if agent not in ("default", "custom", "bench") and agent not in (st.get("agents") or []):
            why[name] = f"no {agent}"
            continue
        use = n.get("agent", "claude") if agent in ("default",) else agent
        blk = agent_blocked(name, use) if agent not in ("custom", "bench") else None
        if blk:
            why[name] = f"{use} {blk['why']} until {time.strftime('%m-%d %H:%M', time.localtime(blk['until']))}"
            continue
        if agent == "default" and not st.get("agents"):
            why[name] = "no agent"
            continue
        busy = load.get(name, 0)
        cap = job_cap(name, n, st)
        if busy >= cap:
            why[name] = f"{busy} job(s) running (max {cap})"
            continue
        if st.get("mem_total_gb"):
            free_gb = float(st["mem_total_gb"]) * float(st.get("mem_free_pct", 0)) / 100 - planned_gb.get(name, 0.0)
            if busy and free_gb - cfg["defaults"].get("mem_reserve_gb", 1.5) < job_mem_gb(j, name):
                why[name] = f"memory {free_gb:.1f} GB free, job needs {job_mem_gb(j, name):.1f}"
                continue   # an idle node always takes one job: its own memory guard still protects it
        limit = float(cfg["defaults"].get("max_load_per_cpu", 2)) * max(int(st.get("cpus", 1) or 1), 1)
        if float(st.get("load", 0) or 0) > limit:
            why[name] = f"load {float(st.get('load', 0)):.1f} > {limit:g}"
            continue       # CPU is the limit too (three E2E runs on 8 cores pushed load to 300); also when the load is not ours (CI)
        units = int(n.get("cpu_slots") or 2)
        cores = max(int(st.get("cpus", 1) or 1), 1)
        if busy and cpu_used.get(name, 0) + job_cpu(j) > units and float(st.get("load", 0) or 0) >= cores:
            why[name] = f"CPU units full ({cpu_used.get(name, 0)}/{units}, load {float(st.get('load', 0)):.1f})"
            continue       # units full and the cores really busy; a heavy job in a quiet phase (load < cores) leaves room
        disk_room = float(st.get("disk_free_gb", 0) or 0) - planned_disk.get(name, 0.0) - min_disk
        if busy and disk_room < disk_need:
            why[name] = f"disk {disk_room + min_disk:.0f} GB free, job needs {disk_need:.0f}"
            continue       # a work tree and its build caches would push the disk under its floor (an idle node still takes one)
        # even use by capacity, lowest strain first: the job goes to the machine that is least full once it has it.
        # Full = jobs / its share (max_jobs, so a node with share 8 takes about four jobs for each a share-2 laptop takes),
        # load / allowed load (2x cores), memory in use (the job's own counted in) and the part of the free disk the job fills.
        jobs_full = (busy + 1) / max(int(n.get("max_jobs", 2)), 1)
        load_full = float(st.get("load", 0) or 0) / limit
        total_gb = float(st.get("mem_total_gb") or 0)
        mem_used = 1 - float(st.get("mem_free_pct", 0) or 0) / 100
        if total_gb:
            mem_used = min(1.0, mem_used + (planned_gb.get(name, 0.0) + job_mem_gb(j, name)) / total_gb)
        disk_used = min(1.0, disk_need / max(disk_room, 1.0))
        score = -100 * (0.35 * jobs_full + 0.25 * load_full + 0.2 * mem_used + 0.2 * disk_used)
        score += 15 * perf[name] / top_perf      # a faster machine (all cores, from `scaleout bench`) finishes builds sooner
        # keep the Macs for what only a Mac can do: Android work goes to other machines first, any-machine work leans
        # away from Macs, and work that reads like iOS but is untagged leans to Macs
        mac = st.get("os", "").lower() == "darwin"
        if mac and plat != "ios":
            score -= 30 if plat == "android" else 15
        elif not mac and plat == "ios":
            score -= 40
        score += float(n.get("score_bias", 0))   # the orchestrator has a negative bias: it orchestrates, runs what only it can, and takes overflow
        if score > best_score:
            best, best_score = name, score
    return best


def skills_tar(cfg: Dict[str, Any]) -> Tuple[bytes, str]:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        paths = list(cfg["skills"].get("paths", SKILL_PATHS))
        for rel in paths + [x for x in SKILL_EXTRA if x not in paths]:
            p = HOME / rel
            if p.exists():
                tar.add(str(p), arcname=rel, filter=lambda ti: None if set(ti.name.split("/")[2:]) & SKILL_SKIP
                        or ti.name.split("/")[-1].startswith(".git") else ti)
    data = buf.getvalue()
    return data, hashlib.sha256(data).hexdigest()[:16]


def push_skills(cfg: Dict[str, Any], name: str, force: bool = False) -> bool:
    data, h = skills_tar(cfg)
    st = read_json(node_state_path(name), {})
    if not force and st.get("skills_hash") == h:
        return False
    n = Node(name, cfg)
    if n.ctl("recv", "~", input=data, timeout=300).returncode == 0 and n.ctl("skills-hash", h).returncode == 0:
        n.ctl("relocate", str(HOME), timeout=120)   # paths that name this machine's home → the node's home (best effort)
        st["skills_hash"] = h
        write_json(node_state_path(name), st)
        return True
    return False


def dispatch_error_line(text: str) -> str:
    """The one line of a failed git/ctl output worth showing next to a waiting job: the last error line, kept short."""
    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
    marked = [ln for ln in lines if any(k in ln for k in ("rejected", "error:", "fatal:", "failed"))]
    return ((marked or lines)[-1] if lines else "")[:160]


def dispatch(cfg: Dict[str, Any], j: Dict[str, Any], name: str) -> bool:
    n = Node(name, cfg)
    node = cfg["nodes"][name]
    if cfg["skills"].get("auto", True) and not node.get("local"):
        try:
            push_skills(cfg, name)
        except Exception:  # skills are best effort
            pass
    wd = j.get("workdir") or node.get("dir") or "~/scaleout-work"
    orphans = read_json(STATE_DIR / "orphans.json", [])
    if [name, j["id"]] in orphans:
        # the clean-up at the end of the tick would cancel and delete the copy handed over now (the job "vanished" twice
        # and failed); clear the old one first, before the new one exists
        n.ctl("cancel", j["id"], timeout=20)
        n.ctl("rm", j["id"], timeout=20)
        write_json(STATE_DIR / "orphans.json", [o for o in orphans if o != [name, j["id"]]])
    if j.get("repo"):
        # the orchestrator's own jobs get a copy on its internal disk too (a plain local path, no SSH): under launchd, macOS
        # makes every non-Apple program wait for a privacy prompt on an external disk, even through a work tree's .git link
        alias = n.route()
        bare = n.ctl("repo-init", j["repo_name"]).stdout.decode().strip().splitlines()[-1:]
        if not alias or not bare:
            j["dispatch_error"] = "no route to the node" if not alias else "the node could not prepare the repository"
            return False
        refspec = f"{j['repo_base']}:refs/scaleout/base/{j['id']}"
        push = git(j["repo"], "push", "-q", "--no-verify", node_git_url(alias, bare[0]), refspec, timeout=3600)
        if push.returncode != 0 and b"shallow update not allowed" in push.stderr:
            # a `git fetch --depth` somewhere left the requester's repository shallow: every push to a node is refused and the
            # job would wait forever with no reason shown. Fill in the missing history once and try again.
            git(j["repo"], "fetch", "-q", "--unshallow", timeout=3600)
            push = git(j["repo"], "push", "-q", "--no-verify", node_git_url(alias, bare[0]), refspec, timeout=3600)
        if push.returncode != 0:
            why = push.stderr.decode(errors="replace").strip()
            j["history"] = (j.get("history") or [])[-20:] + [{"t": now(), "event": "push-failed", "node": name, "why": why[-300:]}]
            j["dispatch_error"] = "could not send the repository: " + (dispatch_error_line(why) or "git push failed")
            return False
        co = n.ctl("repo-checkout", j["repo_name"], j["repo_base"], timeout=1800)
        wt = co.stdout.decode().strip().splitlines()[-1:] if co.returncode == 0 else []
        if not wt:
            j["dispatch_error"] = "the node could not check out the work tree: " + (dispatch_error_line(co.stderr.decode(errors="replace")) or "no reason given")
            return False
        j["repo_wt"] = wt[0]
        wd = wt[0] + ("/" + j["repo_sub"] if j.get("repo_sub") else "")
    if j.get("sync"):
        wd = ("~/.scaleout/work/" if not n.windows else "~\\.scaleout\\work\\") + j["id"]
        tar = run(["tar", "-cf", "-", "-C", j["sync"], "--exclude", "node_modules", "--exclude", ".gradle", "--exclude", "build", "--exclude", ".dart_tool", "."]).stdout
        if n.ctl("recv", wd, input=tar, timeout=900).returncode != 0:
            return False
    prompt = (STATE_DIR / "prompts" / f"{j['id']}.md").read_bytes()
    if j.get("repo"):
        prompt += ("\n\n---\n(agent-scaleout) Your working folder is a disposable copy of the requester's repository at their "
                   "current working tree. Leave your changes in the files: do not commit, push or switch branches; "
                   "they are sent back automatically.\n").encode()
    if j.get("agent") != "bench":
        prompt += ("\n\n---\n(agent-scaleout) You are already running as a scaleout job: run builds, tests, simulators and "
                   "emulators here yourself; do not hand the work to scaleout again.\n").encode()
    if j.get("agent") != "bench":
        prompt += ("\n\n---\n(agent-scaleout) Save every screenshot, screen recording, log or report you produce into the folder "
                   "given by the SCALEOUT_ARTIFACTS environment variable; it is sent back to the requester automatically. "
                   "List those file names in your final answer.\n").encode()
    if j.get("model") and j.get("model_why") == "check":
        prompt += ("\n\n---" + CHECK_RULES).encode()
    res = n.ctl("submit", j["id"], j.get("agent") or "default", str(j["timeout"]), wd, j.get("resume") or "", j.get("model") or "", j.get("effort") or "", input=prompt, timeout=60)
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
        try:   # Claude Code reports tokens, cost and turns in its JSON answer
            rep = json.loads(out.decode(errors="replace").strip().splitlines()[-1])
            j.update(report_usage(rep))
            if j.get("model") and rep.get("modelUsage") and not any(j["model"] in m for m in rep["modelUsage"]):
                j.pop("model"), j.pop("model_why", None), j.pop("effort", None)   # a runner from before an update took it: it ran on the node's own model
            if rep.get("session_id"):   # the context a fresh agent loads before any work: spent only because it ran apart
                jt = json.loads(n.ctl("job-tokens", str(rep["session_id"]), timeout=60).stdout.decode().strip().splitlines()[-1])
                j["start_tokens"], j["requests"] = int(jt.get("start") or 0), int(jt.get("requests") or 0)
        except (ValueError, IndexError, AttributeError, TypeError):
            pass
        if j.get("model_why") == "check":
            found = CHECK_LINE.findall(job_answer(out))
            if found:
                j["check"] = dict(zip(("pass", "fail", "blocked"), map(int, found[-1])))
        if s.get("mem_mb") is not None:
            j["mem_mb"] = int(s.get("mem_mb") or 0)
        if int(s.get("own_mb") or 0):
            j["mem_mb"], j["mem_own"] = int(s["own_mb"]), True
        if rc != 0:
            err = n.ctl("err", j["id"], "60").stdout
            (STATE_DIR / "results" / f"{j['id']}.err").write_bytes(err)
            said = out.decode(errors="replace")[-4000:] + "\n" + err.decode(errors="replace")[-4000:]
            trouble = agent_trouble(said)
            if j.get("model") and (trouble or re.search(r"selected model|model .{0,60}(not (exist|available|found)|no access)", said, re.I)):
                # a limit of that model, or no access to it: the same job on the node's own model, the node keeps taking jobs
                n.ctl("rm", j["id"])
                if j.get("repo_wt"):
                    n.ctl("repo-unlock", j["repo_wt"]); j.pop("repo_wt", None)
                j["history"].append({"t": now(), "event": "moved", "node": name, "why": f"{j['model']}: {trouble[0] if trouble else 'not available'}, back to the node's model"})
                j.pop("model"), j.pop("model_why", None), j.pop("effort", None)
                j.update({"status": "queued", "node": None})
                return
            if trouble and j.get("agent") not in ("custom", "bench"):
                use = cfg["nodes"][name].get("agent", "claude") if (j.get("agent") or "default") == "default" else j["agent"]
                block_agent(name, use, trouble[0], trouble[1])
                n.ctl("rm", j["id"])
                if j.get("repo_wt"):
                    n.ctl("repo-unlock", j["repo_wt"]); j.pop("repo_wt", None)
                j.setdefault("avoid", {})[name] = trouble[1]
                j["history"].append({"t": now(), "event": "moved", "node": name,
                                     "why": f"{use} on {name}: {trouble[0]} until {time.strftime('%m-%d %H:%M', time.localtime(trouble[1]))}"})
                j.update({"status": "queued", "node": None})
                return
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
        if j.get("repo") and j.get("repo_wt"):
            res = n.ctl("repo-result", j["repo_wt"], j["repo_base"], j["id"], timeout=900)
            head = (res.stdout.decode().strip().splitlines() or [""])[-1]
            alias = n.route()
            if head and head != j["repo_base"] and alias:
                bare = f"{j['repo_wt'].rsplit('-wt', 1)[0]}.git"
                br = f"scaleout/{j['id']}"
                f = git(j["repo"], "fetch", "-q", node_git_url(alias, bare), f"refs/scaleout/result/{j['id']}:refs/heads/{br}", timeout=1800)
                if f.returncode == 0:
                    j["repo_branch"] = br
                    j["repo_stat"] = git(j["repo"], "diff", "--stat", j["repo_base"], br).stdout.decode()[-3000:]
                    if j.get("apply"):
                        patch_ = git(j["repo"], "diff", "--binary", j["repo_base"], br).stdout
                        ap = git(j["repo"], "apply", "--whitespace=nowarn", input=patch_)
                        j["repo_applied"] = ap.returncode == 0
                        if ap.returncode != 0:
                            j["repo_apply_error"] = ap.stderr.decode()[-1500:]
                else:
                    j["repo_error"] = f.stderr.decode()[-500:]
            elif head == j["repo_base"]:
                j["repo_stat"] = "(no file changes)"
            n.ctl("repo-unlock", j["repo_wt"])
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


def job_answer(out: bytes) -> str:
    """The text of a job's answer (Claude Code puts it in JSON)."""
    raw = out.decode(errors="replace").strip()
    try:
        obj = json.loads(raw.splitlines()[-1])
        return str(obj.get("result") or "") if isinstance(obj, dict) else raw
    except (ValueError, IndexError):
        return raw


def requeue(cfg: Dict[str, Any], j: Dict[str, Any], why: str) -> None:
    if j.get("repo_wt") and j.get("node") in cfg["nodes"]:
        try:
            Node(j["node"], cfg).ctl("repo-unlock", j["repo_wt"], timeout=20)
        except Exception:
            pass
        j.pop("repo_wt", None)
    j["attempts"] = j.get("attempts", 0) + 1
    j["history"].append({"t": now(), "event": "retry", "node": j.get("node"), "why": why})
    if j.get("node") and why.startswith(("node ", "job vanished", "stalled")):
        j.setdefault("avoid", {})[j["node"]] = now() + 15 * 60   # it may still be sick: the next try goes to another machine
    if j.get("node"):
        orphans = read_json(STATE_DIR / "orphans.json", [])
        orphans.append([j["node"], j["id"]])
        write_json(STATE_DIR / "orphans.json", orphans)
    if j["attempts"] > cfg["defaults"]["retries"]:
        j.update({"status": "failed", "rc": -1, "error": why, "finished_at": now()})
    else:
        j.update({"status": "queued", "node": None})


HISTORY = STATE_DIR / "history.jsonl"


def sample_history(metrics: Dict[str, Dict[str, Any]], cfg: Optional[Dict[str, Any]] = None) -> None:
    """Every 5 minutes: one line with each machine's load, memory, running jobs, strain and bottleneck, and how many jobs
    wait and why (`scaleout insights` reads it; housekeeping keeps history_days of it)."""
    try:
        last = HISTORY.stat().st_mtime if HISTORY.exists() else 0
    except OSError:
        last = 0
    if now() - last < 300:
        return
    me = local_metrics()
    row = {"t": int(now()), "orch": {k: me.get(k) for k in ("load", "cpus", "mem_free_pct", "swap_gb")},
           "nodes": {n: {"load": st.get("load"), "cpus": st.get("cpus"), "mem_free_pct": st.get("mem_free_pct"),
                         "running": st.get("running"), "online": st.get("online")} for n, st in metrics.items()}}
    if cfg:
        jobs = all_jobs()
        running: Dict[str, int] = {}
        for j in jobs:
            if j.get("status") in ("dispatched", "running") and j.get("node"):
                running[j["node"]] = running.get(j["node"], 0) + 1
        for name, st in metrics.items():
            if st.get("online") and name in cfg["nodes"]:
                cap, gates, neck, strain = machine_gates(cfg, name, st, max(running.get(name, 0), int(st.get("running", 0) or 0)))
                row["nodes"][name].update({"cap": cap, "busy": running.get(name, 0), "strain": round(strain, 3), "neck": neck,
                                           "gates": {k: round(v, 3) for k, v in gates.items()}})
        waiting = [j for j in jobs if j.get("status") == "queued" and not j.get("bench")]
        reasons: Dict[str, int] = {}
        for j in waiting:
            for key in wait_reasons(j.get("wait") or ""):
                reasons[key] = reasons.get(key, 0) + 1
        row["waiting"], row["reasons"] = len(waiting), reasons
    with open(HISTORY, "a") as f:
        f.write(json.dumps(row) + "\n")


def maintain(cfg: Dict[str, Any], metrics: Dict[str, Dict[str, Any]]) -> None:
    """Resource recovery and self-healing on nodes."""
    d = cfg["defaults"]
    try:
        sample_history(metrics, cfg)
    except Exception:
        pass
    try:
        if now() - read_json(HOUSEKEEP, {}).get("t", 0) > 6 * 3600:
            housekeep(cfg)
    except Exception:
        pass
    try:   # token saving with --auto: once a day, move the auto-compact window toward what the measurements say
        t = tokens_cfg(cfg)
        if t["on"] and t["auto"] and now() - int(t.get("last_tune", 0) or 0) > 86400:
            tokens_autotune(cfg)
    except Exception:
        pass
    for name, st in metrics.items():
        if not st.get("online"):
            continue
        n = Node(name, cfg)
        if n.local:
            if (any((local_home() / "queue").iterdir()) if (local_home() / "queue").is_dir() else False):
                start_local_runner(n.n)   # a job queued just as the runner was leaving
            low = st.get("disk_free_gb", 99) < 2 * d["cleanup_target_disk_gb"]
            if now() - st.get("last_gc", 0) > (3600 if low else 6 * 3600):
                n.ctl("gc", "1" if low else str(d["keep_job_days"]))
                st["last_gc"] = now()
                write_json(node_state_path(name), st)
            # short of disk here too: the same cleanup the nodes get (idle work trees, Xcode build caches, package caches)
            if st.get("disk_free_gb", 99) < d["cleanup_below_disk_gb"] and now() - st.get("last_cleanup", 0) > 3600:
                res = n.ctl("cleanup", "", str(d["cleanup_target_disk_gb"]), timeout=900)
                st["last_cleanup"] = now()
                st["cleanup_note"] = res.stdout.decode().strip()[-120:]
                write_json(node_state_path(name), st)
            continue
        full = st.get("disk_free_gb", 99) < d["min_free_disk_gb"]   # disk-full: jobs wait, so try again sooner
        if st.get("disk_free_gb", 99) < d["cleanup_below_disk_gb"] and now() - st.get("last_cleanup", 0) > (600 if full else 3600):
            res = n.ctl("cleanup", "aggressive" if full else "", str(d["cleanup_target_disk_gb"]), timeout=900)
            st["last_cleanup"] = now()
            st["cleanup_note"] = res.stdout.decode().strip()[-120:]
            write_json(node_state_path(name), st)
        if st.get("runner") is False and now() - st.get("last_repair", 0) > 600:
            n.ctl("repair", timeout=120)
            st["last_repair"] = now()
            write_json(node_state_path(name), st)
        if d.get("reap_idle_min", 20) and now() - st.get("last_reap", 0) > 120:
            r = n.ctl("reap", str(d["reap_idle_min"]), timeout=120)
            st["last_reap"] = now()
            if r.stdout.startswith(b"stopped"):
                st["reap_note"] = r.stdout.decode().strip()
            write_json(node_state_path(name), st)
        if now() - st.get("last_gc", 0) > 6 * 3600:
            n.ctl("gc", str(d["keep_job_days"]))
            st["last_gc"] = now()
            write_json(node_state_path(name), st)
    if d.get("reap_idle_min", 20):
        try:
            local_reap(int(d["reap_idle_min"]))
        except Exception:
            pass
    if d.get("auto_tune", True):
        last = read_json(STATE_DIR / "tune.json", {}).get("t", 0)
        if now() - last > 24 * 3600:
            try:
                tune(cfg, apply=True, quiet=True)
            finally:
                write_json(STATE_DIR / "tune.json", {"t": now()})
    orphans = read_json(STATE_DIR / "orphans.json", [])
    left = []
    active = {(j.get("node"), j["id"]) for j in all_jobs() if j.get("status") in ("dispatched", "running")}
    for name, jid in orphans:
        st = metrics.get(name, {})
        if (name, jid) in active:   # handed back to that node since: the copy there is the live one
            continue
        if name in cfg["nodes"] and st.get("online"):
            Node(name, cfg).ctl("cancel", jid)
            Node(name, cfg).ctl("rm", jid)
        elif name in cfg["nodes"]:
            left.append([name, jid])
    write_json(STATE_DIR / "orphans.json", left)


class TickLock:
    def __init__(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.f = open(STATE_DIR / "tick2.lock", "w")
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
        planned_gb: Dict[str, float] = {}
        planned_disk: Dict[str, float] = {}
        cpu_used: Dict[str, int] = {}
        for j in jobs:
            if j.get("status") in ("dispatched", "running") and j.get("node"):
                cpu_used[j["node"]] = cpu_used.get(j["node"], 0) + job_cpu(j)
        for j in jobs:   # jobs handed out in the last 3 minutes may not have grown to their size yet
            if j.get("status") in ("dispatched", "running") and j.get("node") and now() - j.get("dispatched_at", 0) < 180:
                planned_gb[j["node"]] = planned_gb.get(j["node"], 0.0) + job_mem_gb(j, j["node"])
        for j in jobs:   # ... and their build output takes longer to land on disk
            if j.get("status") in ("dispatched", "running") and j.get("node") and now() - j.get("dispatched_at", 0) < 600:
                planned_disk[j["node"]] = planned_disk.get(j["node"], 0.0) + job_disk_gb(j)
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
            why: Dict[str, str] = {}
            name = pick_node(cfg, j, metrics, load, dev_load, planned_gb, cpu_used, why, planned_disk)
            if not name:
                wait = "; ".join(f"{k}: {v}" for k, v in sorted(why.items())) or "no nodes"
                if wait != j.get("wait", ""):
                    j["wait"] = wait
                    save_job(j)
                continue
            j.pop("dispatch_error", None)
            if not dispatch(cfg, j, name):
                # a node had room but the hand-over failed: say why, or the job sits in the queue with nothing shown
                wait = f"{name}: {j.pop('dispatch_error', '') or 'could not hand the job over'}"
                if wait != j.get("wait", ""):
                    j["wait"] = wait
                save_job(j)
                continue
            j.pop("wait", None)
            load[name] = load.get(name, 0) + 1
            cpu_used[name] = cpu_used.get(name, 0) + job_cpu(j)
            planned_gb[name] = planned_gb.get(name, 0.0) + job_mem_gb(j, name)
            planned_disk[name] = planned_disk.get(name, 0.0) + job_disk_gb(j)
            for t in j.get("tags") or []:
                if t in DEVICE_TAGS:
                    dev_load[(name, t)] = dev_load.get((name, t), 0) + 1
            save_job(j)
            if verbose:
                say(f"dispatched {j['id']} -> {name}")
        maintain(cfg, metrics)


DAEMON_ALIVE = STATE_DIR / "daemon.alive"


def daemon_alive() -> bool:
    try:
        return now() - DAEMON_ALIVE.stat().st_mtime < 60
    except OSError:
        return False


def wait_job(jid: str, max_s: int) -> Tuple[int, str]:
    """Wait for a job. When the daemon runs it alone schedules: a long-lived waiting process must never dispatch with
    the code it started with (after an upgrade that would bypass new limits), so it only collects then."""
    start = time.time()
    told = ""
    while True:
        if not daemon_alive():
            tick()
        j = load_job(jid)
        if j["status"] == "queued" and j.get("wait") and j["wait"] != told:
            told = j["wait"]
            sys.stderr.write(f"scaleout: {jid} waits for room — {told}\n")
            sys.stderr.flush()
        if j["status"] in ("done", "failed", "cancelled"):
            return job_output(j)
        if max_s and time.time() - start >= max_s:
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
                     workdir=a.dir or "", sync=a.sync or "", tags=a.tags.split(",") if a.tags else [], timeout=a.timeout, pull=not a.no_pull, resume=a.resume, force=a.force, repo=a.repo, apply=a.apply, mem_gb=a.mem, cpu=a.cpu, model=a.model, check=a.check, effort=a.effort or "")
    tick()
    print(jid)


def cmd_run(a) -> None:
    cfg = load_cfg()
    jid = submit_job(cfg, read_prompt(a), node=None if a.node in (None, "auto") else a.node, agent=a.agent,
                     workdir=a.dir or "", sync=a.sync or "", tags=a.tags.split(",") if a.tags else [], timeout=a.timeout, pull=not a.no_pull, resume=a.resume, force=a.force, repo=a.repo, apply=a.apply, mem_gb=a.mem, cpu=a.cpu, model=a.model, check=a.check, effort=a.effort or "")
    print(jid, file=sys.stderr)
    rc, text = wait_job(jid, a.max)
    print(text + repo_text(load_job(jid)) + artifacts_text(load_job(jid)) + check_text(load_job(jid)))
    sys.exit(rc)


def cmd_wait(a) -> None:
    rc, text = wait_job(a.job, a.max)
    print(text + repo_text(load_job(a.job)) + artifacts_text(load_job(a.job)) + check_text(load_job(a.job)))
    sys.exit(rc)


def repo_text(j: Dict[str, Any]) -> str:
    if not j.get("repo"):
        return ""
    if j.get("repo_error"):
        return f"\n\n[repo] could not fetch the result: {j['repo_error']}"
    if not j.get("repo_branch"):
        return f"\n\n[repo] {j.get('repo_stat') or 'no result'}"
    t = f"\n\n[repo] result branch {j['repo_branch']} in {j['repo']}\n{j.get('repo_stat', '').rstrip()}"
    if j.get("apply"):
        t += ("\n  applied to the working tree" if j.get("repo_applied") else
              f"\n  NOT applied (conflicts with local changes) — merge {j['repo_branch']} by hand:\n{j.get('repo_apply_error', '')}")
    return t


def artifacts_text(j: Dict[str, Any]) -> str:
    if j.get("artifacts"):
        return f"\n\n[artifacts] {j['artifacts_dir']}\n" + "\n".join(f"  {x}" for x in j["artifacts"][:50])
    return f"\n\n[artifacts] {j['artifacts_note']}" if j.get("artifacts_note") else ""


def check_text(j: Dict[str, Any]) -> str:
    """Under the answer of a check that a cheaper model ran: what to do with its failures, so that their truth gets counted."""
    if j.get("model_why") != "check" or j.get("status") not in ("done", "failed"):
        return ""
    c = j.get("check")
    t = f"\n\n[check] run by {check_name(j)}: " + (f"pass {c['pass']} · fail {c['fail']} · blocked {c['blocked']}" if c else "it left no SCALEOUT-CHECK line")
    if j.get("verdict"):
        return t + "  (recorded: " + " ".join(f"{k}={v}" for k, v in j["verdict"].items()) + ")"
    if not c or c["fail"]:
        t += (f"\n  Look at the evidence of each failure before changing code, then record what it was (costs nothing):"
              f"\n  scaleout check {j['id']} real=<n> false=<n>     real = a fault there, false = reported but nothing was wrong")
    if not c or not c["fail"]:
        t += f"\n  If a fault it passed turns up later: scaleout check {j['id']} missed=<n>"
    return t


def check_name(j: Dict[str, Any]) -> str:
    return str(j.get("model")) + (f" ({j['effort']})" if j.get("effort") else "")


def check_rows(days: int) -> Dict[str, Dict[str, float]]:
    rows: Dict[str, Dict[str, float]] = {}
    for j in all_jobs():
        if j.get("model_why") != "check" or j.get("status") not in ("done", "failed") or j.get("finished_at", 0) < now() - days * 86400:
            continue
        r = rows.setdefault(check_name(j), dict.fromkeys(("jobs", "cost", "failed", "noline", "pass", "fail", "blocked", "open", "real", "false", "missed"), 0))
        c, v = j.get("check") or {}, j.get("verdict") or {}
        r["jobs"] += 1
        r["cost"] += float(j.get("cost_usd") or 0)
        r["failed"] += j["status"] == "failed"
        r["noline"] += j["status"] == "done" and not c
        for k in ("pass", "fail", "blocked"):
            r[k] += c.get(k, 0)
        for k in ("real", "false", "missed"):
            r[k] += v.get(k, 0)
        r["open"] += max(0, c.get("fail", 0) - v.get("real", 0) - v.get("false", 0))
    return rows


def cmd_check(a) -> None:
    """`scaleout check`: how true the failures of the check model's jobs were. `scaleout check JOB real=1 false=0`: record it."""
    if a.job:
        j = load_job(a.job)
        got = {}
        for x in a.counts:
            m = re.fullmatch(r"(real|false|missed)=(\d+)", x)
            if not m:
                die(E_USAGE, f"'{x}': use real=<n> false=<n> missed=<n>")
            got[m.group(1)] = int(m.group(2))
        if got:
            j["verdict"] = {**(j.get("verdict") or {}), **got}
            save_job(j)
        say((check_text(j).strip() or "this job was not run as a check"))
        return
    cfg = load_cfg()
    m = cfg["defaults"].get("check_model")
    say(f"check model: {m + ' · effort ' + (cfg['defaults'].get('check_effort') or 'as set on each machine') if m else 'off (scaleout config check_model claude-sonnet-5-5)'} · last {a.days} days")
    rows = check_rows(a.days)
    if not rows:
        say("no checks run on it yet")
    for name, r in rows.items():
        seen = r["real"] + r["false"]
        say(f"{name}: {r['jobs']} jobs · ${r['cost']:.2f} · {r['failed']} did not finish · {r['noline']} without a result line")
        say(f"  cases: pass {r['pass']} · fail {r['fail']} · blocked {r['blocked']}")
        say(f"  failures: real {r['real']} · false {r['false']}" + (f" ({100 * r['false'] / seen:.0f}% false)" if seen else "")
            + f" · not looked at {r['open']} · faults it missed {r['missed']}")


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
    print(text + repo_text(j) + artifacts_text(j) + check_text(j))
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


DIRECT_DIR = STATE_DIR / "direct"   # heavy commands sessions ran on this machine (agents/claude/direct-run.py)


def direct_runs(keep_days: int = 7) -> List[Dict[str, Any]]:
    """Heavy builds and tests a Claude Code session ran here itself, as jobs: status running / done / failed / cancelled /
    lost (no end was recorded). A command sent to the background ends when its output file says `[exited with code N]`."""
    out: List[Dict[str, Any]] = []
    for p in sorted(DIRECT_DIR.glob("*.json")) if DIRECT_DIR.exists() else []:
        r = read_json(p, None)
        if not isinstance(r, dict) or "started" not in r:
            continue
        st, started = r.get("status", "running"), float(r["started"])
        if st != "running" and float(r.get("finished") or started) < now() - keep_days * 86400:
            p.unlink(missing_ok=True)
            continue
        if st == "running" and r.get("background"):
            f = None
            if r.get("task") and r.get("session"):
                for root in {"/tmp", "/private/tmp", os.environ.get("TMPDIR", "/tmp")}:
                    hit = list(Path(root).glob(f"claude-*/*/{r['session']}/tasks/{r['task']}.output"))
                    if hit:
                        f = hit[0]
                        break
            tail = ""
            if f:
                try:
                    with open(f, "rb") as fh:
                        fh.seek(max(0, f.stat().st_size - 200))
                        tail = fh.read().decode(errors="replace")
                except OSError:
                    pass
            m = re.search(r"\[exited with code (\d+)\]\s*$", tail)
            if m:
                st = "done" if m.group(1) == "0" else "failed"
                r.update(status=st, finished=f.stat().st_mtime)
                write_json(p, r)
            elif (f is None and now() - started > 3600) or now() - started > 86400:
                st = "lost"
        elif st == "running" and now() - started > 15 * 60:   # a foreground command stops at 10 min: its end was not seen
            st = "lost"
        out.append({"id": "s-" + str(r.get("id", p.stem))[-10:], "node": LOCAL, "status": st, "direct": True,
                    "created": started, "dispatched_at": started, "finished_at": r.get("finished") if st != "running" else None,
                    "what": r.get("what") or r.get("cmd", ""), "cwd": r.get("cwd", ""), "session": r.get("session", "")})
    return out


def cmd_jobs(a) -> None:
    cfg = load_cfg()
    jobs = sorted(all_jobs() + direct_runs(), key=lambda j: j.get("created", 0))[-a.limit:]
    if not jobs:
        say("no jobs")
    nw = name_w(cfg, 16)
    for j in jobs:
        age = time.strftime("%m-%d %H:%M", time.localtime(j.get("created", 0)))
        where = pad(label_of(cfg, j["node"]) if j.get("node") else "-", nw)
        if j.get("direct"):   # run in a session here, not through scaleout
            say(f"{j['id']:<20} {j['status']:<10} {where}session   {age}  {j.get('what', '')}  ({Path(j.get('cwd') or '.').name})")
            continue
        say(f"{j['id']:<20} {j['status']:<10} {where}tries={j.get('attempts', 0)}  {age}"
            + (f"  waits: {j['wait']}" if j.get("status") == "queued" and j.get("wait") else ""))


HOUSEKEEP = STATE_DIR / "housekeep.json"


def read_jsonl(p: Path, since: float = 0) -> List[Dict[str, Any]]:
    out = []
    try:
        with open(p) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("t", 0) >= since:
                    out.append(r)
    except OSError:
        pass
    return out


def trim_jsonl(p: Path, keep_days: float) -> int:
    """Drop lines older than keep_days; bytes freed."""
    if not p.exists():
        return 0
    before = p.stat().st_size
    keep = [json.dumps(r, ensure_ascii=False) for r in read_jsonl(p, now() - keep_days * 86400)]
    tmp = p.with_suffix(".tmp")
    tmp.write_text("\n".join(keep) + ("\n" if keep else ""))
    tmp.replace(p)
    return max(0, before - p.stat().st_size)


def tree_bytes(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.exists() else 0


def gc_jobs(cfg: Dict[str, Any]) -> Tuple[int, int]:
    """Forget finished jobs older than keep_job_days, with their prompt, answer and saved artifacts, and any result or
    prompt left without a job. The job log keeps one line per job for log_days. (jobs, bytes freed)"""
    cut = now() - cfg["defaults"]["keep_job_days"] * 86400
    n = freed = 0
    known = set()
    for j in all_jobs():
        known.add(j.get("id"))
        if j.get("status") in ("done", "failed", "cancelled") and not j.get("logged") and not j.get("bench"):
            save_job(j)   # finished before the job log existed: log it now
        if j.get("status") in ("done", "failed", "cancelled") and j.get("finished_at", now()) < cut:
            for p in (job_path(j["id"]), STATE_DIR / "prompts" / f"{j['id']}.md", STATE_DIR / "results" / f"{j['id']}.out",
                      STATE_DIR / "results" / f"{j['id']}.err", STATE_DIR / "results" / j["id"]):
                if p.exists():
                    freed += tree_bytes(p)
                    shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
            known.discard(j.get("id"))
            n += 1
    for folder in (STATE_DIR / "results", STATE_DIR / "prompts"):
        for p in (folder.iterdir() if folder.is_dir() else []):
            jid = p.name.split(".")[0]
            if jid.startswith("j") and jid not in known and p.stat().st_mtime < cut:
                freed += tree_bytes(p)
                shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
    return n, freed


def housekeep(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Every 6 hours (the daemon): old jobs and their results go, logs are trimmed to their days, the daemon log is capped,
    temporary files go. Nodes clean their own side (ctl gc). Nothing outside scaleout's state folder is touched."""
    d = cfg["defaults"]
    jobs, freed = gc_jobs(cfg)
    freed += trim_jsonl(HISTORY, d.get("history_days", 30))
    freed += trim_jsonl(JOB_LOG, d.get("log_days", 90))
    for log in STATE_DIR.glob("*.log"):
        if log.stat().st_size > 5 * 1024 * 1024:
            data = log.read_bytes()[-2 * 1024 * 1024:]
            freed += log.stat().st_size - len(data)
            log.write_bytes(data)
    tmp = STATE_DIR / "tmp"
    for p in (tmp.iterdir() if tmp.is_dir() else []):
        if p.stat().st_mtime < now() - 86400:
            freed += tree_bytes(p)
            shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
    rec = {"t": int(now()), "jobs": jobs, "freed_mb": round(freed / 1048576, 1),
           "total_freed_mb": round(read_json(HOUSEKEEP, {}).get("total_freed_mb", 0) + freed / 1048576, 1)}
    write_json(HOUSEKEEP, rec)
    return rec


def cmd_gc(a) -> None:
    rec = housekeep(load_cfg())
    say(f"removed {rec['jobs']} old jobs, freed {rec['freed_mb']} MB (logs trimmed, results and artifacts of old jobs gone)")


# ── commands: nodes ──────────────────────────────────────────────────────────
def cmd_nodes(a) -> None:
    cfg = load_cfg()
    if not cfg["nodes"]:
        say("no nodes — scaleout add NAME user@host  |  scaleout invite NAME")
    for name, n in cfg["nodes"].items():
        say(f"{pad(label_of(cfg, name), name_w(cfg, 16))} {role_of(cfg, name):<13}{'id=' + name:<18}{n.get('os', '?'):<8} user={n.get('user', '?')} lan={n.get('lan') or '-'} tunnel={n.get('port') or '-'} "
            f"agent={n.get('agent', 'claude')} share={n.get('max_jobs', 2)} cap={job_cap(name, n)} tags={','.join(n.get('tags') or []) or '-'}{' (invite pending)' if n.get('pending') else ''}")


def fmt_devices(st: Dict[str, Any]) -> str:
    d = st.get("devices") or {}
    parts = [f"{k[0].upper()}{v}" for k, v in (("android", d.get("android", 0)), ("ios", d.get("ios", 0))) if int(v or 0)]
    return " ".join(parts) or "-"


# ── paint: colour and width for the terminal views ───────────────────────────
PAINT = False   # set by paint_auto(): colour only on a terminal
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
C_ACCENT, C_VIOLET, C_OK, C_WARN, C_BAD, C_DIM, C_FRAME, C_TEXT = 81, 141, 114, 221, 203, 245, 240, 252
GRASS = [237, 22, 28, 34, 46]          # no tokens → the busiest days
GRASS_PLAIN = ["·", "░", "▒", "▓", "█"]


def paint_auto() -> None:
    global PAINT
    PAINT = sys.stdout.isatty() and not os.environ.get("NO_COLOR") and os.environ.get("TERM", "") != "dumb"


def c(text: Any, fg: Optional[int] = None, bold: bool = False, dim: bool = False) -> str:
    text = str(text)
    if not PAINT or (fg is None and not bold and not dim):
        return text
    codes = (["1"] if bold else []) + (["2"] if dim else []) + ([f"38;5;{fg}"] if fg is not None else [])
    return f"\x1b[{';'.join(codes)}m{text}\x1b[0m"


def cw(ch: str) -> int:
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 0 if unicodedata.combining(ch) else 1


def vlen(text: str) -> int:
    """Columns a string takes on screen: colour codes take none, Korean and other wide letters take two."""
    return sum(cw(ch) for ch in ANSI_RE.sub("", text))


def clip(text: str, width: int) -> str:
    """Cut to `width` columns, keeping colour codes whole."""
    if vlen(text) <= width:
        return text
    out, used, i = [], 0, 0
    while i < len(text):
        m = ANSI_RE.match(text, i)
        if m:
            out.append(m.group()); i = m.end(); continue
        w = cw(text[i])
        if used + w > width - 1:
            break
        out.append(text[i]); used += w; i += 1
    return "".join(out) + "…" + ("\x1b[0m" if PAINT else "")


def pad(text: Any, width: int, right: bool = False) -> str:
    text = clip(str(text), width)
    gap = " " * max(0, width - vlen(text))
    return gap + text if right else text + gap


def level_color(pct_good: float) -> int:
    return C_OK if pct_good >= 40 else C_WARN if pct_good >= 20 else C_BAD


def meter(pct: float, width: int = 10, color: Optional[int] = None) -> str:
    pct = max(0.0, min(100.0, float(pct or 0)))
    full = int(round(pct / 100 * width))
    return c("█" * full, color if color is not None else C_ACCENT) + c("░" * (width - full), 238)


def box(title: str, lines: List[str], width: int, hint: str = "") -> List[str]:
    """A rounded panel: ╭─ TITLE ───── hint ╮"""
    inner = width - 4
    hint_s = f" {hint} " if hint else ""
    dashes = max(1, width - 5 - vlen(title) - vlen(hint_s))
    out = [c("╭─ ", C_FRAME) + c(title, C_ACCENT, bold=True) + " " + c("─" * dashes, C_FRAME) + c(hint_s, C_DIM) + c("╮", C_FRAME)]
    for line in lines:
        out.append(c("│", C_FRAME) + " " + pad(line, inner) + " " + c("│", C_FRAME))
    out.append(c("╰" + "─" * (width - 2) + "╯", C_FRAME))
    return out


# ── machine names and roles ──────────────────────────────────────────────────
def label_of(cfg: Dict[str, Any], name: str) -> str:
    """The name a person gave this machine (`scaleout rename`), or its id. Orchestrator and node are roles, not names."""
    n = cfg.get("nodes", {}).get(name) or {}
    if n.get("label"):
        return str(n["label"])
    if name == LOCAL and (cfg.get("orchestrator") or {}).get("label"):
        return str(cfg["orchestrator"]["label"])
    return name


def name_w(cfg: Dict[str, Any], least: int = 22) -> int:
    """Width of the name column: the longest machine name and a clear gap."""
    return max([least] + [vlen(label_of(cfg, n)) + 4 for n in list(cfg.get("nodes", {})) + [LOCAL]])


def role_of(cfg: Dict[str, Any], name: str) -> str:
    return "orchestrator" if name == LOCAL or (cfg.get("nodes", {}).get(name) or {}).get("local") else "node"


def node_id(cfg: Dict[str, Any], given: str) -> str:
    """The id behind a name typed on the command line: an id, or a name set with `scaleout rename`."""
    if given in cfg.get("nodes", {}) or given == LOCAL:
        return given
    for n in list(cfg.get("nodes", {})) + [LOCAL]:
        if label_of(cfg, n) == given:
            return n
    return given


def pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def cmd_insights(a) -> None:
    """What the logs say about the last N days: how strained each machine was and by what, why jobs waited and for how
    long, how much running in parallel saved, failures by cause, and what to change."""
    cfg = load_cfg()
    days = max(1, a.days)
    since = now() - days * 86400
    rows, jobs = read_jsonl(HISTORY, since), read_jsonl(JOB_LOG, since)
    if a.json:
        print(json.dumps({"samples": len(rows), "jobs": jobs[-500:]}, ensure_ascii=False)); return
    nw = name_w(cfg, 16)
    say(c(f"scaleout insights · last {days} day(s) · {len(rows)} samples (every 5 min) · {len(jobs)} finished jobs", C_TEXT, bold=True))
    if not rows and not jobs:
        say("no log yet: the daemon writes a sample every 5 minutes and a line per finished job"); return
    tips: List[str] = []
    waiting_rows = [r for r in rows if r.get("waiting")]
    say("")
    say(c(f"{'MACHINE':<{nw}}{'ONLINE':>8}{'AVG':>7}{'BUSY':>7}  {'BOTTLENECK (when busy)':<28}{'IDLE WHILE JOBS WAITED':>24}{'JOBS':>6}{'RUN':>8}{'FAIL':>6}", C_DIM))
    for r in rows:   # samples from before 0.9.3 have load and memory but no strain: work it out the same way
        for name, x in (r.get("nodes") or {}).items():
            if x.get("online") and "strain" not in x and name in cfg["nodes"] and x.get("cpus"):
                x["cap"], x["gates"], x["neck"], x["strain"] = machine_gates(cfg, name, x, int(x.get("running") or 0))
    for name in sorted(cfg["nodes"], key=lambda x: (not cfg["nodes"][x].get("local"), x)):
        samples = [(r.get("nodes") or {}).get(name) or {} for r in rows]
        on = [x for x in samples if x.get("online")]
        st = [x for x in on if "strain" in x]
        avg = sum(x["strain"] for x in st) / len(st) if st else 0
        busy = [x for x in st if x["strain"] >= 0.9]
        necks: Dict[str, int] = {}
        for x in busy:
            necks[x["neck"]] = necks.get(x["neck"], 0) + 1
        neck_txt = ", ".join(f"{k} {pct(v / len(busy))}" for k, v in sorted(necks.items(), key=lambda kv: -kv[1])) if busy else "-"
        idle_w = [((r.get("nodes") or {}).get(name) or {}) for r in waiting_rows]
        idle_w = [x for x in idle_w if x.get("online") and "strain" in x]
        idle = sum(1 for x in idle_w if x["strain"] < 0.5) / len(idle_w) if idle_w else 0
        mine = [j for j in jobs if j.get("node") == name]
        runs = sorted(j["run_s"] for j in mine if j.get("run_s"))
        fails = sum(1 for j in mine if j.get("status") == "failed")
        say(pad(c(label_of(cfg, name), C_TEXT, bold=True), nw) + f"{pct(len(on) / len(samples)) if samples else '-':>8}"
            + f"{pct(avg) if st else '-':>7}{pct(len(busy) / len(st)) if st else '-':>7}  {neck_txt:<28}"
            + f"{pct(idle) if idle_w else '-':>24}{len(mine):>6}{fmt_dur(runs[len(runs) // 2]) if runs else '-':>8}{fails:>6}")
        if busy and len(busy) / max(len(st), 1) >= 0.2:
            top = max(necks, key=necks.get)
            if top == "memory":
                tips.append(f"{label_of(cfg, name)} is often full on memory ({pct(len(busy) / len(st))} of the time): memory decides how many "
                            "jobs run there. More RAM there, or fewer emulator jobs at once, would let more run.")
            elif top == "cpu":
                tips.append(f"{label_of(cfg, name)} is often CPU-bound ({pct(len(busy) / len(st))} of the time): lower its share "
                            f"(scaleout config --node {name} --max-jobs N) so heavy builds spread to other machines.")
            else:
                tips.append(f"{label_of(cfg, name)} often has every job slot taken while load and memory have room: raise its share.")
        if idle_w and idle >= 0.5 and len(idle_w) >= 6:
            tips.append(f"{label_of(cfg, name)} sat idle {pct(idle)} of the time jobs were waiting: those jobs could not go there "
                        "(see the wait reasons below; a `mac` tag on non-iOS work is the usual cause).")
    say("")
    waits = sorted(j["wait_s"] for j in jobs if j.get("wait_s") is not None)
    reasons: Dict[str, int] = {}
    for r in waiting_rows:
        for k, v in (r.get("reasons") or {}).items():
            reasons[k] = reasons.get(k, 0) + v
    tot = sum(reasons.values())
    say(c("QUEUE", C_ACCENT, bold=True) + f"  jobs waited before starting: median {fmt_dur(waits[len(waits) // 2]) if waits else '-'}"
        f" · 90% within {fmt_dur(waits[int(len(waits) * 0.9)]) if waits else '-'} · longest {fmt_dur(waits[-1]) if waits else '-'}"
        f" · something waited in {pct(len(waiting_rows) / len(rows)) if rows else '-'} of samples")
    if tot:
        say("  waits by reason: " + ", ".join(f"{k} {pct(v / tot)}" for k, v in sorted(reasons.items(), key=lambda kv: -kv[1])))
    if waits and waits[int(len(waits) * 0.9)] > 600:
        top = max(reasons, key=reasons.get) if reasons else "no room"
        tips.append(f"1 job in 10 waited over {fmt_dur(waits[int(len(waits) * 0.9)])} before starting, mostly on {top}.")
    if reasons.get("needs a Mac", 0) > tot * 0.3 and tot:
        tips.append("Many waits were jobs tagged mac/xcode waiting for a Mac: tag only work that needs one (iOS build, simulator, signing).")
    byday: Dict[str, List[Tuple[float, float]]] = {}
    for j in jobs:
        if j.get("run_s"):
            byday.setdefault(time.strftime("%m-%d", time.localtime(j["t"])), []).append((j["t"] - j["run_s"], j["t"]))
    if byday:
        par = []
        for day, iv in sorted(byday.items()):
            wall = merge_intervals(iv)
            par.append(f"{day} {sum(b - x for x, b in iv) / wall:.1f}x" if wall else f"{day} -")
        say("  running in parallel (job time / wall time): " + " · ".join(par[-7:]))
    say("")
    kinds: Dict[str, List[Tuple[float, float]]] = {}
    for j in jobs:
        if j.get("mem_mb") and j.get("mem_gb_est"):
            kinds.setdefault(j.get("kind") or "-", []).append((j["mem_gb_est"], j["mem_mb"] / 1024))
    if kinds:
        say(c("MEMORY", C_ACCENT, bold=True) + "  reserved vs used per job: " + " · ".join(
            f"{k} {sum(e for e, _ in v) / len(v):.1f} GB / {sum(u for _, u in v) / len(v):.1f} GB ({len(v)})" for k, v in sorted(kinds.items())))
    causes: Dict[str, int] = {}
    per_node: Dict[Tuple[str, str], int] = {}
    for j in jobs:
        for ev, why, node in j.get("events") or []:
            if ev == "moved":
                key = "agent limit" if "limit" in why else "signed out" if "signed out" in why else "moved"
                causes[key] = causes.get(key, 0) + 1
                per_node[(key, node or "?")] = per_node.get((key, node or "?"), 0) + 1
            elif ev in ("retry", "push-failed"):
                causes[ev] = causes.get(ev, 0) + 1
                per_node[(ev, node or "?")] = per_node.get((ev, node or "?"), 0) + 1
        if j.get("status") == "failed":
            causes["failed"] = causes.get("failed", 0) + 1
        elif j.get("status") == "cancelled":
            causes["cancelled"] = causes.get("cancelled", 0) + 1
    if causes:
        say(c("TROUBLE", C_ACCENT, bold=True) + "  " + " · ".join(f"{k} {v}" for k, v in sorted(causes.items(), key=lambda kv: -kv[1])))
    for (key, node), v in sorted(per_node.items(), key=lambda kv: -kv[1]):
        if key == "agent limit":
            tips.append(f"The agent on {label_of(cfg, node)} hit its usage limit {v} time(s): give that machine another account (ccp) or a smaller share.")
        elif key == "signed out":
            tips.append(f"The agent on {label_of(cfg, node)} was signed out {v} time(s): sign it in there, then scaleout unblock {node}.")
        elif key == "retry" and v >= 3:
            tips.append(f"Jobs on {label_of(cfg, node)} stalled or were lost {v} times: check its network and runner (scaleout doctor).")
    hk = read_json(HOUSEKEEP, {})
    size = sum(tree_bytes(STATE_DIR / x) for x in ("jobs", "results", "prompts", "log")) + tree_bytes(HISTORY)
    say("")
    say(c("DISK", C_ACCENT, bold=True) + f"  scaleout's records here: {size / 1048576:.0f} MB · cleaned every 6 h "
        f"(jobs {cfg['defaults']['keep_job_days']} d, job log {cfg['defaults'].get('log_days', 90)} d, samples {cfg['defaults'].get('history_days', 30)} d)"
        + (f" · last {time.strftime('%m-%d %H:%M', time.localtime(hk['t']))}, freed {hk.get('freed_mb', 0)} MB (total {hk.get('total_freed_mb', 0)} MB)" if hk.get("t") else ""))
    say("")
    say(c("SUGGESTIONS", C_ACCENT, bold=True))
    for t in tips or ["No clear bottleneck: jobs spread and started without long waits."]:
        say(f"  • {t}")


def cmd_unblock(a) -> None:
    """Let a node's agent take jobs again (after signing it in, or when its limit reset earlier than read)."""
    data = read_json(AGENT_BLOCKS, {})
    name = node_id(load_cfg(), a.name)
    gone = data.get(name, {}) if not a.agent else {a.agent: (data.get(name) or {}).get(a.agent)}
    for ag in list(gone):
        (data.get(name) or {}).pop(ag, None)
    write_json(AGENT_BLOCKS, data)
    say(f"{name}: {', '.join(k for k, v in gone.items() if v) or 'nothing was'} skipped; takes jobs again")


def cmd_rename(a) -> None:
    cfg = load_cfg()
    name = node_id(cfg, a.name)
    if name != LOCAL and name not in cfg["nodes"]:
        die(2, f"no machine named '{a.name}' (scaleout nodes)")
    new = (a.new or "").strip()
    if new:
        new = " ".join(new.split())
        if vlen(new) > 24:
            die(2, "a name is at most 24 columns wide")
        for other in list(cfg["nodes"]) + [LOCAL]:
            if other != name and new in (other, label_of(cfg, other)):
                die(2, f"'{new}' is already the name of another machine")
    for holder in ([cfg["nodes"][name]] if name in cfg["nodes"] else []) + ([cfg.setdefault("orchestrator", {})] if name == LOCAL else []):
        if new and new != name:
            holder["label"] = new
        else:
            holder.pop("label", None)
    save_cfg(cfg)
    say(f"{name}: shown as '{label_of(cfg, name)}' ({role_of(cfg, name)}); commands take either name")


# ── tokens of every machine, per day ─────────────────────────────────────────
USAGE_DAYS = STATE_DIR / "usage-days.json"   # {"machines": {id: {"days": {date: tokens}, "ts": read at, "tried": asked at}}}


def collect_usage(cfg: Dict[str, Any], max_age: int = 600, only_local: bool = False) -> Dict[str, Any]:
    """Ask each machine for its daily Claude tokens (all sessions there, not only scaleout jobs): node/unix/usage.py,
    run here for this machine and through ctl on nodes. A machine that does not answer keeps its last numbers."""
    data = read_json(USAGE_DAYS, {})
    mach = data.setdefault("machines", {})
    targets = [LOCAL] + ([] if only_local else [n for n, v in cfg["nodes"].items()
                                                if not v.get("local") and not v.get("pending") and v.get("os", "").lower() != "windows"])
    changed = False
    for name in targets:
        ent = mach.setdefault(name, {"days": {}})
        if now() - ent.get("tried", 0) < max_age:
            continue
        ent["tried"] = now(); changed = True
        try:
            if name == LOCAL:
                r = subprocess.run([sys.executable, str(PKG_ROOT / "node" / "unix" / "usage.py")], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   stdin=subprocess.DEVNULL, timeout=120, env={**os.environ, "SCALEOUT_USAGE_CACHE": str(STATE_DIR / "usage-local.json")})
            else:
                r = Node(name, cfg).ctl("usage", timeout=90)
            got = json.loads(r.stdout.decode().strip().splitlines()[-1])
            days = got["days"]
        except (subprocess.TimeoutExpired, ValueError, IndexError, KeyError, OSError):
            continue   # offline, or a node from before this verb: scaleout update
        if isinstance(days, dict):
            ent["days"].update(days)
            ent["ts"], ent["partial"] = now(), bool(got.get("partial"))
    if changed:
        write_json(USAGE_DAYS, data)
    return data


def day_tokens(ent: Dict[str, Any], since_day: str = "", output: bool = False) -> int:
    return sum((v.get("out", 0) if output else tokens_of(v)) for d, v in (ent.get("days") or {}).items() if d >= since_day)


def grass_lines(totals: Dict[str, int], weeks: int) -> List[str]:
    """Days as squares, a column per week (Monday on top), darker to brighter with the tokens of that day."""
    today = time.localtime()
    t0 = time.mktime(today[:3] + (12, 0, 0, 0, 0, -1))
    start = t0 - (today.tm_wday + 7 * (weeks - 1)) * 86400      # the Monday of the first column
    vals = sorted(v for v in totals.values() if v > 0)
    cuts = [vals[int(len(vals) * q)] for q in (0.25, 0.5, 0.75)] if vals else [1, 1, 1]

    def level(v: int) -> int:
        return 0 if v <= 0 else 1 + sum(v > x for x in cuts)

    def cell(lv: int) -> str:
        return c("■", GRASS[lv]) if PAINT else GRASS_PLAIN[lv]

    head, last, free = [" "] * (weeks * 2 + 2), "", 0
    for w in range(weeks):
        mon = time.strftime("%b", time.localtime(start + w * 7 * 86400))
        if mon != last and w * 2 >= free:
            head[w * 2:w * 2 + 3] = list(mon)
            free = w * 2 + 4
        last = mon
    months = "".join(head).rstrip()
    L = ["     " + c(months, C_DIM)]
    for wd, name in enumerate(("Mon", "", "Wed", "", "Fri", "", "Sun")):
        row = ""
        for w in range(weeks):
            t = start + (w * 7 + wd) * 86400
            row += (" " if t > t0 else cell(level(totals.get(time.strftime("%Y-%m-%d", time.localtime(t)), 0)))) + " "
        L.append(c(f"{name:<5}", C_DIM) + row)
    if vals:
        best = max(totals, key=lambda d: totals[d])
        L.append("     " + c("less ", C_DIM) + " ".join(cell(i) for i in range(5)) + c(" more", C_DIM)
                 + c(f"   busiest {best[5:]} {fmt_tok(totals[best])} · {len(vals)} active days · {fmt_tok(sum(vals) / len(vals))} a day", C_DIM))
    return L


def tokens_lines(cfg: Dict[str, Any], lu: Optional["LocalUsage"], weeks: int = 18, grass: bool = True) -> List[str]:
    """Tokens of every machine (today, 7 days, 30 days, all it has kept) and the grass of all of them together."""
    mach = (read_json(USAGE_DAYS, {}).get("machines") or {})
    day = lambda back: time.strftime("%Y-%m-%d", time.localtime(now() - back * 86400))
    spans = [("today", day(0)), ("7 days", day(6)), ("30 days", day(29)), ("all", "")]
    nw = name_w(cfg, 16)
    L = [c(f"{'MACHINE':<{nw}}{'ROLE':<14}" + "".join(f"{n:>10}" for n, _ in spans) + f"{'since':>8}{'read':>9}", C_DIM)]
    names = sorted(mach, key=lambda n: (n != LOCAL, n))
    tot = [0] * len(spans); out = [0] * len(spans); totals: Dict[str, int] = {}
    for n in names:
        ent = mach[n]
        row = [day_tokens(ent, s) for _, s in spans]
        if not row[3] and n not in cfg.get("nodes", {}) and n != LOCAL:
            continue
        tot = [x + y for x, y in zip(tot, row)]
        out = [x + day_tokens(ent, s, output=True) for x, (_, s) in zip(out, spans)]
        for d, v in (ent.get("days") or {}).items():
            totals[d] = totals.get(d, 0) + tokens_of(v)
        first = min(ent.get("days") or {"": 0})
        age = now() - ent.get("ts", 0)
        read = "never" if not ent.get("ts") else "reading…" if ent.get("partial") else "now" if age < 900 else f"{age // 3600}h ago" if age >= 3600 else f"{age // 60}m ago"
        L.append(pad(c(label_of(cfg, n), C_TEXT, bold=True), nw) + pad(c(role_of(cfg, n), C_VIOLET if role_of(cfg, n) == "orchestrator" else C_DIM), 14)
                 + "".join(f"{fmt_tok(x) if x else '-':>10}" for x in row) + c(f"{first[5:] or '-':>8}", C_DIM)
                 + c(f"{read:>9}", C_DIM if read in ("now", "reading…") or "m ago" in read else C_WARN))
    L.append(pad(c("total", C_ACCENT, bold=True), nw + 14) + "".join(c(f"{fmt_tok(x) if x else '-':>10}", C_ACCENT, bold=True) for x in tot))
    L.append(c(pad("  output tokens", nw + 14) + "".join(f"{fmt_tok(x) if x else '-':>10}" for x in out), C_DIM))
    # wasted by scaleout: what node jobs spent only because each ran as its own agent (the context it loaded before any work)
    waste = [0] * len(spans)
    for j in all_jobs():
        if j.get("bench") or not j.get("start_tokens") or not j.get("finished_at"):
            continue
        d = time.strftime("%Y-%m-%d", time.localtime(j["finished_at"]))
        for i, (_, since) in enumerate(spans):
            if d >= since:
                waste[i] += int(j["start_tokens"])
    if any(waste):
        L.append(pad(c("  wasted by scaleout", C_WARN), nw + 14) + "".join(c(f"{fmt_tok(x) if x else '-':>10}", C_WARN) for x in waste)
                 + c(f"   {waste[1] * 100 / max(1, tot[1]):.1f}% of 7 days", C_WARN))
        L.append(c("    = context each node job loads before any work, spent only because it ran apart (est.)", C_DIM))
    if lu is not None:
        w5 = lu.window(5 * 3600)
        L.append(c(f"  {label_of(cfg, LOCAL)}, last 5 h: {fmt_tok(tokens_of(w5))} ({fmt_tok(w5['out'])} output)", C_DIM))
    if grass:
        L.append("")
        L.extend(grass_lines(totals, weeks))
    per = [job_usage(5 * 3600), job_usage(86400), job_usage(7 * 86400)]
    nodes = sorted(set().union(*[x.keys() for x in per]))
    if nodes:
        L.append("")
        L.append(c(f"{'SCALEOUT JOBS BY MACHINE':<30}{'5 h':>10}{'today':>10}{'7 days':>10}{'jobs 7d':>9}{'busy 7d':>9}{'$ 7d':>8}{'wasted 7d':>10}", C_DIM))
        ext = [0, 0, 0]   # extra tokens, jobs measured, all tokens of those jobs (7 days)
        for n in nodes:
            t = [x.get(n, {}) for x in per]
            e = t[2].get("extra", 0)
            ext[0] += e; ext[1] += t[2].get("measured", 0); ext[2] += t[2].get("tokens", 0)
            L.append(pad(label_of(cfg, n), 30) + "".join(f"{fmt_tok(x.get('tokens', 0)):>10}" for x in t)
                     + f"{int(t[2].get('jobs', 0)):>9}{t[2].get('busy_s', 0) / 3600:>8.1f}h{t[2].get('cost', 0):>8.2f}"
                     + c(f"{fmt_tok(e) if t[2].get('measured') else '-':>10}", C_WARN if e else C_DIM))
        if ext[1]:
            L.append(c(f"  wasted 7d: {fmt_tok(ext[0])} over {ext[1]} jobs = {ext[0] * 100 / max(1, ext[2]):.1f}% of their tokens"
                       f" · not counted: files a job re-read, logs this session skipped", C_DIM))
    return L


OS_NAMES = {"darwin": "macOS", "linux": "Linux", "windows": "Windows"}
OS_TAGS = {"darwin": {"mac", "macos"}, "linux": {"linux"}, "windows": {"windows", "win"}}   # what --tags may say for an OS
ARCH_NAMES = {"aarch64": "arm64", "x86_64": "x64", "amd64": "x64"}


def fmt_os(st: Dict[str, Any], node: Optional[Dict[str, Any]] = None) -> str:
    """'macOS arm64', 'Linux x64', 'Windows x64': what a job has to run on (iOS work needs macOS, bash scripts not Windows)."""
    o = str(st.get("os") or (node or {}).get("os") or "").strip()
    if not o:
        return "?"
    arch = str(st.get("arch") or "").strip().lower()
    return (OS_NAMES.get(o.lower(), o) + " " + ARCH_NAMES.get(arch, arch)).strip()


def cmd_status(a) -> None:
    cfg = load_cfg()
    names = a.names or None
    metrics = refresh_metrics(cfg, names, max_age=0)
    say(f"orchestrator {socket.gethostname().split('.')[0]} · agent-scaleout {VERSION}")
    nw = name_w(cfg, 16)
    say(f"{'machine':<{nw}}{'role':<14}{'os':<14}{'state':<12}{'route':<8}{'mem free':>9}{'disk free':>11}{'load':>8}{'jobs':>7}  {'devices':<9}agents")
    for name in sorted(metrics):
        st = metrics[name]
        h = node_health(cfg, name, st)
        route = "-" if not st.get("online") else ("local" if cfg["nodes"].get(name, {}).get("local") else "lan" if str(st.get("route", "")).endswith("-lan") else "tunnel")
        say(f"{pad(label_of(cfg, name), nw)}{role_of(cfg, name):<14}{fmt_os(st, cfg['nodes'].get(name)):<14}{pad(c(h, C_OK if h == 'ok' else C_WARN), 12)}{route:<8}{str(st.get('mem_free_pct', '-')) + '%':>9}{str(st.get('disk_free_gb', '-')) + 'GB':>11}"
            f"{str(st.get('load', '-')):>8}{str(st.get('running', 0)) + '+' + str(st.get('queued', 0)):>7}  {fmt_devices(st):<9}{','.join(st.get('agents') or []) or '-'}")
    q = [j for j in all_jobs() if j.get("status") in ("queued", "dispatched", "running")]
    say(f"queue: {sum(1 for j in q if j['status'] == 'queued')} waiting · {sum(1 for j in q if j['status'] != 'queued')} running")
    for name, ags in (read_json(AGENT_BLOCKS, {}) or {}).items():
        for ag, b in (ags or {}).items():
            if b.get("until", 0) > now():
                say(f"  {label_of(cfg, name)}: {ag} skipped ({b.get('why')}) until {time.strftime('%m-%d %H:%M', time.localtime(b['until']))}"
                    + (f"  — sign in on that machine, then: scaleout unblock {name}" if b.get("why") == "signed out" else ""))


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
    for name in a.names or [n for n, v in cfg["nodes"].items() if not v.get("local")]:
        r = Node(name, cfg).ctl("cleanup", "deep" if a.deep else ("aggressive" if a.aggressive else ""),
                                str(cfg["defaults"]["cleanup_target_disk_gb"]), timeout=900)
        say(f"{name}: {r.stdout.decode().strip() or r.stderr.decode().strip()}")


def cmd_sync_skills(a) -> None:
    cfg = load_cfg()
    data, h = skills_tar(cfg)
    say(f"skills bundle {len(data) // 1024} KB ({', '.join(p for p in cfg['skills']['paths'] if (HOME / p).exists())}) · {h}")
    for name in a.names or [n for n, v in cfg["nodes"].items() if not v.get("pending") and not v.get("local")]:
        try:
            say(f"{name}: {'sent' if push_skills(cfg, name, force=True) else ('failed' if Node(name, cfg).route() else 'unreachable')}")
        except Exception as e:  # noqa: BLE001
            say(f"{name}: failed ({e})")


def cmd_update(a) -> None:
    cfg = load_cfg()
    for name in a.names or list(cfg["nodes"]):
        n = Node(name, cfg)
        node = cfg["nodes"][name]
        if n.local:
            local_install(force=True)
            say(f"{name}: updated")
            continue
        if n.windows:
            r = n.ctl("update", input=node_package("windows"), timeout=300)
        else:
            up = n.shell("mkdir -p ~/.scaleout/pkg && tar -xzf - -C ~/.scaleout/pkg", input=node_package("unix"), timeout=300)
            args = ["--name", name, "--dir", node.get("dir", "~/scaleout-work"), "--max-jobs", str(job_cap(name, node)), "--agent", node.get("agent", "claude")]
            if node.get("port"):
                args += tunnel_args(cfg, node["port"])
            r = n.shell("bash ~/.scaleout/pkg/setup.sh " + " ".join(shlex.quote(x) for x in args), timeout=300) if up.returncode == 0 else up
        say(f"{name}: {'updated' if r.returncode == 0 else 'failed: ' + r.stderr.decode().strip()[-200:]}")
    if tokens_cfg(cfg)["on"]:   # a new or updated node gets the token saving setting too
        tokens_apply(cfg, a.names or None)


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
    elif re.fullmatch(r"-?\d*\.\d+", v):
        v = float(v)
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
        DAEMON_ALIVE.touch()
        try:
            collect_usage(load_cfg())   # each machine at most every 10 min
        except Exception as e:
            say(f"usage error: {e}")
        time.sleep(a.interval)


def cmd_install_daemon(a) -> None:
    exe = shutil.which("scaleout") or str(HOME / ".local" / "bin" / "scaleout")
    log = STATE_DIR / "daemon.log"
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if platform.system() == "Darwin":
        # Apple's python, started by /bin/bash: macOS treats Homebrew's Python.app as an app of its own and, under launchd,
        # every file access on an external disk (repositories on an SSD) waits forever for a privacy prompt nobody sees
        py = "/usr/bin/python3" if run(["xcode-select", "-p"]).returncode == 0 else sys.executable
        p = HOME / "Library" / "LaunchAgents" / "dev.scaleout.orchestrator.plist"
        p.write_text(f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>dev.scaleout.orchestrator</string>
<key>ProgramArguments</key><array><string>/bin/bash</string><string>-c</string><string>{py} {exe} daemon; exit $?</string></array>
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
        r = n.ctl("runner-install", url, gh_token(scope, "registration-token"), rname, labels, *([a.group] if a.group else []), timeout=900)
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
        cap = max(1, sc["cpu_slots"])   # upper bound; memory, load and CPU units decide each dispatch
        if apply and cfg["nodes"][name].get("cpu_slots") != sc["cpu_slots"]:
            cfg["nodes"][name]["cpu_slots"] = sc["cpu_slots"]
            save_cfg(cfg)
        if apply and cfg["nodes"][name].get("max_jobs") != cap and not cfg["nodes"][name].get("max_jobs_fixed"):
            cfg["nodes"][name]["max_jobs"] = cap
            Node(name, cfg).ctl("conf", "MAX_JOBS", str(cap))
            save_cfg(cfg)
            say(f"  {name}: max_jobs {cap} (CPU bound; memory is checked on every dispatch)")
    total = sum(v["score"] for v in cur.values())
    before = sum(v["score"] for k, v in prev.items() if k in cfg["nodes"])
    full = bench_history()
    full.append({"t": now(), "nodes": cur, "total": total})
    write_json(STATE_DIR / "bench.json", full[-200:])
    if not quiet_table:
        nw = name_w(cfg, 16)
        say(f"{'machine':<{nw}}{'model':<30}{'mem':>5}{'cores':>6}{'1-core':>8}{'all':>7}{'jobs':>6}{'score':>7}{'price':>11}{'per 10k':>9}")
        for name in sorted(cur):
            v = cur[name]
            say(f"{pad(label_of(cfg, name), nw)}{str(v.get('model', ''))[:29]:<30}{str(v.get('mem_gb', '?')) + 'G':>5}{str(v.get('cpus', '?')):>6}"
                f"{v['single_x']:>7.2f}x{v['multi_x']:>6.2f}x{v['slots']:>6}{v['score']:>7}"
                f"{(format(v['price'], ',') if v.get('price') else '-'):>11}{per10k(v['score'], v.get('price')):>9}")
    for name in names:
        if name in cur and name not in prev:
            v = cur[name]
            paid = f" for {v['price']:,} -> {per10k(v['score'], v['price'])} points per 10k" if v.get("price") else ""
            say(f"  {label_of(cfg, name)} added {v['score']} points{paid}")
    x = f" ({total / before:.2f}x)" if before else ""
    say(f"pool score {total}  (was {before}, {total - before:+d}{x}) · 100 = one M1 8GB Mac mini doing one mobile job")


def run_load(cfg: Dict[str, Any], jobs: int, max_s: int, only: Optional[List[str]] = None) -> None:
    names = [n for n, v in cfg["nodes"].items() if not v.get("pending") and v.get("os", "").lower() != "windows" and (not only or n in only)]
    if only and not names:
        die(E_USAGE, "none of those machines can take load test jobs")
    slots = sum(int(cfg["nodes"][n].get("max_jobs", 2)) for n in names)
    jobs = jobs or max(4, 3 * slots)
    say(f"load test: {jobs} synthetic build jobs (~2.5 GB, 4 busy cores, 512 MB written each) on {', '.join(label_of(cfg, n) for n in names)} · {slots} slot(s)")
    ids = []
    for _ in range(jobs):
        jid = submit_job(cfg, "load test", agent="bench", timeout=20)
        j = load_job(jid)
        j["bench"] = True
        if only:
            j["only"] = names   # these machines only
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
        say(f"  {pad(label_of(cfg, n), 22)}{len(per[n]):>3} jobs · avg {sum(per[n]) / len(per[n]) / 1000:.0f}s each")
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
        return run_load(cfg, a.jobs, a.max, a.names or None)
    if a.self_:
        r = run(["bash", str(PKG_ROOT / "node" / "unix" / "ctl.sh"), "bench"], timeout=600)
        b = json.loads(r.stdout.decode().strip().splitlines()[-1])
        sc = bench_score(b)
        write_json(STATE_DIR / "bench-self.json", {"t": now(), **sc, "model": b.get("model", ""), "mem_gb": b.get("mem_total_gb")})
        say(f"orchestrator {b.get('model', '')} · {b.get('mem_total_gb')}G · score {sc['score']} ({sc['slots']} job slots)"
            " — the baseline `scaleout report` compares nodes with (measured while busy reads lower)")
        return
    names = a.names or [n for n, v in cfg["nodes"].items() if not v.get("pending")]
    run_bench(names, apply=a.apply)


def p90(xs: List[float]) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(0.9 * (len(xs) - 1))))]


def tune(cfg: Dict[str, Any], apply: bool = False, quiet: bool = False, days: int = 14) -> None:
    """max_jobs = the CPU slots `scaleout bench` measured. Memory is checked for every job when it is handed out (it must
    fit in the node's free memory), so the worst jobs' memory must not cap how many small ones run side by side; only a
    node bench has not measured falls back to (total - idle use - 1 GB headroom) / p90 of the memory one real job added."""
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
        rec = max(1, min(cpu if cpu != 99 else mem_slots, 8))
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


# ── commands: provision ──────────────────────────────────────────────────────
def local_toolchain() -> Dict[str, str]:
    """What this machine uses, so nodes can match it."""
    def out(cmd: List[str]) -> str:
        try:
            return run(cmd, timeout=60).stdout.decode().strip()
        except Exception:
            return ""
    fv = HOME / "fvm" / "versions"
    flutter = " ".join(sorted(p.name for p in fv.iterdir() if re.fullmatch(r"\d+\.\d+\.\d+", p.name))) if fv.is_dir() else ""
    plat = HOME / "Library" / "Android" / "sdk" / "platforms"
    # the Java Android builds run on here (Flutter's jdk-dir, else Android Studio's JBR): Gradle refuses one newer than it knows
    jdk = re.search(r'"jdk-dir"\s*:\s*"([^"]+)"', (HOME / ".config" / "flutter" / "settings").read_text()) if (HOME / ".config" / "flutter" / "settings").exists() else None
    java = Path(jdk.group(1)) / "bin" / "java" if jdk else Path("/Applications/Android Studio.app/Contents/jbr/Contents/Home/bin/java")
    jv = re.search(r'version "(\d+)', run([str(java), "-version"], timeout=30).stderr.decode()) if java.exists() else None
    return {
        "FLUTTER": flutter,
        "POD": out(["pod", "--version"]),
        "FIREBASE": out(["firebase", "--version"]),
        "NODE_MAJOR": re.sub(r"^v(\d+).*", r"\1", out(["node", "--version"])),
        "XCODE": re.sub(r"^Xcode ", "", out(["xcodebuild", "-version"]).splitlines()[0] if out(["xcodebuild", "-version"]) else ""),
        "ANDROID": " ".join(sorted(p.name for p in plat.iterdir())) if plat.is_dir() else "",
        "JAVA_MAJOR": jv.group(1) if jv else "",
    }


def cmd_provision(a) -> None:
    cfg = load_cfg()
    want = local_toolchain()
    if a.flutter:
        want["FLUTTER"] = a.flutter.replace(",", " ")
    if a.android:
        want["ANDROID"] = a.android.replace(",", " ")
    for k in (a.skip or "").split(","):
        if k:
            want[k.upper()] = ""
    say("target (from this machine): " + " · ".join(f"{k.lower()} {v}" for k, v in want.items() if v))
    for name in a.names or [n for n, v in cfg["nodes"].items() if not v.get("pending")]:
        n = Node(name, cfg)
        if n.windows:
            say(f"{name}: provisioning Windows nodes is not supported yet")
            continue
        args = [f"{k}={v}" for k, v in want.items() if v] + (["DRY_RUN=1"] if a.dry_run else [])
        r = n.ctl("provision", *args, timeout=7200)
        say(f"== {name}")
        todo = 0
        for line in r.stdout.decode().splitlines():
            st, _, rest = line.partition(" ")
            item, _, detail = rest.partition(" ")
            todo += st in ("TODO", "FAIL")
            say(f"  {st:<5} {item:<16} {detail}")
        if r.returncode != 0 and not r.stdout:
            say(f"  ! {r.stderr.decode().strip()[-300:]}")
        say(f"  {'everything matches' if not todo else f'{todo} item(s) need attention'}")


# ── token saving (`scaleout tokens`) ─────────────────────────────────────────
# Every tool call re-reads the session's whole context from the cache, so tokens ≈ calls × context size. Claude Code
# compacts near the model's full window (~967k on 1M models), so long sessions pay for 500k-1M on every step. Token
# saving compacts earlier, keeps what a summary would lose in two files that are put back word for word right after
# it (the agent's notes + a ledger taken from the transcript just before), and can tune the window from what it measures.
CLAUDE_SETTINGS = HOME / ".claude" / "settings.json"
WINDOW_ENV = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"
TOKENS_DEFAULT: Dict[str, Any] = {"on": False, "window": 300000, "auto": False, "fork_max": 100000,
                                  "min": 200000, "max": 400000}
TOKENS_DIR = STATE_DIR / "tokens"
RESERVE = 35000              # Claude Code compacts this far below the window (it keeps room for the answer and the summary)
POST, POST_WRITE = 124000, 90000   # until measured: the context right after a compaction (tools, skills, memory, summary, files put back, and what came after a summary taken in advance) and the part of it written to the cache anew
SUMMARY_OUT = 8000           # tokens a summary takes to write
W_OUT, W_WRITE = 100, 40     # an output token and a 1 h cache write in cache reads (Opus 5.5 API prices, checked against the cost of 313 jobs)
HOOK_MAX = 9500              # Claude Code takes up to 10,000 characters from one hook; a longer text reaches the session as a 2 KB preview
LEDGER_MAX = 9000
NOTES_DIR = ".scaleout"
JOB_ID_RE = re.compile(r"\bj\d{8}-\d{6}-[0-9a-f]{4}\b")


def tokens_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    return {**TOKENS_DEFAULT, **(cfg.get("tokens") or {})}


def parse_tokens(v: str) -> int:
    """'200k', '0.2M' or '200000' -> 200000."""
    m = re.fullmatch(r"\s*([\d.]+)\s*([kKmM]?)\s*", str(v))
    if not m:
        die(E_USAGE, f"not a token count: {v} (e.g. 200k)")
    n = float(m.group(1)) * {"": 1, "k": 1e3, "m": 1e6}[m.group(2).lower()]
    return int(n)


def claude_env(key: str, value: Optional[Any]) -> bool:
    """Set (value) or remove (None) one env entry in this machine's Claude Code user settings. A file that is not valid
    JSON is never rewritten."""
    s: Dict[str, Any] = {}
    if CLAUDE_SETTINGS.exists():
        try:
            s = json.loads(CLAUDE_SETTINGS.read_text())
        except ValueError:
            return False
    env = s.setdefault("env", {})
    if value is None:
        env.pop(key, None)
    else:
        env[key] = str(value)
    if not env:
        s.pop("env")
    CLAUDE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    tmp = CLAUDE_SETTINGS.with_name(CLAUDE_SETTINGS.name + ".scaleout-tmp")
    tmp.write_text(json.dumps(s, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, CLAUDE_SETTINGS)
    return True


def _usage_tokens(u: Dict[str, Any]) -> int:
    return int(u.get("input_tokens", 0) or 0) + int(u.get("cache_read_input_tokens", 0) or 0) + int(u.get("cache_creation_input_tokens", 0) or 0)


def transcript_context(path: str) -> int:
    """Context the session's last request read (input + cache), from the end of its transcript."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 512 * 1024))
            lines = f.read().splitlines()
    except (OSError, TypeError):
        return 0
    for line in reversed(lines):
        if b'"usage"' not in line:
            continue
        try:
            n = _usage_tokens((json.loads(line).get("message") or {}).get("usage") or {})
        except (ValueError, AttributeError):
            continue
        if n:
            return n
    return 0


def notes_paths(project: str, sid: str = "") -> Tuple[Path, Path]:
    """Each session has its own notes and ledger: several sessions often work in one folder."""
    d, s = Path(project or ".") / NOTES_DIR, re.sub(r"[^A-Za-z0-9]", "", sid or "")[:8]
    return (d / f"notes-{s}.md", d / f"ledger-{s}.md") if s else (d / "notes.md", d / "ledger.md")


def keep_out_of_git(project: str) -> None:
    """`.scaleout/` goes in the repository's local exclude file: never committed, the project's .gitignore untouched."""
    try:
        p = Path(project).resolve()
        for d in [p, *p.parents]:
            g = d / ".git"
            if g.is_dir():
                common = g
                break
            if g.is_file():
                m = re.match(r"gitdir:\s*(.+)", g.read_text().strip())
                if not m:
                    return
                gd = Path(m.group(1))
                gd = gd if gd.is_absolute() else (d / gd)
                cd = gd / "commondir"
                common = (gd / cd.read_text().strip()).resolve() if cd.exists() else gd
                break
        else:
            return
        ex = common / "info" / "exclude"
        text = ex.read_text() if ex.exists() else ""
        if not re.search(r"^/?\.scaleout/?$", text, re.M):
            ex.parent.mkdir(parents=True, exist_ok=True)
            ex.write_text(text + ("" if text.endswith("\n") or not text else "\n") + ".scaleout/\n")
    except OSError:
        pass


HARNESS_TEXT = ("<command-", "<local-command", "<system-reminder>", "<task-notification>", "[Request interrupted", "Caveat:",
                "<bash-", "[SYSTEM NOTIFICATION", "<cross-session", "<agent-message", "<teammate-message",
                "This session is being continued from a previous conversation", "Stop hook feedback")
REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)


def _user_text(msg: Dict[str, Any]) -> str:
    """What the user typed in a transcript entry ('' for tool results and the harness's own messages; reminders the
    harness put around the user's words are cut out, the words kept)."""
    c = msg.get("content")
    blocks = [c] if isinstance(c, str) else [b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"] \
        if isinstance(c, list) and not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c) else []
    kept = []
    for b in blocks:
        b = REMINDER_RE.sub("", b or "").strip()
        if b and not b.startswith(HARNESS_TEXT):
            kept.append(b)
    return "\n".join(kept)


def _entry_user_text(d: Dict[str, Any]) -> str:
    """The user's words in a transcript line: a typed message, or one sent while the agent was working (queued_command)."""
    if d.get("type") == "user" and not d.get("isMeta") and not d.get("isCompactSummary") and not d.get("isSidechain"):
        return _user_text(d.get("message") or {})
    a = d.get("attachment") if d.get("type") == "attachment" else None
    if isinstance(a, dict) and a.get("type") == "queued_command" and (a.get("origin") or {}).get("kind", "human") == "human":
        p = a.get("prompt")
        return REMINDER_RE.sub("", p).strip() if isinstance(p, str) else ""
    return ""


def _fit(lines: List[str], limit: int) -> List[str]:
    """The latest lines that fit in `limit` characters, in their order."""
    out, n = [], 0
    for x in reversed(lines):
        x = x if len(x) <= limit else x[:limit - 1] + "…"
        if n + len(x) + 1 > limit:
            break
        out.append(x)
        n += len(x) + 1
    return out[::-1]


def build_ledger(transcript: str, ctx: int = 0, project: str = "") -> str:
    """A ledger of the session taken from its transcript, no model call: the task list, job ids, the user's latest messages
    word for word, files changed and read, recent commands. Each part has its share, so all of it fits in one hook."""
    def short(path: str) -> str:
        for root, to in ((str(project).rstrip("/") + "/" if project else "", ""), (str(HOME) + "/", "~/")):
            if root and path.startswith(root):
                return to + path[len(root):]
        return path
    users: List[str] = []
    edits: Dict[str, int] = {}
    reads: List[str] = []
    cmds: List[str] = []
    jobs: List[str] = []
    todos: List[Dict[str, Any]] = []
    tasks: List[List[str]] = []   # [subject, status] in creation order (TaskCreate ids count from 1)
    try:
        fh = open(transcript, errors="replace")
    except (OSError, TypeError):
        return ""
    with fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            msg = d.get("message") or {}
            t = _entry_user_text(d)
            if t:
                ts = d.get("timestamp") or ((d.get("attachment") or {}).get("timestamp") if isinstance(d.get("attachment"), dict) else "") or ""
                try:   # transcripts keep UTC; the ledger shows this machine's time
                    ts = time.strftime("%m-%d %H:%M", time.localtime(time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone))
                except ValueError:
                    ts = ts[:16]
                users.append("- (" + ts + ") " + re.sub(r"\n\s*\n+", "\n", t)[:600])
            if d.get("type") != "assistant" or not isinstance(msg.get("content"), list):
                continue
            for b in msg["content"]:
                if not isinstance(b, dict) or b.get("type") != "tool_use":
                    continue
                name, inp = b.get("name", ""), b.get("input") or {}
                path = inp.get("file_path") or inp.get("notebook_path") or ""
                if name in ("Edit", "Write", "MultiEdit", "NotebookEdit") and path:
                    edits[path] = edits.get(path, 0) + 1
                    edits[path] = edits.pop(path)   # most recent last
                elif name == "Read" and path:
                    if path in reads:
                        reads.remove(path)
                    reads.append(path)
                elif name == "Bash" and inp.get("command"):
                    cmd = " ".join(str(inp["command"]).split())
                    cmds.append(cmd[:220])
                    for jid in JOB_ID_RE.findall(cmd):
                        if jid not in jobs:
                            jobs.append(jid)
                elif name == "TodoWrite" and isinstance(inp.get("todos"), list):
                    todos = inp["todos"]
                elif name == "TaskCreate" and inp.get("subject"):
                    tasks.append([str(inp["subject"]), "pending"])
                elif name == "TaskUpdate" and str(inp.get("taskId", "")).isdigit():
                    k = int(inp["taskId"]) - 1
                    if 0 <= k < len(tasks):
                        if inp.get("subject"):
                            tasks[k][0] = str(inp["subject"])
                        if inp.get("status"):
                            tasks[k][1] = str(inp["status"])
    out = [f"# Ledger — taken automatically from the transcript ({time.strftime('%Y-%m-%d %H:%M')}"
           + (f", context {ctx // 1000}k" if ctx else "") + ")", ""]
    live = [t for t in tasks if t[1] != "deleted"]
    todo = [f"- [{t.get('status', '?')}] {t.get('content') or t.get('subject') or ''}" for t in todos if isinstance(t, dict)] \
        + [f"- [{s}] {subj}" for subj, s in live if s != "completed"]
    done = [f"- [completed] {subj}" for subj, s in live if s == "completed"]
    for title, lines in (("Task list", _fit(done, 400) + _fit(todo, 1400)),
                         ("scaleout jobs mentioned", _fit([f"- {j}" for j in jobs[-10:]], 400)),
                         ("The user's latest messages (oldest first, word for word)", _fit(users[-15:], 3600)),
                         ("Files changed (most recent last)", _fit([f"- {short(p)}" + (f" ×{n}" if n > 1 else "") for p, n in edits.items()][-25:], 1200)),
                         ("Recent commands (oldest first)", _fit([f"- `{c}`" for c in cmds[-12:]], 1200)),
                         ("Files read recently", _fit([f"- {short(p)}" for p in reads[-12:]], 600))):
        if lines:
            out += [f"## {title}"] + lines + [""]
    return "\n".join(out)[:LEDGER_MAX]


def _hook_input() -> Dict[str, Any]:
    try:
        return json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return {}


def _project_of(h: Dict[str, Any]) -> str:
    """The session's folder; inside a worktree under .claude/worktrees/ that worktree (a session there may not write outside it)."""
    m = re.match(r"(.+?/\.claude/worktrees/[^/]+)(?:/|$)", str(h.get("cwd") or ""))
    return m.group(1) if m else os.environ.get("CLAUDE_PROJECT_DIR") or h.get("cwd") or os.getcwd()


def _session_file(h: Dict[str, Any], kind: int) -> Optional[Path]:
    """This session's notes (kind 0) or ledger (1), the newest one when it moved between a folder and its worktrees."""
    roots = {_project_of(h), os.environ.get("CLAUDE_PROJECT_DIR") or "", h.get("cwd") or ""} - {""}
    f = TOKENS_DIR / "compactions.jsonl"      # and the folders its earlier compactions were in: the working folder changes
    for line in f.read_text().splitlines() if f.exists() and h.get("session_id") else []:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("session") == h["session_id"] and ev.get("project"):
            roots.add(ev["project"])
    found = [f for f in (notes_paths(r, h.get("session_id", ""))[kind] for r in roots) if f.exists() and f.stat().st_size]
    return max(found, key=lambda f: f.stat().st_mtime) if found else None


def _wrote(transcript: str, path: Path) -> bool:
    """Whether this session wrote that file (its own Edit or Write calls)."""
    try:
        with open(transcript, errors="replace") as fh:
            for line in fh:
                if path.name not in line or '"tool_use"' not in line:
                    continue
                try:
                    blocks = (json.loads(line).get("message") or {}).get("content")
                except (ValueError, AttributeError):
                    continue
                for b in blocks if isinstance(blocks, list) else []:
                    if isinstance(b, dict) and b.get("name") in ("Edit", "Write") and (b.get("input") or {}).get("file_path") == str(path):
                        return True
    except (OSError, TypeError):
        pass
    return False


def _cap(text: str, limit: int, path: Path) -> str:
    return text if len(text) <= limit else text[:limit - len(str(path)) - 60].rstrip() + f"\n\n(cut here: the rest is in {path} — read it)"


def _first_line(f: Path) -> str:
    try:
        return next((x.strip("# ").strip() for x in f.read_text(errors="replace").splitlines() if x.strip()), "")[:120]
    except OSError:
        return ""


def agents_dir() -> Path:
    """agents/claude of this package (a checkout or the installed copy)."""
    d = PKG_ROOT / "agents" / "claude"
    return d if d.is_dir() else SHARE / "agents" / "claude"


def token_rules(t: Dict[str, Any], project: str, sid: str = "") -> str:
    f = agents_dir() / "token-rules.md"
    if not f.exists():
        return ""
    notes, ledger = notes_paths(project, sid)
    return (f.read_text().replace("{at_k}", str((int(t["window"]) - RESERVE) // 1000)).replace("{fork_k}", str(int(t["fork_max"]) // 1000))
            .replace("{notes}", str(notes)).replace("{ledger}", str(ledger)))


def notes_context(h: Dict[str, Any]) -> str:
    """After a compaction: this session's notes, word for word. In a new session: where other sessions left theirs (not their text)."""
    project, source = _project_of(h), h.get("source", "startup")
    if source == "compact":
        f = _session_file(h, 0)
        if f is None:   # notes of versions before 0.9.8 were one file per folder: this session's only if it wrote it
            old = notes_paths(project)[0]
            f = old if old.exists() and _wrote(h.get("transcript_path") or "", old) else None
        if f is None:
            return ""
        head = f"# Your notes, restored after the compaction ({f}): trust them over the summary where they differ\n\n"
        return head + _cap(f.read_text(errors="replace"), HOOK_MAX - len(head), f)
    if source not in ("startup", "clear"):
        return ""
    d = Path(project) / NOTES_DIR
    own = notes_paths(project, h.get("session_id", ""))[0].name
    recent = sorted((f for f in d.glob("notes*.md") if f.name != own and f.stat().st_size and now() - f.stat().st_mtime < 14 * 86400),
                    key=lambda f: f.stat().st_mtime, reverse=True)[:3] if d.is_dir() else []
    if not recent:
        return ""
    return "\n".join(["# Notes other sessions left in this folder (read one only to continue that session's work; it is not yours to edit)"]
                     + [f"- {f} ({time.strftime('%m-%d %H:%M', time.localtime(f.stat().st_mtime))}): {_first_line(f)}" for f in recent])


def ledger_context(h: Dict[str, Any]) -> str:
    """After a compaction: the ledger. Taken now when no PreCompact hook took it (a session older than that hook)."""
    if h.get("source") != "compact":
        return ""
    f = fresh_ledger(h)
    if f is None:
        return ""
    head = f"# Ledger of this session before the compaction ({f})\n\n"
    return head + _cap(f.read_text(errors="replace"), HOOK_MAX - len(head), f)


def fresh_ledger(h: Dict[str, Any]) -> Optional[Path]:
    """The ledger of the compaction that just ran, taken now when the PreCompact hook did not."""
    TOKENS_DIR.mkdir(parents=True, exist_ok=True)
    with open(TOKENS_DIR / "ledger.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)   # the session and ledger hooks of one compaction both ask for it
        f = _session_file(h, 1)
        return f if f is not None and now() - f.stat().st_mtime < 600 else write_ledger(h)


def write_ledger(h: Dict[str, Any]) -> Optional[Path]:
    project, tp = _project_of(h), h.get("transcript_path") or ""
    ctx = transcript_context(tp)
    text = build_ledger(tp, ctx, project)
    _log_tokens("compactions.jsonl", {"t": int(now()), "session": h.get("session_id", ""), "transcript": tp, "project": project,
                                      "ctx": ctx, "trigger": h.get("trigger") or h.get("source") or "", "window": tokens_cfg(load_cfg())["window"]})
    if not text:
        return None
    f = notes_paths(project, h.get("session_id", ""))[1]
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text + "\n")
    keep_out_of_git(project)
    for old in f.parent.glob("ledger*.md"):
        if now() - old.stat().st_mtime > 14 * 86400:
            old.unlink()
    return f


def _log_tokens(name: str, rec: Dict[str, Any]) -> None:
    TOKENS_DIR.mkdir(parents=True, exist_ok=True)
    with open(TOKENS_DIR / name, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def hook_tokens(event: str, h: Optional[Dict[str, Any]] = None) -> None:
    """precompact · notes · ledger · ctx · agent: the token saving hooks (they do nothing while `scaleout tokens` is off)."""
    if not CONFIG.exists():
        return
    t = tokens_cfg(load_cfg())
    if not t["on"]:
        return
    h = _hook_input() if h is None else h
    project, tp = _project_of(h), h.get("transcript_path") or ""
    if event in ("notes", "ledger"):
        text = notes_context(h) if event == "notes" else ledger_context(h)
        if text:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}, ensure_ascii=False))
        return
    if event == "precompact":   # full context is still there: take the ledger now
        write_ledger(h)
        return
    if event == "agent":        # a fork re-reads this whole context on every step
        if (h.get("tool_input") or {}).get("subagent_type") != "fork":
            return
        ctx = transcript_context(tp)
        if ctx > int(t["fork_max"]):
            reason = (f"Token saving: this session's context is {ctx // 1000}k, and a fork inherits all of it and re-reads it on "
                      f"every step (above {int(t['fork_max']) // 1000}k forks are refused). Start a fresh subagent instead "
                      "(general-purpose, Explore or scaleout) with a standalone brief: the goal, the files, what to return.")
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                     "permissionDecisionReason": reason}}))
        return
    if event == "ctx":          # near the window: have the agent write its notes while it still has everything
        ctx, win = transcript_context(tp), int(t["window"])
        at = win - RESERVE   # where Claude Code really compacts
        if ctx < 0.85 * at:
            return
        sid = re.sub(r"[^A-Za-z0-9-]", "", h.get("session_id", "")) or "x"
        f = TOKENS_DIR / "nudge" / f"{sid}.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        lock = open(f.with_suffix(".lock"), "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return   # the other hook of the same call is on it
        last = read_json(f, {})
        notes, _ = notes_paths(project, h.get("session_id", ""))
        fresh = notes.exists() and now() - notes.stat().st_mtime < 600
        if fresh or (last.get("ctx", 0) and last["ctx"] <= ctx and now() - last.get("t", 0) < 1800):
            return   # notes just written, or already told since the last compaction
        write_json(f, {"t": int(now()), "ctx": ctx})
        keep_out_of_git(project)
        msg = (f"[token saving] Context is {ctx // 1000}k and Claude Code compacts at about {at // 1000}k. Before it does, write "
               f"{notes} (this session's notes) now: goal, decisions and why, done, next steps, key paths and commands, and the user's corrections in "
               "their words. Replace stale lines; keep it short. It is put back word for word after the compaction.")
        print(json.dumps({"hookSpecificOutput": {"hookEventName": h.get("hook_event_name") or "PostToolUse", "additionalContext": msg}}))


def _today_requests(days: float = 1.0) -> Dict[str, List[Tuple[str, int]]]:
    """Requests of this machine's sessions in the last `days`, each counted once (message id): per transcript, (time, context)."""
    root, since, seen = HOME / ".claude" / "projects", now() - days * 86400, set()
    out: Dict[str, List[Tuple[str, int]]] = {}
    for f in list(root.glob("*/*.jsonl")) + list(root.glob("*/*/subagents/*.jsonl")):
        try:
            if f.stat().st_mtime < since:
                continue
            fh = open(f, errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                m, ts = d.get("message") or {}, d.get("timestamp", "")
                u = m.get("usage")
                if not u or not ts:
                    continue
                try:
                    tsec = time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
                except ValueError:
                    continue
                if tsec < since:
                    continue
                key = m.get("id") or d.get("uuid")
                if key in seen:
                    continue
                seen.add(key)
                out.setdefault(str(f), []).append((ts, _usage_tokens(u)))
    for v in out.values():
        v.sort()
    return out


def whatif(reqs: Dict[str, List[Tuple[str, int]]], window: int, post: int = POST) -> Tuple[float, int]:
    """Tokens the same requests would have read with auto-compaction at `window`, and how many compactions that takes.
    Each request keeps the growth it really had; where Claude Code compacts (RESERVE below the window) the context is read
    once more for the summary and falls back to `post`, what is there right after a compaction."""
    total, comps, at = 0.0, 0, window - RESERVE
    for v in reqs.values():
        sim = prev = None
        for _, c in v:
            if sim is None:
                sim = min(c, at)
            else:
                # where the session itself compacted (a big drop) the replay goes on: at another window it may not have, and
                # a free fall there would make the largest window look cheap
                grow = 0 if c < 0.7 * prev else c - prev
                sim = max(0, sim + grow)
                if sim > at:
                    comps, total = comps + 1, total + at
                    sim = post + max(0, grow)
            prev = c
            total += sim
    return total, comps


def compaction_stats(days: int = 7) -> List[Dict[str, Any]]:
    """What followed each compaction of the sessions in compactions.jsonl, measured in their transcripts: the context and cache
    write of the next request, the requests until the next compaction, and the context read by those (first 40) that re-read a
    file read before it. The compactions are the transcript's own compact_boundary lines: from a 250k window up Claude Code calls
    the PreCompact hook ahead of time (it takes the summary in advance), sometimes twice for one compaction."""
    f = TOKENS_DIR / "compactions.jsonl"
    evs: Dict[str, List[Dict[str, Any]]] = {}
    for line in f.read_text().splitlines() if f.exists() else []:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("t", 0) >= now() - days * 86400 and ev.get("transcript"):
            evs.setdefault(ev["transcript"], []).append(ev)
    out: List[Dict[str, Any]] = []
    for tp, es in evs.items():
        es.sort(key=lambda e: e["t"])
        reqs: List[Dict[str, Any]] = []
        bounds: List[Tuple[float, int, int]] = []   # time, context before it, requests before it
        try:
            fh = open(tp, errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"usage"' not in line and '"compact_boundary"' not in line:
                    continue
                try:
                    d = json.loads(line)
                    tsec = calendar.timegm(time.strptime(d.get("timestamp", "")[:19], "%Y-%m-%dT%H:%M:%S"))
                except ValueError:
                    continue
                if d.get("subtype") == "compact_boundary":
                    bounds.append((tsec, int((d.get("compactMetadata") or {}).get("preTokens") or 0), len(reqs)))
                    continue
                m = d.get("message") or {}
                u = m.get("usage") or {}
                if d.get("isSidechain") or not _usage_tokens(u):
                    continue
                paths = {(b.get("input") or {}).get("file_path") for b in m.get("content") or []
                         if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Read"} - {None} if isinstance(m.get("content"), list) else set()
                if reqs and m.get("id") and reqs[-1]["id"] == m["id"]:
                    reqs[-1]["paths"] |= paths     # one request, written as several lines
                    continue
                reqs.append({"t": tsec, "id": m.get("id"), "ctx": _usage_tokens(u), "write": int(u.get("cache_creation_input_tokens", 0) or 0), "paths": paths})
        for i, (t, pre, n) in enumerate(bounds):
            end = bounds[i + 1][2] if i + 1 < len(bounds) else None
            seg = reqs[n:end]
            e = next((e for e in reversed(es) if t - 900 <= e["t"] <= t + 5), None)   # the hook that announced it
            if not seg or e is None:
                continue      # nothing ran after it, or it was before token saving
            before = set().union(*[r["paths"] for r in reqs[:n]])
            out.append({"t": int(t), "session": e.get("session", ""), "window": int(e.get("window") or 0), "ctx": pre, "post": seg[0]["ctx"],
                        "write": seg[0]["write"], "requests": len(seg), "open": end is None,
                        "reread": sum(r["ctx"] for r in seg[:40] if r["paths"] & before)})
    return out


def _median(v: List[float]) -> float:
    v = sorted(v)
    return v[len(v) // 2] if v else 0


def compaction_cost(st: List[Dict[str, Any]], window: int) -> Dict[str, int]:
    """What a compaction costs besides the reading `whatif` counts, in cache reads: writing the summary, putting the new
    context in the cache, and reading again what the summary dropped (2 x window until measured). Only compactions at about this
    window count: how much is left right after one depends on it."""
    st = [e for e in st if abs(e["window"] - window) <= 50000]
    m = len(st) >= 3
    post, write = (int(_median([e["post"] for e in st])), int(_median([e["write"] for e in st]))) if m else (POST, POST_WRITE)
    reread = int(sum(e["reread"] for e in st) / len(st)) if m else None
    return {"post": post, "write": write, "reread": reread if reread is not None else 2 * window, "measured": len(st) if m else 0,
            "cost": SUMMARY_OUT * W_OUT + write * W_WRITE + (reread if reread is not None else 2 * window)}


def window_floor(t: Dict[str, Any], post: int) -> int:
    """The smallest window worth having: below it a session compacts again a few steps after a compaction."""
    return max(int(t["min"]), -(-(post + RESERVE + 70000) // 10000) * 10000)


def tokens_autotune(cfg: Dict[str, Any], apply: bool = True) -> Dict[str, Any]:
    """Pick the window that costs the least (within 3%, the one that compacts least): replay the last 7 days of requests at
    each window and charge every compaction what compactions measurably cost (compaction_cost). Never under window_floor; at most 50k a day otherwise; up when
    sessions compact again within a few steps."""
    t = tokens_cfg(cfg)
    reqs, st = _today_requests(7), compaction_stats(7)
    cur = int(t["window"])
    cc = compaction_cost(st, cur)
    lo = window_floor(t, cc["post"])
    hi = max(lo, int(t["max"]))
    rows = []
    for w in range(lo, hi + 1, 10000):
        tot, comps = whatif(reqs, w, cc["post"])
        rows.append((tot + comps * cc["cost"], w, tot, comps))
    best = max(r[1] for r in rows if r[0] <= 1.03 * min(rows)[0])   # about as cheap as the cheapest, with the fewest compactions
    gaps = [e["requests"] for e in st if e["window"] == cur and not e["open"]]
    thrash = len(gaps) >= 3 and _median(gaps) < 25
    want = max(best, cur + 50000) if thrash else best
    new = int(round(max(lo, min(hi, max(cur - 50000, min(cur + 50000, want)))) / 10000) * 10000)
    rec = {"t": int(now()), "from": cur, "to": new, "best": best, "floor": lo, "post": cc["post"], "cost": cc["cost"], "measured": cc["measured"],
           "gap": _median(gaps) if gaps else None, "rows": [{"window": w, "cost": int(x), "tokens": int(tot), "compactions": c} for x, w, tot, c in rows]}
    if apply:
        _log_tokens("autotune.jsonl", rec)
        cfg.setdefault("tokens", {})["last_tune"] = int(now())
        if new != cur:
            cfg["tokens"]["window"] = new
        save_cfg(cfg)
        if new != cur:
            tokens_apply(cfg)
    rec["changed"] = new != cur
    return rec


def tokens_apply(cfg: Dict[str, Any], names: Optional[List[str]] = None) -> Dict[str, str]:
    """Put the token saving setting on this machine (Claude Code user settings) and on every node (their settings + job rules)."""
    t, res = tokens_cfg(cfg), {}
    ok = claude_env(WINDOW_ENV, int(t["window"]) if t["on"] else None)
    res["(this machine)"] = ("on " if t["on"] else "off ") + ("" if ok else f"— {CLAUDE_SETTINGS} is not valid JSON, left as is")
    for name in names or [n for n, v in cfg["nodes"].items() if not v.get("pending")]:
        n = Node(name, cfg)
        if n.windows:
            res[name] = "skipped (Windows)"
            continue
        r = n.ctl("tokens", "on" if t["on"] else "off", str(int(t["window"])), timeout=60)
        res[name] = (r.stdout.decode().strip().splitlines() or ["?"])[-1] if r.returncode == 0 else f"failed: {r.stderr.decode().strip()[-120:]}"
    return res


def cmd_tokens(a) -> None:
    cfg = load_cfg()
    t = cfg.setdefault("tokens", {})
    changed = False
    if a.window:
        w = parse_tokens(a.window)
        if w < 100000:
            die(E_USAGE, "the window must be at least 100k (Claude Code's minimum)")
        t["window"], changed = w, True
    if a.fork_max:
        t["fork_max"], changed = parse_tokens(a.fork_max), True
    if a.auto is not None:
        t["auto"], changed = a.auto, True
    if a.action in ("on", "off"):
        t["on"], changed = a.action == "on", True
    if a.action == "tune":
        rec = tokens_autotune(cfg)
        say(f"window {rec['from'] // 1000}k -> {rec['to'] // 1000}k (best {rec['best'] // 1000}k, never under {rec['floor'] // 1000}k; the context right after"
            f" a compaction is {rec['post'] // 1000}k and one costs {rec['cost'] / 1e6:.1f}M cache reads, "
            + (f"measured over {rec['measured']})" if rec["measured"] else "assumed until measured)"))
        return
    if changed:
        save_cfg(cfg)
        for k, v in tokens_apply(cfg).items():
            say(f"{k}: {v}")
        if a.action == "on":
            say("running sessions take the window at once; new hooks come with a new session, a restart or /reload-plugins")
    tc = tokens_cfg(cfg)
    say(f"token saving: {'on' if tc['on'] else 'off'} · auto-compact at {int(tc['window']) // 1000}k"
        f" · forks refused above {int(tc['fork_max']) // 1000}k · auto-tune {'on' if tc['auto'] else 'off'}"
        f" ({int(tc['min']) // 1000}k..{int(tc['max']) // 1000}k)")
    if a.action in ("on", "off") or changed:
        if tc["on"] and int(tc["window"]) < int(tc["min"]):
            say(f"! under {int(tc['min']) // 1000}k sessions compact again after a few steps: the context right after a compaction is about"
                f" {POST // 1000}k, and Claude Code compacts {RESERVE // 1000}k below the window")
        return
    reqs, st = _today_requests(1), compaction_stats(7)
    cc = compaction_cost(st, int(tc["window"]))
    if tc["on"] and int(tc["window"]) < window_floor(tc, cc["post"]):
        say(f"! {int(tc['window']) // 1000}k is too small here: Claude Code compacts at about {(int(tc['window']) - RESERVE) // 1000}k and the context right after"
            f" a compaction is already about {cc['post'] // 1000}k — use {window_floor(tc, cc['post']) // 1000}k or more")
    allc = [c for v in reqs.values() for _, c in v]
    if allc:
        big = sum(c for c in allc if c > 200000)
        say(f"last 24 h here: {len(allc):,} requests · {sum(allc) / 1e6:,.0f}M context read · average {sum(allc) / len(allc) / 1e3:.0f}k"
            f" · {big / max(sum(allc), 1) * 100:.0f}% of it in requests above 200k")
        say(f"{'window':>8} {'tokens':>9} {'saved':>6} {'compactions':>12}")
        for w in (150000, 200000, 250000, 300000, 400000):
            tot, comps = whatif(reqs, w, cc["post"])
            say(f"{w // 1000:>7}k {tot / 1e6:>8,.0f}M {100 - tot / max(sum(allc), 1) * 100:>5.0f}% {comps:>12}")
    gaps = [e["requests"] for e in st if not e["open"]]
    say(f"compactions measured (7 d): {len(st)}" + (f" · context right after one {cc['post'] // 1000}k · {_median(gaps)} requests until the next (median)" if gaps else ""))
    if (TOKENS_DIR / "autotune.jsonl").exists():
        last = json.loads((TOKENS_DIR / "autotune.jsonl").read_text().splitlines()[-1])
        say(f"last auto-tune {time.strftime('%m-%d %H:%M', time.localtime(last['t']))}: {last['from'] // 1000}k -> {last['to'] // 1000}k")


# ── commands: Claude Code integration ────────────────────────────────────────
SHARE = HOME / ".local" / "share" / "agent-scaleout"
RULES_BEGIN, RULES_END = "<!-- agent-scaleout: offload rules (scaleout claude-setup) -->", "<!-- /agent-scaleout -->"


def cmd_hook(a) -> None:
    """`scaleout hook pre|post|session|notes|ledger|…`: what the Claude Code plugin's hooks run. The logic lives in the installed package
    (agents/claude), so a scaleout update updates the hooks too."""
    base = agents_dir()
    if a.event == "pre":
        script = base / "offload-hook.sh"
        if not script.exists():
            sys.exit(0)   # never get in the way of a command
        os.execv("/bin/bash", ["bash", str(script)])
    if a.event == "post":      # close the record of a heavy command; then the same nudge as `ctx` (a session older than that hook has only this one)
        raw, script = sys.stdin.read(), base / "post-hook.sh"
        try:
            if script.exists():
                subprocess.run(["/bin/bash", str(script)], input=raw.encode(), timeout=8, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            hook_tokens("ctx", json.loads(raw or "{}"))
        except Exception:
            pass
        sys.exit(0)
    if a.event in ("precompact", "notes", "ledger", "ctx", "agent"):
        try:
            hook_tokens(a.event)
        except Exception:   # a hook must never break the session
            pass
        sys.exit(0)
    if a.event == "session":   # the offload rules (+ the token saving rules) for every new session here; notes and ledger are hooks of their own (HOOK_MAX)
        if not CONFIG.exists():
            sys.exit(0)   # not an orchestrator: nothing to say
        parts = [(base / "scaleout-rules.md").read_text()] if (base / "scaleout-rules.md").exists() else []
        try:
            t = tokens_cfg(load_cfg())
            if t["on"]:
                h = _hook_input()
                project = _project_of(h)
                keep_out_of_git(project)
                parts.append(token_rules(t, project, h.get("session_id", "")))
                if h.get("source") == "compact":   # a session older than the notes and ledger hooks has only this one
                    files = [str(f) for f in (_session_file(h, 0), fresh_ledger(h)) if f]
                    if files:
                        parts.append("## After this compaction\nYour notes and the ledger come back with it. If you do not see their text in this context"
                                     " (a session started before scaleout 0.9.8), read them now: " + ", ".join(files))
        except Exception:
            pass
        rules = "\n\n".join(p for p in parts if p)
        if rules:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": rules}}, ensure_ascii=False))


def _manual_setup_remove() -> List[str]:
    """Take out what earlier versions put in by hand: the rules import in ~/.claude/CLAUDE.md, the hooks in settings.json
    and ~/.claude/agents/scaleout.md (the plugin carries all three now)."""
    claude, done = HOME / ".claude", []
    md = claude / "CLAUDE.md"
    if md.exists():
        text = md.read_text()
        new = re.sub(re.escape(RULES_BEGIN) + r".*?" + re.escape(RULES_END) + r"\n?", "", text, flags=re.S)
        if new != text:
            md.write_text(new.rstrip("\n") + "\n" if new.strip() else ""); done.append(f"rules import in {md}")
    sp = claude / "settings.json"
    if sp.exists():
        cfg = json.loads(sp.read_text())
        hooks, n = cfg.get("hooks") or {}, 0
        for event in list(hooks):
            keep = [e for e in hooks[event] if not any(x in h.get("command", "") for h in e.get("hooks", [])
                                                      for x in ("offload-hook.sh", "post-hook.sh", "scaleout hook"))]
            n += len(hooks[event]) - len(keep)
            if keep:
                hooks[event] = keep
            else:
                hooks.pop(event)
        if n:
            shutil.copy(sp, sp.with_suffix(".json.scaleout-bak"))
            if hooks:
                cfg["hooks"] = hooks
            else:
                cfg.pop("hooks", None)
            sp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n"); done.append(f"{n} hook(s) in {sp}")
    ag = claude / "agents" / "scaleout.md"
    if ag.exists():
        ag.unlink(); done.append(str(ag))
    return done


def cmd_claude_setup(a) -> None:
    """Every Claude Code session on this machine gets scaleout through its plugin: the offload hook (heavy work goes to the
    pool while a node has room), the rules as session context, the relay subagent and /scaleout:status · top · usage.
    The plugin is read from the installed package (~/.local/share/agent-scaleout), so `scaleout update` keeps it current."""
    claude_bin = shutil.which("claude") or str(HOME / ".local" / "bin" / "claude")
    market = SHARE if (SHARE / ".claude-plugin" / "marketplace.json").exists() else PKG_ROOT
    for x in _manual_setup_remove():
        say(f"removed the old manual setup: {x}")
    if a.remove:
        r = run([claude_bin, "plugin", "uninstall", "scaleout@agent-scaleout"], timeout=120)
        say("plugin removed" if r.returncode == 0 else f"plugin: {(r.stderr or r.stdout).decode().strip()[-200:]}")
        return
    if not Path(claude_bin).exists():
        die(E_USAGE, "Claude Code not found (install it, then run scaleout claude-setup again)")
    listed = run([claude_bin, "plugin", "marketplace", "list"], timeout=120).stdout.decode()
    if "agent-scaleout" not in listed:
        r = run([claude_bin, "plugin", "marketplace", "add", str(market)], timeout=300)
        if r.returncode != 0:
            die(E_USAGE, f"could not add the plugin marketplace {market}: {(r.stderr or r.stdout).decode().strip()[-300:]}")
    else:
        run([claude_bin, "plugin", "marketplace", "update", "agent-scaleout"], timeout=300)
    plugins = run([claude_bin, "plugin", "list"], timeout=120).stdout.decode()
    if "scaleout@agent-scaleout" in plugins:
        r = run([claude_bin, "plugin", "update", "scaleout@agent-scaleout"], timeout=300)
    else:
        r = run([claude_bin, "plugin", "install", "scaleout@agent-scaleout"], timeout=300)
    if r.returncode != 0:
        die(E_USAGE, f"plugin install failed: {(r.stderr or r.stdout).decode().strip()[-300:]}")
    say(f"Claude Code plugin scaleout@agent-scaleout installed from {market}: offload hook, rules for every new session,"
        " the scaleout subagent, /scaleout:status · top · usage")
    conf = CONF_DIR / "hook.conf"
    mode = re.search(r"^OFFLOAD=(\w+)", conf.read_text(), re.M).group(1) if conf.exists() and re.search(r"^OFFLOAD=", conf.read_text(), re.M) else "always"
    say(f"offload: {mode} (heavy builds and tests go to the pool while a node has room; set OFFLOAD=pressure or off in {conf})")
    say("running sessions take the hook at once; the rules reach sessions started from now on")


# ── commands: usage + top ────────────────────────────────────────────────────
def tokens_of(u: Dict[str, int]) -> int:
    return int(u.get("in", 0)) + int(u.get("out", 0)) + int(u.get("cache_write", 0)) + int(u.get("cache_read", 0))


def fmt_tok(n: float) -> str:
    return f"{n / 1e9:.1f}B" if n >= 1e9 else f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k" if n >= 1e3 else f"{int(n)}"


class LocalUsage:
    """Claude Code tokens of every session on this machine, read incrementally from ~/.claude/projects/*/*.jsonl
    (only appended bytes are read; hourly buckets for 7 days are cached in the state folder)."""
    CACHE = STATE_DIR / "usage-cache.json"

    def __init__(self) -> None:
        c = read_json(self.CACHE, {})
        self.offsets: Dict[str, int] = c.get("offsets", {})
        self.hours: Dict[str, Dict[str, int]] = c.get("hours", {})
        self.seen: Dict[str, List[str]] = {}
        self.last_save = 0.0

    def refresh(self) -> None:
        root = HOME / ".claude" / "projects"
        cutoff = now() - 7 * 86400
        for f in list(root.glob("*/*.jsonl")) + list(root.glob("*/*/subagents/*.jsonl")):   # sessions and their subagents
            try:
                st = f.stat()
            except OSError:
                continue
            if st.st_mtime < cutoff:
                continue
            key = str(f.resolve())
            off = self.offsets.get(key, 0)
            if st.st_size < off:
                off = 0
            if st.st_size == off:
                continue
            seen = set(self.seen.get(key, []))
            with open(f, "rb") as fh:
                fh.seek(off)
                data = fh.read()
            end = data.rfind(b"\n") + 1      # keep a half-written last line for next time
            for line in data[:end].splitlines():
                if b'"usage"' not in line:
                    continue
                try:
                    r = json.loads(line)
                    m = r.get("message") or {}
                    u = m.get("usage") or {}
                    mid = m.get("id") or r.get("requestId") or ""
                    if not u or (mid and mid in seen):
                        continue
                    seen.add(mid)
                    ts = r.get("timestamp", "")
                    t = time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone if ts else now()
                except (ValueError, TypeError, OverflowError):
                    continue
                h = str(int(t // 3600 * 3600))
                b = self.hours.setdefault(h, {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0, "msgs": 0})
                b["in"] += int(u.get("input_tokens", 0)); b["out"] += int(u.get("output_tokens", 0))
                b["cache_read"] += int(u.get("cache_read_input_tokens", 0)); b["cache_write"] += int(u.get("cache_creation_input_tokens", 0))
                b["msgs"] += 1
            self.offsets[key] = off + end
            self.seen[key] = list(seen)[-2000:]
        old = str(int(cutoff // 3600 * 3600))
        self.hours = {h: v for h, v in self.hours.items() if h >= old}
        if now() - self.last_save > 60:
            write_json(self.CACHE, {"offsets": self.offsets, "hours": self.hours})
            self.last_save = now()

    def window(self, seconds: int) -> Dict[str, int]:
        since = now() - seconds
        tot = {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0, "msgs": 0}
        for h, v in self.hours.items():
            if int(h) + 3600 > since:
                for k in tot:
                    tot[k] += v.get(k, 0)
        return tot


_TITLES: Dict[str, Tuple[int, str]] = {}


def session_title(f: Path) -> str:
    """A session's name (/rename → the last "customTitle" in its transcript), read incrementally."""
    key = str(f)
    try:
        size = f.stat().st_size
    except OSError:
        return ""
    off, title = _TITLES.get(key, (0, ""))
    if size < off:
        off, title = 0, ""
    if size > off:
        with open(f, "rb") as fh:
            fh.seek(off)
            data = fh.read()
        end = data.rfind(b"\n") + 1
        for line in data[:end].splitlines():
            if b'"customTitle"' in line:
                try:
                    title = str(json.loads(line).get("customTitle") or title)
                except ValueError:
                    pass
        _TITLES[key] = (off + end, title)
    return title


def claude_now(active_s: int = 180) -> Dict[str, Any]:
    """Claude Code on this machine right now: sessions that wrote in the last `active_s` seconds or have subagents
    running, each with its running subagents (from <session>/subagents/*.meta.json), plus the claude processes' CPU and
    memory. Subagents run inside their session's process (the model works on Anthropic's servers), so they are not
    scaleout jobs; heavy commands they run still meet the offload hook."""
    root, t = HOME / ".claude" / "projects", now()
    sessions, seen = [], set()
    for link in root.glob("*/*.jsonl"):
        try:
            f = link.resolve()            # the same session may be linked from another project folder
            mt = f.stat().st_mtime
        except OSError:
            continue                      # a link whose session is gone
        if f in seen:
            continue
        seen.add(f)
        if t - mt > 86400:
            continue
        subs = []
        for m in f.with_suffix("").glob("subagents/*.meta.json"):
            meta = read_json(m, {})
            if meta.get("finished"):
                continue
            try:
                smt = (m.parent / (m.name[:-len(".meta.json")] + ".jsonl")).stat().st_mtime
            except OSError:
                continue
            if t - smt > 600:
                continue                  # stopped without finishing (session closed)
            subs.append({"desc": str(meta.get("description") or meta.get("agentType") or "subagent"),
                         "type": str(meta.get("agentType") or ""), "depth": int(meta.get("spawnDepth") or 1)})
        if t - mt > active_s and not subs:
            continue
        sessions.append({"id": f.stem, "name": session_title(f) or f.stem[:8], "idle": int(t - mt), "subs": subs})
    sessions.sort(key=lambda x: (-len(x["subs"]), x["idle"]))
    procs = cpu = rss = 0.0
    r = run(["ps", "-axo", "pcpu=,rss=,comm="])
    for line in r.stdout.decode(errors="replace").splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[2].rsplit("/", 1)[-1] == "claude":
            procs += 1
            cpu += float(parts[0])
            rss += float(parts[1])
    return {"sessions": sessions, "subagents": sum(len(x["subs"]) for x in sessions),
            "procs": int(procs), "cpu_pct": round(cpu), "mem_gb": round(rss / 1048576, 1)}


def cols(text: str) -> int:
    """Terminal columns: Korean and other wide characters take two."""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def fit(text: str, width: int) -> str:
    """Cut to `width` terminal columns and pad with spaces."""
    out, n = "", 0
    for c in text:
        w = cols(c)
        if n + w > width:
            break
        out, n = out + c, n + w
    return out + " " * (width - n)


def claude_now_lines(cn: Dict[str, Any], width: int = 6) -> List[str]:
    working = sum(1 for x in cn["sessions"] if x["idle"] < 180)
    L = [f"CLAUDE NOW  sessions {working} working · subagents {cn['subagents']} · {cn['procs']} claude processes"
         f" · CPU {cn['cpu_pct']}% · mem {cn['mem_gb']}G"]
    for x in cn["sessions"][:width]:
        state = "now" if x["idle"] < 60 else f"{x['idle'] // 60}m ago"
        line = f"  {fit(x['name'], 16)} {state:>7}"
        if x["subs"]:
            line += f"  {len(x['subs'])} subagent(s): " + " · ".join(sorted({s['desc'][:28] for s in x['subs']}))[:70]
        L.append(line)
    if len(cn["sessions"]) > width:
        L.append(f"  … {len(cn['sessions']) - width} more")
    for name, st in sorted(cn.get("nodes", {}).items()):
        c = st.get("claude") or {}
        if not st.get("running") and not c.get("subagents"):
            continue
        line = f"  {fit(name + ' (node)', 16)} jobs {st.get('running', 0)}"
        if c.get("subagents"):
            line += f"  {c['subagents']} subagent(s): " + " · ".join(c.get("descs") or [])[:70]
        L.append(line)
    return L


def report_usage(rep: Dict[str, Any]) -> Dict[str, Any]:
    """Tokens, cost, turns and subagents of a `claude -p --output-format json` answer. `modelUsage` covers the whole run
    (every model, subagents included); `usage` holds only the last turn, so it is the fallback for older Claude Code."""
    out: Dict[str, Any] = {}
    mu = rep.get("modelUsage") or {}
    u = rep.get("usage") or {}
    if mu:
        out["usage"] = {"in": sum(int(v.get("inputTokens", 0)) for v in mu.values()),
                        "out": sum(int(v.get("outputTokens", 0)) for v in mu.values()),
                        "cache_read": sum(int(v.get("cacheReadInputTokens", 0)) for v in mu.values()),
                        "cache_write": sum(int(v.get("cacheCreationInputTokens", 0)) for v in mu.values())}
    elif u:
        out["usage"] = {"in": int(u.get("input_tokens", 0)), "out": int(u.get("output_tokens", 0)),
                        "cache_read": int(u.get("cache_read_input_tokens", 0)),
                        "cache_write": int(u.get("cache_creation_input_tokens", 0))}
    if out:
        out["cost_usd"] = float(rep.get("total_cost_usd") or 0)
        out["turns"] = int(rep.get("num_turns") or 0)
        out["subagents"] = int((rep.get("subagent_stats") or {}).get("spawned") or 0)
    return out


def job_usage(seconds: int) -> Dict[str, Dict[str, float]]:
    """Tokens / cost / jobs / busy time per node, from finished jobs."""
    since = now() - seconds
    per: Dict[str, Dict[str, float]] = {}
    for j in all_jobs():
        if j.get("bench") or not j.get("node") or j.get("finished_at", 0) < since:
            continue
        p = per.setdefault(j["node"], {"jobs": 0, "tokens": 0, "cost": 0.0, "busy_s": 0, "failed": 0, "extra": 0, "measured": 0})
        if j.get("start_tokens"):
            p["extra"] += int(j["start_tokens"]); p["measured"] += 1
        p["jobs"] += 1
        p["failed"] += j.get("status") == "failed"
        p["tokens"] += tokens_of(j.get("usage") or {})
        p["cost"] += float(j.get("cost_usd") or 0)
        if j.get("dispatched_at"):
            p["busy_s"] += max(0, j["finished_at"] - j["dispatched_at"])
    return per


def local_metrics() -> Dict[str, Any]:
    m: Dict[str, Any] = {"cpus": os.cpu_count() or 1, "load": os.getloadavg()[0]}
    try:
        du = shutil.disk_usage(str(HOME))
        m["disk_free_gb"] = du.free // (1 << 30)
    except OSError:
        pass
    if platform.system() == "Darwin":
        r = run(["sysctl", "-n", "kern.memorystatus_level", "hw.memsize", "vm.swapusage"], timeout=5).stdout.decode().split("\n")
        try:
            m["mem_free_pct"] = int(r[0]); m["mem_total_gb"] = int(r[1]) // (1 << 30)
            sw = re.search(r"used = ([\d.]+)M", r[2])
            m["swap_gb"] = round(float(sw.group(1)) / 1024, 1) if sw else 0
        except (ValueError, IndexError):
            pass
    else:
        try:
            mi = dict(l.split(":", 1) for l in open("/proc/meminfo").read().splitlines() if ":" in l)
            kb = lambda k: int(mi[k].split()[0])
            m["mem_total_gb"] = kb("MemTotal") // (1 << 20)
            m["mem_free_pct"] = kb("MemAvailable") * 100 // kb("MemTotal")
            m["swap_gb"] = round((kb("SwapTotal") - kb("SwapFree")) / (1 << 20), 1)
        except (OSError, KeyError, ValueError):
            pass
    return m


def bar(pct: float, width: int = 10) -> str:
    pct = max(0.0, min(100.0, pct))
    full = int(pct / 100 * width)
    return "█" * full + "░" * (width - full)


def merge_intervals(iv: List[Tuple[float, float]]) -> float:
    total, end = 0.0, -1.0
    for a, b in sorted(iv):
        if a > end:
            total += b - a
            end = b
        elif b > end:
            total += b - end
            end = b
    return total


def speed_of(jobs: List[Dict[str, Any]], since: float) -> Tuple[float, float, int]:
    """(hours one at a time, hours the pool was actually busy, jobs) for jobs finished since `since`."""
    iv = [(j["dispatched_at"], j["finished_at"]) for j in jobs
          if not j.get("bench") and j.get("node") and j.get("dispatched_at") and j.get("finished_at", 0) >= since]
    return sum(b - x for x, b in iv) / 3600, merge_intervals(iv) / 3600, len(iv)


def history_tail(hours: float) -> List[Dict[str, Any]]:
    """Recent 5-minute samples, reading only the end of the file."""
    if not HISTORY.exists():
        return []
    want = int(hours * 12) + 2
    with open(HISTORY, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - want * 400))
        data = f.read().decode(errors="replace").splitlines()[-want:]
    since = now() - hours * 3600
    out = []
    for line in data:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("t", 0) >= since:
            out.append(r)
    return out


def spark(vals: List[float], hi: Optional[float] = None) -> str:
    if not vals:
        return "-"
    top = hi or max(vals) or 1
    return "".join(SPARKS[min(7, int(v / top * 7))] for v in vals)


SPARKS = "▁▂▃▄▅▆▇█"


def productivity_lines(jobs: List[Dict[str, Any]]) -> List[str]:
    today = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    t1, w1, n1 = speed_of(jobs, today)
    t7, w7, n7 = speed_of(jobs, now() - 7 * 86400)
    sp = lambda t, w: f"{t / w:.1f}x" if w else "-"  # noqa: E731
    L = [f"{'PRODUCTIVITY':<30}{'today':>12}{'7 days':>12}",
         f"{'  speed from parallel jobs':<30}{sp(t1, w1):>12}{sp(t7, w7):>12}   (one at a time {t7:.1f} h → {w7:.1f} h)",
         f"{'  heavy work done by nodes':<30}{t1:>11.1f}h{t7:>11.1f}h   ({n1} / {n7} jobs)"]
    me = read_json(STATE_DIR / "bench-self.json", {})
    hist = [h for h in bench_history() if "nodes" in h]
    pool = hist[-1]["total"] if hist else 0
    L.append(f"{'  capacity':<30}" + (f"nodes {pool} + this machine {me['score']} = {(me['score'] + pool) / me['score']:.1f}x of this machine alone"
                                      if me.get("score") else f"nodes {pool}  (scaleout bench --self to compare)"))
    rows = history_tail(6)
    if rows:
        ld = [float(r["orch"].get("load") or 0) for r in rows]
        sw = [float(r["orch"].get("swap_gb") or 0) for r in rows]
        mf = [100 - int(r["orch"].get("mem_free_pct") or 0) for r in rows]
        cp = int(rows[-1]["orch"].get("cpus") or 8)
        ld, sw, mf = [v[::-5][::-1] for v in (ld, sw, mf)]   # a point per 25 minutes, the newest kept
        L.append(f"{'  this machine, last 6 h':<26}load {spark(ld, max(max(ld), cp * 2))} {ld[-1]:.0f}/{cp}"
                 f"   swap {spark(sw, max(max(sw), 1))} {sw[-1]:.0f}G   mem used {spark(mf, 100)} {mf[-1]}%")
    else:
        L.append(f"{'  this machine, last 6 h':<30}(history starts now, every 5 min)")
    return L


def cmd_report(a) -> None:
    cfg = load_cfg()
    since = now() - a.days * 86400
    jobs = [j for j in all_jobs() if not j.get("bench") and j.get("node") and j.get("dispatched_at") and j.get("finished_at", 0) >= since]
    say(f"PRODUCTIVITY · last {a.days} day(s) · {time.strftime('%F %H:%M')}")
    # 1. speed: the same work one at a time vs. what the pool actually took
    iv = [(j["dispatched_at"], j["finished_at"]) for j in jobs]
    serial, wall = sum(b - x for x, b in iv), merge_intervals(iv)
    if iv:
        say(f"\nSPEED   {len(jobs)} jobs: one at a time {serial / 3600:.1f} h · actually busy {wall / 3600:.1f} h"
            f" → {serial / wall if wall else 1:.1f}x faster from running in parallel")
    else:
        say("\nSPEED   no finished jobs in this period yet")
    # 2. capacity
    me = read_json(STATE_DIR / "bench-self.json", {})
    hist = [h for h in bench_history() if "nodes" in h]
    pool = hist[-1]["total"] if hist else 0
    loads = [h["load"] for h in bench_history() if "load" in h]
    line = f"CAPACITY  nodes score {pool}"
    if me.get("score"):
        line += f" · this machine alone {me['score']} → {pool / me['score'] * 100:+.0f}% room for heavy work ({(me['score'] + pool) / me['score']:.1f}x)"
    else:
        line += "  (run `scaleout bench --self` once to compare with this machine)"
    say(line + (f" · last load test {loads[-1]['score']}" if loads else ""))
    # 3. work moved off this machine
    say(f"\n{'NODE':<14}{'jobs':>6}{'failed':>8}{'busy h':>8}{'max ||':>8}{'avg run':>9}{'avg wait':>10}{'tokens':>9}{'$':>8}")
    per: Dict[str, List[Dict[str, Any]]] = {}
    for j in jobs:
        per.setdefault(j["node"], []).append(j)
    for n in sorted(per):
        js = per[n]
        ev: List[Tuple[float, int]] = []
        for j in js:
            ev += [(j["dispatched_at"], 1), (j["finished_at"], -1)]
        c = m = 0
        for _, dlt in sorted(ev):
            c += dlt
            m = max(m, c)
        runs = [j["finished_at"] - j["dispatched_at"] for j in js]
        waits = [j["dispatched_at"] - j["created"] for j in js if j.get("created")]
        say(f"{n:<14}{len(js):>6}{sum(j['status'] == 'failed' for j in js):>8}{sum(runs) / 3600:>8.1f}{m:>8}"
            f"{sum(runs) / len(runs) / 60:>8.1f}m{(sum(waits) / len(waits) / 60 if waits else 0):>9.1f}m"
            f"{fmt_tok(sum(tokens_of(j.get('usage') or {}) for j in js)):>9}{sum(float(j.get('cost_usd') or 0) for j in js):>8.2f}")
    # 4. per job kind
    kinds: Dict[Tuple[str, str], List[float]] = {}
    for j in jobs:
        kinds.setdefault((job_kind(j), j["node"]), []).append(j["finished_at"] - j["dispatched_at"])
    if kinds:
        say("\nBY JOB KIND (avg run)  " + " · ".join(f"{k} on {n}: {sum(v) / len(v) / 60:.1f}m ({len(v)})" for (k, n), v in sorted(kinds.items())))
    # 5. this machine's health, per day (5-minute samples)
    rows = []
    if HISTORY.exists():
        for line in HISTORY.read_text().splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("t", 0) >= since:
                rows.append(r)
    if rows:
        days: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            days.setdefault(time.strftime("%m-%d", time.localtime(r["t"])), []).append(r)
        say(f"\n{'THIS MACHINE':<14}{'avg load':>10}{'peak':>7}{'avg swap':>10}{'min mem free':>14}{'node jobs':>11}")
        for dname in sorted(days):
            rs = days[dname]
            ld = [float(r["orch"].get("load") or 0) for r in rs]
            sw = [float(r["orch"].get("swap_gb") or 0) for r in rs]
            mf = [int(r["orch"].get("mem_free_pct") or 0) for r in rs]
            nj = [sum(int((v or {}).get("running") or 0) for v in r.get("nodes", {}).values()) for r in rs]
            say(f"{dname:<14}{sum(ld) / len(ld):>10.1f}{max(ld):>7.1f}{sum(sw) / len(sw):>9.1f}G{min(mf):>13}%{sum(nj) / len(nj):>11.1f}")
        added = sorted((v.get("added", ""), n) for n, v in cfg["nodes"].items() if v.get("added"))
        if added:
            say("  nodes joined: " + ", ".join(f"{n} {t[:10]}" for t, n in added) + " — compare the days before and after")
    else:
        say("\nTHIS MACHINE  history starts now (sampled every 5 minutes by the daemon)")


def cmd_usage(a) -> None:
    cfg = load_cfg()
    lu = LocalUsage()
    lu.refresh()
    lu.last_save = 0
    lu.refresh()
    collect_usage(cfg, max_age=0 if a.refresh else 600)
    width = max(60, min(shutil.get_terminal_size((110, 40)).columns, 118))
    print("\n".join(box("CLAUDE TOKENS", tokens_lines(cfg, lu, weeks=a.weeks or max(8, min(26, (width - 10) // 2))), width,
                        "every session on each machine")))


def machine_gates(cfg: Dict[str, Any], name: str, st: Dict[str, Any], busy: int) -> Tuple[int, Dict[str, float], str, float]:
    """(cap, how full each gate is, the fullest gate, its fullness 0..1): jobs / cap, load / (2x cores), memory in use."""
    cap = job_cap(name, cfg["nodes"].get(name, {}), st)
    cores = max(int(st.get("cpus", 1) or 1), 1)
    gates = {"jobs": busy / max(cap, 1),
             "cpu": float(st.get("load", 0) or 0) / (float(cfg["defaults"].get("max_load_per_cpu", 2) or 2) * cores),
             "memory": 1 - float(st.get("mem_free_pct", 0) or 0) / 100}
    neck = max(gates, key=gates.get)
    return cap, gates, neck, min(1.0, gates[neck])


WAIT_WORDS = (("memory", "memory"), ("cpu load", "load "), ("job slots", "job(s) running"), ("needs a Mac", "lacks mac"),
              ("needs a Mac", "lacks xcode"), ("needs a phone", "phone"), ("agent limit", "usage limit"),
              ("signed out", "signed out"), ("offline", "offline"), ("disk", "disk"))


def wait_reasons(text: str) -> List[str]:
    low = (text or "").lower()
    return sorted({key for key, word in WAIT_WORDS if word in low})


def efficiency_lines(cfg: Dict[str, Any], jobs: List[Dict[str, Any]], nw: int) -> List[str]:
    """Live efficiency: per machine how full it is on each gate (jobs / cap, load / 2x cores, memory) and which one binds;
    its benchmark score scaled by the room it has left (live capacity); the pool's use, why queued jobs wait, how long
    they waited today and how much running in parallel saved."""
    d = cfg["defaults"]
    bench = (bench_history() or [{}])[-1].get("nodes", {})
    running: Dict[str, int] = {}
    for j in jobs:
        if j.get("status") in ("dispatched", "running") and j.get("node"):
            running[j["node"]] = running.get(j["node"], 0) + 1
    L = [c(f"{'MACHINE':<{nw}}{'JOBS':>9}{'LOAD':>8}{'MEM':>7}{'STRAIN':>16}  {'BOTTLENECK':<11}{'SCORE':>7}{'LIVE':>7}", C_DIM)]
    tot_score = live_score = 0.0
    for name in sorted(cfg["nodes"], key=lambda x: (not cfg["nodes"][x].get("local"), x)):
        n = cfg["nodes"][name]
        st = read_json(node_state_path(name), {})
        if not st.get("online"):
            L.append(pad(label_of(cfg, name), nw) + c("offline", C_BAD)); continue
        busy = max(running.get(name, 0), int(st.get("running", 0) or 0))
        cores = max(int(st.get("cpus", 1) or 1), 1)
        cap, gates, neck, strain = machine_gates(cfg, name, st, busy)
        score = float((bench.get(name) or {}).get("score") or 0)
        live = score * (1 - strain)
        tot_score += score; live_score += live
        col = C_OK if strain < 0.6 else C_WARN if strain < 0.9 else C_BAD
        L.append(pad(c(label_of(cfg, name), C_TEXT, bold=True), nw) + f"{f'{busy}/{cap}':>9}"
                 + f"{float(st.get('load', 0) or 0) / cores:>7.1f}×" + f"{gates['memory'] * 100:>6.0f}%"
                 + "  " + meter(strain * 100, 8, col) + c(f"{strain * 100:>5.0f}%", col)
                 + "  " + pad(c(neck, col if strain >= 0.6 else C_DIM), 11)
                 + c(f"{score:>7.0f}" if score else f"{'-':>7}", C_DIM) + c(f"{live:>7.0f}" if score else f"{'-':>7}", col))
    waiting = [j for j in jobs if j.get("status") == "queued" and not j.get("bench")]
    reasons: Dict[str, int] = {}
    for j in waiting:
        for key in wait_reasons(j.get("wait") or ""):
            reasons[key] = reasons.get(key, 0) + 1
    today = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    started = [j for j in jobs if (j.get("dispatched_at") or 0) >= today and not j.get("bench") and j.get("created")]
    waits = sorted(j["dispatched_at"] - j["created"] for j in started)
    iv = [(j["dispatched_at"], j["finished_at"]) for j in jobs if (j.get("finished_at") or 0) >= today and j.get("dispatched_at") and not j.get("bench")]
    serial, wall = sum(b - x for x, b in iv), merge_intervals(iv)
    use = (1 - live_score / tot_score) * 100 if tot_score else 0
    L.append("")
    L.append(f"  pool in use {c(f'{use:.0f}%', C_ACCENT, bold=True)} · live capacity {c(f'{live_score:.0f}', C_ACCENT, bold=True)} of {tot_score:.0f}"
             + c("  (live = bench score × room left)", C_DIM))
    neck_txt = ", ".join(f"{k} {v * 100 // max(len(waiting), 1)}%" for k, v in sorted(reasons.items(), key=lambda x: -x[1])) or "-"
    line = f"  waiting {len(waiting)}" + (c(f" · held by {neck_txt}", C_WARN) if waiting else "")
    if waits:
        line += f" · wait before start today: median {fmt_dur(waits[len(waits) // 2])}, max {fmt_dur(waits[-1])}"
    line += f" · parallel today {serial / wall:.1f}x" if wall else " · no finished jobs today"
    L.append(line)
    L.append(c("  7-day trends, bottlenecks and suggestions: scaleout insights", C_DIM))
    return L


def fmt_dur(sec: float) -> str:
    sec = int(max(0, sec))
    return f"{sec // 3600}h{sec % 3600 // 60:02d}m" if sec >= 3600 else f"{sec // 60}m{sec % 60:02d}s" if sec >= 60 else f"{sec}s"


JOB_MARKS = {"done": ("✔", C_OK), "failed": ("✖", C_BAD), "cancelled": ("⊘", C_DIM), "running": ("●", C_ACCENT), "lost": ("?", C_WARN),
             "dispatched": ("●", C_ACCENT), "queued": ("◌", C_WARN)}


def top_lines(cfg: Dict[str, Any], lu: "LocalUsage", interval: int, width: int = 110) -> List[str]:
    """The dashboard as panels. Reads only what the daemon has already gathered (no SSH), so it is cheap to redraw."""
    width = max(72, min(width, 118))
    L: List[str] = []
    L.append(c(" ◆ agent-scaleout", C_VIOLET, bold=True) + c(f" {VERSION}", C_DIM) + "   " + c(time.strftime("%H:%M:%S"), C_TEXT, bold=True)
             + c(f"   refresh {interval}s · q quit", C_DIM))
    # machines
    me = local_metrics()
    local = {n for n, v in cfg["nodes"].items() if v.get("local")}
    nw = name_w(cfg)
    rows = [c(f"{'MACHINE':<{nw}}{'ROLE':<14}{'OS':<12}{'STATE':<9}{'MEM free':<14}{'SWAP':>6}{'DISK':>6}{'LOAD/CPU':>13}{'JOBS':>6}  DEVICES", C_DIM)]

    def machine(name: str, role: str, os_s: str, h: str, st: Dict[str, Any], swap: str) -> str:
        free = float(st.get("mem_free_pct") or 0)
        cpus = int(st.get("cpus") or 1)
        load = float(st.get("load") or 0)
        lc = C_OK if load < cpus else C_WARN if load < cpus * 2 else C_BAD
        dot = c("●", C_OK if h == "ok" else C_BAD if h.startswith(("off", "down", "unreach")) else C_WARN)
        return (pad(c(name, C_TEXT, bold=True), nw) + pad(c(role, C_VIOLET if role == "orchestrator" else C_DIM), 14) + pad(os_s, 12)
                + pad(dot + " " + c(h, C_OK if h == "ok" else C_WARN), 9)
                + meter(free, 8, level_color(free)) + f"{str(int(free)) + '%':>5} " + f"{swap:>6}" + f"{str(st.get('disk_free_gb', '?')) + 'G':>6}"
                + "  " + meter(min(100, load / (cpus * 2) * 100), 4, lc) + c(f"{load:>4.1f}", lc) + c(f"/{cpus:<2}", C_DIM)
                + f"{str(st.get('running', 0)) + '+' + str(st.get('queued', 0)):>6}  {fmt_devices(st)}")

    if not local:
        rows.append(machine(label_of(cfg, LOCAL), "orchestrator", fmt_os({"os": platform.system(), "arch": platform.machine()}), "ok",
                            {**me, "running": "-", "queued": "-"}, f"{me.get('swap_gb', '-')}G"))
    for name in sorted(cfg["nodes"], key=lambda x: (x not in local, x)):
        st = read_json(node_state_path(name), {})
        h = node_health(cfg, name, st) if cfg["nodes"][name].get("pending") is not True else "pending"
        age = int(now() - st.get("ts", 0)) if st.get("ts") else None
        if age is not None and age >= 90:
            h = f"{h} {age // 60}m"
        rows.append(machine(label_of(cfg, name), role_of(cfg, name), fmt_os(st, cfg["nodes"][name]), h[:7], st,
                            f"{me.get('swap_gb', '-')}G" if name in local else "-"))
    L.extend(box("MACHINES", rows, width, f"{len(rows) - 1} in the pool"))
    try:
        L.extend(box("EFFICIENCY", efficiency_lines(cfg, all_jobs(), nw), width, "load = per core (full at 2×) · strain = the fullest gate"))
    except Exception:   # the dashboard never dies on a state file it cannot read
        pass
    # queue
    jobs = all_jobs()
    direct = direct_runs()
    every = sorted([j for j in jobs if not j.get("bench")] + direct, key=lambda j: j.get("created", 0))
    active = [j for j in every if j.get("status") in ("queued", "dispatched", "running")]
    today = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    done_today = [j for j in every if (j.get("finished_at") or 0) >= today]
    waiting, running = sum(1 for j in active if j["status"] == "queued"), sum(1 for j in active if j["status"] != "queued")
    ok, bad = sum(1 for j in done_today if j["status"] == "done"), sum(1 for j in done_today if j["status"] == "failed")
    in_s = sum(1 for j in active if j.get("direct"))
    rows = [c("◌", C_WARN) + f" waiting {c(waiting, C_WARN if waiting else C_DIM, bold=True)}   " + c("●", C_ACCENT) + f" running {c(running, C_ACCENT if running else C_DIM, bold=True)}"
            + (c(f" ({in_s} in sessions)", C_DIM) if in_s else "") + "   "
            + c("✔", C_OK) + f" finished today {c(ok, C_OK if ok else C_DIM, bold=True)}   " + c("✖", C_BAD) + f" failed today {c(bad, C_BAD if bad else C_DIM, bold=True)}"
            + c(f"   ▸ = run in a session, not through scaleout", C_DIM)]
    for j in reversed(every[-8:]):
        age = int(now() - (j.get("finished_at") or j.get("dispatched_at") or j.get("created", now())))
        ago = f"{age // 86400}d" if age >= 172800 else f"{age // 3600}h" if age >= 7200 else f"{age // 60}m"
        mark, col = JOB_MARKS.get(j.get("status", ""), ("·", C_DIM))
        if j.get("direct"):
            rows.append(c(mark, col) + " " + c("▸ ", C_VIOLET) + c(f"{'session ' + j.get('session', '')[:5]:<13}", C_DIM) + "  " + pad(label_of(cfg, j["node"]), nw + 1)
                        + pad(c(j.get("status", ""), col), 11) + c(f"{ago:>5} ago", C_DIM) + "  " + c(j.get("what", ""), C_TEXT)
                        + c(f"  {Path(j.get('cwd') or '.').name}", C_DIM))
            continue
        rows.append(c(mark, col) + " " + c(f"{j['id'][-15:]}", C_DIM) + "  " + pad(label_of(cfg, j["node"]) if j.get("node") else "-", nw + 1)
                    + pad(c(j.get("status", ""), col), 11) + c(f"{ago:>5} ago", C_DIM)
                    + f"  {fmt_tok(tokens_of(j.get('usage') or {})):>7} tok  " + c((j.get("tags") and ",".join(j["tags"])) or "", C_DIM))
        if j.get("status") == "queued" and j.get("wait"):
            rows.append(c(f"    waits for room: {j['wait']}", C_WARN))
    L.extend(box("WORK", rows, width, "scaleout jobs and heavy commands run in sessions"))
    # tokens of every machine, and the days as grass
    L.extend(box("CLAUDE TOKENS", tokens_lines(cfg, lu, weeks=max(8, min(26, (width - 10) // 2))), width, "every session on each machine"))
    # what Claude is doing right now
    try:
        cn = claude_now()
        cn["nodes"] = {n: read_json(node_state_path(n), {}) for n in cfg["nodes"] if not cfg["nodes"][n].get("local")}
        cl = claude_now_lines(cn)
        head = cl[0].replace("CLAUDE NOW", "", 1).strip() if cl else ""
        L.extend(box("CLAUDE NOW", [c(head, C_TEXT)] + cl[1:], width))
    except Exception:   # the dashboard never dies on a transcript it cannot read
        pass
    pl = productivity_lines(jobs)
    if pl:
        pl[0] = c(pl[0].replace("PRODUCTIVITY", " " * 12, 1), C_DIM)
        pl = [re.sub("[" + SPARKS + "]+", lambda m: c(m.group(), 67), x) for x in pl]   # the little graphs stay in the background
    L.extend(box("PRODUCTIVITY", pl, width))
    return L


def cmd_top(a) -> None:
    cfg_t = 0.0
    cfg = load_cfg()
    lu = LocalUsage()

    def frame() -> List[str]:
        size = shutil.get_terminal_size((110, 40))
        lu.refresh()
        return [clip(x, size.columns - 1) for x in top_lines(cfg, lu, a.interval, size.columns - 1)][:max(3, size.lines - 1)]

    if a.once or not sys.stdout.isatty() or not sys.stdin.isatty():
        lu.refresh()
        print("\n".join(top_lines(cfg, lu, a.interval, shutil.get_terminal_size((110, 40)).columns - 1)))
        return
    import select
    import termios
    import tty
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    out = sys.stdout
    try:
        tty.setcbreak(fd)
        out.write("\x1b[?1049h\x1b[?25l")   # the other screen, no cursor
        while True:
            if now() - cfg_t > 30:
                cfg, cfg_t = load_cfg(), now()
            out.write("\x1b[H" + "".join(line + "\x1b[K\n" for line in frame()) + "\x1b[J")
            out.flush()
            if select.select([fd], [], [], a.interval)[0]:   # sleeps here between redraws: no busy loop
                ch = os.read(fd, 8)
                if ch[:1] in (b"q", b"Q", b"\x03") or ch == b"\x1b":
                    break
    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        out.write("\x1b[?25h\x1b[?1049l")
        out.flush()


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

    s = sp.add_parser("local", help="let this machine run jobs itself while it has room (on | off | status)")
    s.add_argument("action", nargs="?", default="status", choices=["on", "off", "status"])
    s.add_argument("--max-jobs", type=int, default=0)
    s.set_defaults(fn=cmd_local)
    s = sp.add_parser("add", help="attach a macOS/Linux node you can already SSH to (password asked once)")
    s.add_argument("name"); s.add_argument("target", nargs="?", help="user@host")
    s.add_argument("--ssh", help="use an existing ssh alias instead of user@host"); s.add_argument("--lan")
    s.add_argument("--no-tunnel", action="store_true"); s.add_argument("--no-test", action="store_true")
    node_opts(s); s.set_defaults(fn=cmd_add)

    s = sp.add_parser("invite", help="print a one-line installer to paste on a new node")
    s.add_argument("name"); s.add_argument("--os", choices=["unix", "windows"], default="unix"); s.add_argument("--hours", type=int, default=24)
    s.add_argument("--no-brew", action="store_true", help="do not install Homebrew on a macOS node")
    s.add_argument("--no-claude", action="store_true", help="do not install Claude Code on the node")
    s.add_argument("--no-prep", action="store_true", help="Linux: skip the basics (build packages, never sleep, run while logged out)")
    s.add_argument("--autologin", action="store_true", help="Linux desktop: log in by itself after a restart (GDM)")
    s.add_argument("--serve", type=int, default=60, metavar="MIN",
                   help="minutes this machine serves the script on its LAN address, for a short line to type (0: only the long line)")
    node_opts(s); s.set_defaults(fn=cmd_invite)
    s = sp.add_parser("_serve", help=argparse.SUPPRESS)
    s.add_argument("folder"); s.add_argument("host"); s.add_argument("port"); s.add_argument("minutes"); s.add_argument("token"); s.set_defaults(fn=cmd_serve)

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
        s.add_argument("--repo", default="", help="git repo on this machine: the node works on a copy of its current working tree, result comes back as branch scaleout/JOB")
        s.add_argument("--apply", action="store_true", help="with --repo: also apply the result to this working tree (never forced)")
        s.add_argument("--mem", type=float, default=0, help="GB of memory the job needs (default: learned per node and job kind, else by tags)")
        s.add_argument("--cpu", type=int, default=0, help="CPU units: 2 = heavy (builds, tests, simulators), 1 = light (default: guessed from tags and task)")
        s.add_argument("--force", action="store_true", help="with --resume: even if the session was just written here")
        s.add_argument("--model", default="auto", help="Claude model for this job: auto (checks go to check_model), default (the node's own), or a model name")
        s.add_argument("--effort", choices=EFFORTS, help="effort of this job (default: the machine's own; check_effort where --model or a check changes the model)")
        s.add_argument("--check", action="store_true", help="this job only runs and reports (E2E, tests, builds): use check_model")

    s = sp.add_parser("submit", help="queue a job and print its id"); job_opts(s); s.set_defaults(fn=cmd_submit)
    s = sp.add_parser("run", help="queue a job and wait for its result"); job_opts(s); s.add_argument("-m", "--max", type=int, default=540); s.set_defaults(fn=cmd_run)
    s = sp.add_parser("wait", help="wait for a job (exit 2 if still running)"); s.add_argument("job"); s.add_argument("-m", "--max", type=int, default=540, help="seconds, 0 = until it ends (run it in the background)"); s.set_defaults(fn=cmd_wait)
    s = sp.add_parser("result", help="print a finished job's answer"); s.add_argument("job"); s.set_defaults(fn=cmd_result)
    s = sp.add_parser("artifacts", help="screenshots/videos/logs a job saved (paths; --to DIR copies them)")
    s.add_argument("job"); s.add_argument("--to"); s.set_defaults(fn=cmd_artifacts)
    s = sp.add_parser("check", help="jobs the check model ran: were the failures it reported real? JOB real=N false=N missed=N records one")
    s.add_argument("job", nargs="?"); s.add_argument("counts", nargs="*"); s.add_argument("--days", type=int, default=14); s.set_defaults(fn=cmd_check)
    s = sp.add_parser("logs", help="history and stderr of a job"); s.add_argument("job"); s.set_defaults(fn=cmd_logs)
    s = sp.add_parser("cancel", help="cancel a job"); s.add_argument("job"); s.set_defaults(fn=cmd_cancel)
    s = sp.add_parser("jobs", help="recent jobs"); s.add_argument("-n", "--limit", type=int, default=20); s.set_defaults(fn=cmd_jobs)
    s = sp.add_parser("gc", help="clean up now: old jobs, their results and artifacts, logs past their days (the daemon does it every 6 h)"); s.set_defaults(fn=cmd_gc)

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
    s.add_argument("--history", action="store_true")
    s.add_argument("--self", dest="self_", action="store_true", help="score this orchestrator (baseline for `scaleout report`)")
    s.set_defaults(fn=cmd_bench)
    s = sp.add_parser("report", help="productivity: speedup from running in parallel, capacity added, work moved off this machine, its health over time")
    s.add_argument("--days", type=int, default=7); s.set_defaults(fn=cmd_report)
    s = sp.add_parser("tune", help="suggest (or --apply) max_jobs per node from the memory real jobs used")
    s.add_argument("--apply", action="store_true"); s.add_argument("--days", type=int, default=14); s.set_defaults(fn=cmd_tune)
    s = sp.add_parser("stats", help="real work done per node: jobs, failures, run and wait times, memory per job")
    s.add_argument("--days", type=int, default=7); s.set_defaults(fn=cmd_stats)
    s = sp.add_parser("provision", help="install this machine's toolchain (Flutter/fvm, CocoaPods, Node, Firebase CLI, Android platforms) on nodes")
    s.add_argument("names", nargs="*"); s.add_argument("--flutter", help="versions, comma separated (default: this machine's fvm versions)")
    s.add_argument("--android", help="platforms, e.g. android-35,android-36"); s.add_argument("--skip", help="e.g. firebase,android,xcode")
    s.add_argument("--dry-run", action="store_true"); s.set_defaults(fn=cmd_provision)
    s = sp.add_parser("claude-setup", help="install the Claude Code plugin: offload hook, rules for every session, relay subagent, /scaleout commands")
    s.add_argument("--remove", action="store_true"); s.set_defaults(fn=cmd_claude_setup)
    s = sp.add_parser("top", help="live dashboard: orchestrator + nodes (memory, disk, load, jobs) and Claude tokens; low CPU")
    s.add_argument("-i", "--interval", type=int, default=5); s.add_argument("--once", action="store_true"); s.set_defaults(fn=cmd_top)
    s = sp.add_parser("usage", help="Claude tokens of every machine (all sessions there), day by day, and the jobs each one ran")
    s.add_argument("--weeks", type=int, help="weeks of days to draw (default: what fits, up to 26)")
    s.add_argument("--refresh", action="store_true", help="ask every machine now instead of using numbers up to 10 min old")
    s.set_defaults(fn=cmd_usage)
    s = sp.add_parser("insights", help="what the logs say: strain and bottleneck per machine, waits, failures, suggestions")
    s.add_argument("--days", type=int, default=7); s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_insights)
    s = sp.add_parser("unblock", help="let a node's agent take jobs again (after signing it in)")
    s.add_argument("name"); s.add_argument("agent", nargs="?"); s.set_defaults(fn=cmd_unblock)
    s = sp.add_parser("rename", help="give a machine your own name (orchestrator and node are roles); no new name: back to its id")
    s.add_argument("name"); s.add_argument("new", nargs="?"); s.set_defaults(fn=cmd_rename)
    s = sp.add_parser("tick", help="run one scheduling round"); s.set_defaults(fn=cmd_tick)
    s = sp.add_parser("hook", help=argparse.SUPPRESS); s.add_argument("event", choices=["pre", "post", "session", "notes", "ledger", "precompact", "ctx", "agent"]); s.set_defaults(fn=cmd_hook)
    s = sp.add_parser("tokens", help="token saving for every Claude Code session here and on the nodes: earlier compaction, notes + ledger kept across it, self-tuning window")
    s.add_argument("action", nargs="?", choices=["on", "off", "status", "tune"], default="status")
    s.add_argument("--window", help="auto-compact window in tokens (e.g. 250k; under 200k sessions compact again after a few steps)")
    s.add_argument("--auto", dest="auto", action="store_true", default=None, help="let the daemon tune the window once a day from what it measures")
    s.add_argument("--no-auto", dest="auto", action="store_false", help="keep the window fixed")
    s.add_argument("--fork-max", help="refuse forked subagents from sessions larger than this (default 100k)")
    s.set_defaults(fn=cmd_tokens)
    s = sp.add_parser("daemon", help="schedule forever (installed by install-daemon)"); s.add_argument("--interval", type=int, default=15); s.set_defaults(fn=cmd_daemon)
    s = sp.add_parser("install-daemon", help="run the scheduler in the background (launchd / systemd --user)"); s.set_defaults(fn=cmd_install_daemon)

    s = sp.add_parser("runner", help="GitHub Actions self-hosted runners on nodes")
    s.add_argument("action", choices=["add", "remove", "ls"]); s.add_argument("name", nargs="?")
    s.add_argument("--org"); s.add_argument("--repo"); s.add_argument("--labels"); s.add_argument("--runner-name")
    s.add_argument("--group", help="org runner group to join (default: the org's Default group)")
    s.set_defaults(fn=cmd_runner)

    a = p.parse_args(argv)
    paint_auto()
    if a.cmd not in ("add", "invite", "_enroll", "init", "rename"):   # a name set with `scaleout rename` works wherever an id does
        cfg = read_json(CONFIG, {})
        for attr in ("name", "node"):
            if isinstance(getattr(a, attr, None), str):
                setattr(a, attr, node_id(cfg, getattr(a, attr)))
        if isinstance(getattr(a, "names", None), list):
            a.names = [node_id(cfg, x) for x in a.names]
    a.fn(a)


if __name__ == "__main__":
    main()
