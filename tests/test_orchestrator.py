#!/usr/bin/env python3
"""End-to-end tests without SSH or real agents.

Two fake nodes are real node/unix scripts living in temp HOME folders; the orchestrator's Node.ctl/route are
patched to call those scripts locally. A stub `claude` echoes the prompt (or sleeps, to simulate a hang).
Run: python3 tests/test_orchestrator.py
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="scaleout-test-"))
os.environ["SCALEOUT_CONF"] = str(TMP / "conf")
os.environ["SCALEOUT_STATE"] = str(TMP / "state")
os.environ["SCALEOUT_KEY"] = str(TMP / "key")
spec = importlib.util.spec_from_file_location("scaleout", ROOT / "orchestrator" / "scaleout.py")
so = importlib.util.module_from_spec(spec)
spec.loader.exec_module(so)  # type: ignore[union-attr]
so.SSH_CONFIG = TMP / "ssh_config"
so.AUTH_KEYS = TMP / "authorized_keys"
so.KNOWN_HOSTS = TMP / "known_hosts"

STUB = """#!/bin/bash
p="$(head -n1)"; cat >/dev/null
case "$p" in *HANG*) sleep 600;; *FAIL*) echo "boom" >&2; exit 3;; esac
printf '{"result":"done: %s on %s","is_error":false}' "$p" "$(basename "$HOME")"
"""


def make_node(name: str) -> Path:
    h = TMP / name
    b = h / ".scaleout"
    (b / "bin").mkdir(parents=True)
    for d in ("queue", "jobs", "work", "settings"):
        (b / d).mkdir()
    shutil.copy(ROOT / "node/unix/ctl.sh", b / "bin/ctl")
    shutil.copy(ROOT / "node/unix/runner.sh", b / "bin/runner.sh")
    (b / "node.conf").write_text(f"NAME={name}\nWORK_ROOT=~/work\nMAX_JOBS=1\nAGENT=claude\nMIN_FREE_GB=0\nMIN_FREE_MEM_PCT=0\nRUNNER=manual\nDEVICE_SCAN=off\nLINGER_S=0\n")
    (b / "devices.cache").write_text('{"android":0,"ios":0}')
    (h / ".local/bin").mkdir(parents=True)
    (h / ".local/bin/claude").write_text(STUB)
    for f in (b / "bin/ctl", b / "bin/runner.sh", h / ".local/bin/claude"):
        f.chmod(0o755)
    return h


HOMES = {}
OFFLINE = set()
RUNNERS = []


def fake_ctl(self, verb, *args, input=None, timeout=60):
    if self.name in OFFLINE:
        return subprocess.CompletedProcess([], 255, b"", b"unreachable")
    h = HOMES[self.name]
    env = dict(os.environ, HOME=str(h), PATH=f"{h}/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin")
    r = subprocess.run(["bash", str(h / ".scaleout/bin/ctl"), verb, *args], input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=timeout)
    if verb == "submit":  # the node's service manager would start the runner now
        RUNNERS.append(subprocess.Popen(["bash", str(h / ".scaleout/bin/runner.sh")], env=env, start_new_session=True))
    return r


so.Node.ctl = fake_ctl
so.Node.route = lambda self, fresh=False: None if self.name in OFFLINE else "local"


def settle(seconds=8):
    end = time.time() + seconds
    while time.time() < end and any(p.poll() is None for p in RUNNERS):
        time.sleep(0.3)


class E2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cfg = so.load_cfg()
        cfg["orchestrator"] = {"user": "me", "lan": "127.0.0.1", "tunnel": {}}
        cfg["defaults"].update({"metrics_ttl_s": 0, "min_free_disk_gb": 0, "min_free_mem_pct": 0, "cleanup_below_disk_gb": 0, "stall_min": 1, "offline_grace_min": 0, "auto_tune": False,
                                "mem_reserve_gb": -1000})   # the host's real memory must not decide these tests (test_97 checks it)
        cfg["skills"]["auto"] = False
        for n in ("n1", "n2"):
            HOMES[n] = make_node(n)
            cfg["nodes"][n] = {"os": "Darwin", "user": "me", "lan": "x", "max_jobs": 1, "agent": "claude", "tags": [n]}
        so.save_cfg(cfg)

    def test_1_metrics(self):
        m = so.refresh_metrics(so.load_cfg(), max_age=0)
        for n in ("n1", "n2"):
            self.assertTrue(m[n]["online"], m[n])
            self.assertIn("claude", m[n]["agents"])
            self.assertGreaterEqual(m[n]["disk_free_gb"], 1)

    def test_2_spread_and_collect(self):
        cfg = so.load_cfg()
        ids = [so.submit_job(cfg, f"task-{i}") for i in range(3)]
        so.tick()
        nodes = {so.load_job(i)["node"] for i in ids if so.load_job(i)["status"] == "dispatched"}
        self.assertEqual(nodes, {"n1", "n2"}, "two nodes with max_jobs=1 should each get one job")
        for _ in range(6):
            settle()
            so.tick()
            if all(so.load_job(i)["status"] == "done" for i in ids):
                break
        for i in ids:
            rc, text = so.job_output(so.load_job(i))
            self.assertEqual(rc, 0, text)
            self.assertTrue(text.startswith("done: task-"), text)
        # finished job folders are removed from the nodes (disk recovery)
        for h in HOMES.values():
            self.assertEqual([p.name for p in (h / ".scaleout/jobs").iterdir()], [])

    def test_3_failure_reported(self):
        cfg = so.load_cfg()
        jid = so.submit_job(cfg, "please FAIL", node="n1")
        for _ in range(5):
            so.tick(); settle()
            if so.load_job(jid)["status"] in ("done", "failed"):
                break
        so.tick()
        j = so.load_job(jid)
        self.assertEqual(j["status"], "failed")
        rc, text = so.job_output(j)
        self.assertEqual(rc, 1)
        self.assertIn("boom", text)

    def test_4_stall_is_retried_elsewhere(self):
        cfg = so.load_cfg()
        jid = so.submit_job(cfg, "HANG forever", tags=["n1"])
        so.tick()
        self.assertEqual(so.load_job(jid)["node"], "n1")
        time.sleep(2)
        # simulate a crashed runner: kill everything, age the heartbeat
        for p in RUNNERS:
            try:
                os.killpg(p.pid, 9)
            except ProcessLookupError:
                pass
        hb = HOMES["n1"] / ".scaleout/jobs" / jid / "heartbeat"
        old = time.time() - 600
        os.utime(hb, (old, old))
        cfg = so.load_cfg(); cfg["nodes"]["n2"]["tags"] = ["n1", "n2"]; so.save_cfg(cfg)   # let n2 take it
        (HOMES["n2"] / ".local/bin/claude").write_text(STUB.replace("*HANG*) sleep 600;;", "*HANG*) ;;"))
        so.tick()
        j = so.load_job(jid)
        self.assertEqual(j["attempts"], 1)
        self.assertTrue(any(h["event"] == "retry" and "stalled" in h.get("why", "") for h in j["history"]), j["history"])
        for _ in range(5):
            settle(); so.tick()
            if so.load_job(jid)["status"] == "done":
                break
        j = so.load_job(jid)
        self.assertEqual((j["status"], j["node"]), ("done", "n2"), j)
        so.tick()  # orphan on n1 cleaned
        self.assertFalse((HOMES["n1"] / ".scaleout/jobs" / jid).exists())

    def test_5_offline_node_jobs_move(self):
        cfg = so.load_cfg()
        cfg["nodes"]["n2"]["tags"] = ["n2"]; so.save_cfg(cfg)
        (HOMES["n1"] / ".local/bin/claude").write_text(STUB.replace("*HANG*) sleep 600;;", "*HANG*) sleep 30;;"))
        jid = so.submit_job(cfg, "HANG a bit", node="n1")
        so.tick()
        self.assertEqual(so.load_job(jid)["status"], "dispatched")
        OFFLINE.add("n1")
        j = so.load_job(jid); j["last_seen"] -= 3600; so.save_job(j)
        so.tick()
        j = so.load_job(jid)
        self.assertEqual(j["status"], "queued", j)
        self.assertIn("offline", j["history"][-1]["why"])
        OFFLINE.discard("n1")
        so.load_cfg()
        for p in RUNNERS:
            try:
                os.killpg(p.pid, 9)
            except ProcessLookupError:
                pass
        so.cmd_cancel(type("A", (), {"job": jid})())

    def test_6_full_disk_node_is_skipped(self):
        cfg = so.load_cfg()
        cfg["defaults"]["min_free_disk_gb"] = 10 ** 6
        so.save_cfg(cfg)
        jid = so.submit_job(cfg, "anything")
        so.tick()
        self.assertEqual(so.load_job(jid)["status"], "queued")
        st = json.loads((TMP / "state/nodes/n1.json").read_text())
        self.assertEqual(so.node_health(cfg, "n1", st), "disk-full")
        cfg["defaults"]["min_free_disk_gb"] = 0
        so.save_cfg(cfg)
        so.cmd_cancel(type("A", (), {"job": jid})())

    def test_7_sync_folder_round_trip(self):
        src = TMP / "project"
        src.mkdir()
        (src / "input.txt").write_text("hello")
        (HOMES["n2"] / ".local/bin/claude").write_text('#!/bin/bash\ncat > /dev/null; cp input.txt output.txt; echo \'{"result":"copied"}\'\n')
        cfg = so.load_cfg()
        jid = so.submit_job(cfg, "copy", node="n2", sync=str(src))
        for _ in range(6):
            so.tick(); settle()
            if so.load_job(jid)["status"] == "done":
                break
        so.tick()
        self.assertEqual(so.load_job(jid)["status"], "done")
        self.assertEqual((src / "output.txt").read_text(), "hello")

    def test_8_job_held_by_busy_node_moves(self):
        conf = HOMES["n1"] / ".scaleout/node.conf"
        orig = conf.read_text()
        conf.write_text(orig.replace("MIN_FREE_MEM_PCT=0", "MIN_FREE_MEM_PCT=101"))   # n1 "never has enough memory"
        for h in HOMES.values():
            (h / ".local/bin/claude").write_text(STUB)
        cfg = so.load_cfg()
        cfg["nodes"]["n2"]["tags"] = ["n1", "n2"]; so.save_cfg(cfg)
        jid = so.submit_job(cfg, "held", tags=["n1"])
        cfg["nodes"]["n2"]["tags"] = ["n2"]; so.save_cfg(cfg)   # first dispatch can only go to n1
        so.tick()
        self.assertEqual(so.load_job(jid)["node"], "n1")
        time.sleep(3)
        self.assertEqual(json.loads(fake_ctl(so.Node("n1", so.load_cfg()), "state", jid).stdout)["state"], "queued")
        j = so.load_job(jid); j["dispatched_at"] -= 3600; so.save_job(j)
        cfg = so.load_cfg(); cfg["nodes"]["n2"]["tags"] = ["n1", "n2"]; so.save_cfg(cfg)
        so.tick()
        j = so.load_job(jid)
        self.assertTrue(any(h["event"] == "moved" for h in j["history"]), j["history"])
        self.assertEqual(j["attempts"], 0, "a job that never started is moved, not counted as a failure")
        for _ in range(5):
            so.tick(); settle()
            if so.load_job(jid)["status"] == "done":
                break
        self.assertEqual((so.load_job(jid)["status"], so.load_job(jid)["node"]), ("done", "n2"))
        conf.write_text(orig)
        cfg = so.load_cfg(); cfg["nodes"]["n2"]["tags"] = ["n2"]; so.save_cfg(cfg)

    def test_9_real_device_jobs(self):
        settle()   # runners left by earlier tests must be gone, or n2 still looks busy
        (HOMES["n2"] / ".scaleout/devices.cache").write_text('{"android":1,"ios":0}')
        (HOMES["n2"] / ".local/bin/claude").write_text(STUB.replace("*HANG*) sleep 600;;", "*HANG*) sleep 4;;"))
        cfg = so.load_cfg()
        a = so.submit_job(cfg, "HANG on phone", tags=["android-device"])
        b = so.submit_job(cfg, "second phone job", tags=["android-device"])
        c = so.submit_job(cfg, "needs an iPhone", tags=["ios-device"])
        for _ in range(5):
            so.tick()
            if so.load_job(a)["status"] in ("dispatched", "running"):
                break
            time.sleep(1)
        if so.load_job(a)["status"] not in ("dispatched", "running"):   # debug aid
            cfg2 = so.load_cfg(); st = json.loads((TMP / "state/nodes/n2.json").read_text())
            print("\nDEBUG n2", so.node_health(cfg2, "n2", st), {k: st.get(k) for k in ("devices", "running", "queued", "os", "agents")}, cfg2["nodes"]["n2"])
            print("DEBUG jobs", [(x["id"][-4:], x["status"], x.get("node"), x.get("tags")) for x in so.all_jobs() if x["status"] in ("queued", "dispatched", "running")])
        self.assertIn(so.load_job(a)["status"], ("dispatched", "running"))
        self.assertEqual(so.load_job(a)["node"], "n2")
        self.assertEqual(so.load_job(b)["status"], "queued", "one job per attached phone")
        self.assertEqual(so.load_job(c)["status"], "queued", "no node has an iPhone")
        for _ in range(8):
            settle(); so.tick()
            if so.load_job(b)["status"] == "done":
                break
        self.assertEqual((so.load_job(a)["status"], so.load_job(b)["status"], so.load_job(b)["node"]), ("done", "done", "n2"))
        so.cmd_cancel(type("A", (), {"job": c})())
        self.assertEqual(so.fmt_devices({"devices": {"android": 1, "ios": 2}}), "A1 I2")

    def test_95_bench_and_tune(self):
        self.assertEqual(so.bench_score({"single_mbps": 55, "multi_mbps": 230, "mem_total_gb": 8})["score"], 100)
        self.assertEqual(so.bench_score({"single_mbps": 55, "multi_mbps": 230, "mem_total_gb": 16})["score"], 200)
        self.assertEqual(so.bench_score({"single_mbps": 55, "multi_mbps": 230, "mem_total_gb": 4})["slots"], 0)
        so.run_bench(["n1", "n2"])
        h = so.bench_history()[-1]
        self.assertEqual(set(h["nodes"]), {"n1", "n2"})
        self.assertGreater(h["total"], 0)
        # five finished jobs that each added 4 GB -> a 16 GB node with 3 GB idle fits 3 of them
        cfg = so.load_cfg()
        st = json.loads((TMP / "state/nodes/n1.json").read_text()); st.update({"mem_total_gb": 16, "idle_used_gb": 3}); so.write_json(TMP / "state/nodes/n1.json", st)
        for i in range(5):
            jid = so.submit_job(cfg, f"m{i}", node="n1")
            j = so.load_job(jid); j.update({"status": "done", "node": "n1", "mem_mb": 4096, "finished_at": so.now()}); so.save_job(j)
        h["nodes"]["n1"]["cpu_slots"] = 99; so.write_json(TMP / "state/bench.json", [h])
        so.tune(so.load_cfg(), apply=True, quiet=True)
        self.assertEqual(so.load_cfg()["nodes"]["n1"]["max_jobs"], 3)
        self.assertIn("MAX_JOBS=3", (HOMES["n1"] / ".scaleout/node.conf").read_text())
        cfg = so.load_cfg(); cfg["nodes"]["n1"]["max_jobs"] = 1; cfg["nodes"]["n1"]["max_jobs_fixed"] = True; so.save_cfg(cfg)
        so.tune(so.load_cfg(), apply=True, quiet=True)
        self.assertEqual(so.load_cfg()["nodes"]["n1"]["max_jobs"], 1, "hand-set max_jobs is never changed")

    def test_96_repo_mode(self):
        repo = TMP / "repo"
        repo.mkdir()
        g = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        g("init", "-q"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
        (repo / "a.txt").write_text("one\n"); g("add", "a.txt"); g("commit", "-qm", "init")
        (repo / "b.txt").write_text("uncommitted, untracked\n")          # must reach the node and must not come back as a change
        (HOMES["n2"] / ".local/bin/claude").write_text('#!/bin/bash\ncat >/dev/null; test -f b.txt || exit 7; echo two >> a.txt; echo new > c.txt; echo \'{"result":"edited"}\'\n')
        cfg = so.load_cfg()
        jid = so.submit_job(cfg, "edit files", node="n2", repo=str(repo), apply=True)
        self.assertEqual(g("status", "--porcelain").stdout.decode().strip(), "?? b.txt", "snapshot must not touch the user's index or files")
        for _ in range(8):
            so.tick(); settle()
            if so.load_job(jid)["status"] in ("done", "failed"):
                break
        so.tick()
        j = so.load_job(jid)
        self.assertEqual(j["status"], "done", j)
        self.assertEqual(j.get("repo_branch"), f"scaleout/{jid}", j)
        self.assertTrue(j.get("repo_applied"), j)
        self.assertEqual((repo / "a.txt").read_text(), "one\ntwo\n")
        self.assertEqual((repo / "c.txt").read_text(), "new\n")
        changed = g("diff", "--name-only", j["repo_base"], j["repo_branch"]).stdout.decode().split()
        self.assertEqual(sorted(changed), ["a.txt", "c.txt"])
        self.assertIn("[repo] result branch", so.repo_text(j))
        # the work tree on the node is unlocked and keeps ignored files between jobs
        self.assertEqual(list((HOMES["n2"] / ".scaleout/repos").glob("*.lock")), [])

    def test_97_memory_admission(self):
        cfg = so.load_cfg()
        cfg["nodes"] = {"big": {"max_jobs": 8, "tags": ["emulator"], "agent": "claude"}}
        cfg["defaults"]["mem_reserve_gb"] = 1.5
        st = {"online": True, "mem_total_gb": 16, "mem_free_pct": 30, "disk_free_gb": 50, "runner": True,
              "agents": ["claude"], "os": "Darwin", "cpus": 8, "load": 0}
        emu, plain = {"tags": ["emulator"], "agent": "default"}, {"tags": [], "agent": "default"}
        self.assertEqual(so.job_mem_gb(emu, "big"), 7.5)
        # busy node with 4.8 GB free: a 3 GB job fits, a 7.5 GB emulator job does not
        self.assertEqual(so.pick_node(cfg, plain, {"big": st}, {"big": 1}), "big")
        self.assertIsNone(so.pick_node(cfg, emu, {"big": st}, {"big": 1}))
        # an idle node always takes one job (its own memory guard still protects it)
        self.assertEqual(so.pick_node(cfg, emu, {"big": st}, {"big": 0}), "big")
        # plenty free: several emulators fit, until the jobs just handed out use it up
        st2 = dict(st, mem_free_pct=80)   # 12.8 GB free
        self.assertEqual(so.pick_node(cfg, emu, {"big": st2}, {"big": 1}), "big")
        self.assertIsNone(so.pick_node(cfg, emu, {"big": st2}, {"big": 1}, planned_gb={"big": 7.5}))
        # an explicit --mem wins
        self.assertEqual(so.job_mem_gb({"tags": ["emulator"], "mem_gb": 2}, "big"), 2.0)

    def test_98_usage(self):
        self.assertEqual((so.fmt_tok(950), so.fmt_tok(33_000), so.fmt_tok(2_500_000), so.fmt_tok(6.7e9)), ("950", "33k", "2.5M", "6.7B"))
        cfg = so.load_cfg()
        jid = so.submit_job(cfg, "usage", node="n1")
        j = so.load_job(jid)
        j.update({"status": "done", "node": "n1", "dispatched_at": so.now() - 120, "finished_at": so.now(),
                  "usage": {"in": 10, "out": 20, "cache_read": 300, "cache_write": 4000}, "cost_usd": 0.25})
        so.save_job(j)
        per = so.job_usage(3600)["n1"]
        self.assertGreaterEqual(per["tokens"], 4330)
        self.assertGreaterEqual(per["cost"], 0.25)
        self.assertGreaterEqual(per["busy_s"], 119)
        lines = "\n".join(so.top_lines(so.load_cfg(), type("U", (), {"window": lambda self, s: {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0, "msgs": 0}})(), 5))
        self.assertIn("WORK DONE BY NODES", lines)
        self.assertIn("orchestrator", lines)

    def test_99_helpers(self):
        self.assertEqual(so.parse_setup("x\nSCALEOUT_USER bob\nSCALEOUT_AGENTS [\"claude\"]\n")["user"], "bob")
        so.ssh_block_put("scaleout t", "Host scaleout-t\n  Port 1")
        so.ssh_block_put("scaleout t", "Host scaleout-t\n  Port 2")
        txt = so.SSH_CONFIG.read_text()
        self.assertEqual(txt.count("Host scaleout-t\n"), 1)
        self.assertIn("Port 2", txt)
        import tarfile, io
        names = tarfile.open(fileobj=io.BytesIO(so.node_package("unix"))).getnames()
        self.assertTrue({"ctl.sh", "runner.sh", "setup.sh", "claude-settings.json"} <= set(names), names)
        names = tarfile.open(fileobj=io.BytesIO(so.node_package("windows"))).getnames()
        self.assertTrue({"ctl.ps1", "runner.ps1", "setup.ps1"} <= set(names), names)


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        for p in RUNNERS:
            try:
                os.killpg(p.pid, 9)
            except (ProcessLookupError, PermissionError):
                pass
        shutil.rmtree(TMP, ignore_errors=True)
