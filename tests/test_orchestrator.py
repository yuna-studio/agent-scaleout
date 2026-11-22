#!/usr/bin/env python3
"""End-to-end tests without SSH or real agents.

Two fake nodes are real node/unix scripts living in temp HOME folders; the orchestrator's Node.ctl/route are
patched to call those scripts locally. A stub `claude` echoes the prompt (or sleeps, to simulate a hang).
Run: python3 tests/test_orchestrator.py
"""
import argparse
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
    shutil.copy(ROOT / "node/unix/usage.py", b / "bin/usage.py")
    (b / "node.conf").write_text(f"NAME={name}\nWORK_ROOT=~/work\nMAX_JOBS=1\nAGENT=claude\nMIN_FREE_GB=0\nMIN_FREE_MEM_PCT=0\nRUNNER=manual\nDEVICE_SCAN=off\nLINGER_S=0\nKEEP_DEVICES=1\n")
    (b / "devices.cache").write_text('{"android":0,"ios":0}')
    (h / ".local/bin").mkdir(parents=True)
    (h / ".local/bin/claude").write_text(STUB)
    for f in (b / "bin/ctl", b / "bin/runner.sh", h / ".local/bin/claude"):
        f.chmod(0o755)
    return h


HOMES = {}
OFFLINE = set()
RUNNERS = []


ORIG_CTL = so.Node.ctl


def fake_ctl(self, verb, *args, input=None, timeout=60):
    if self.local:   # the orchestrator's own executor runs for real (in the test state folder)
        return ORIG_CTL(self, verb, *args, input=input, timeout=timeout)
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
                                "mem_reserve_gb": -1000, "reap_idle_min": 0, "max_load_per_cpu": 1000})
        # reap 0: never touch this host's real simulators; reserve and load: this host's real memory and CPU must not decide these tests (test_97 checks them)
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
        settle(30)   # runners left by earlier tests must be gone, or two runners can take the same job on n2
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
        self.assertIn("lacks ios-device", so.load_job(c).get("wait", ""), "a waiting job says why")
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
        # measured CPU slots set the ceiling: heavy jobs (7 GB p90) must not pin a 16 GB, 2-slot node to one job,
        # since every job's memory is checked when it is handed out
        for i in range(5):
            jid = so.submit_job(cfg, f"h{i}", node="n1")
            j = so.load_job(jid); j.update({"status": "done", "node": "n1", "mem_mb": 7 * 1024, "finished_at": so.now()}); so.save_job(j)
        h["nodes"]["n1"]["cpu_slots"] = 2; so.write_json(TMP / "state/bench.json", [h])
        so.tune(so.load_cfg(), apply=True, quiet=True)
        self.assertEqual(so.load_cfg()["nodes"]["n1"]["max_jobs"], 2)
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

    def test_96b_orchestrator_runs_jobs_itself(self):
        """`scaleout local on`: this machine runs jobs itself from a local copy on its own disk (no SSH, no node install)."""
        repo = TMP / "repo-local"
        repo.mkdir()
        g = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        g("init", "-q"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
        (repo / "a.txt").write_text("one\n"); g("add", "a.txt"); g("commit", "-qm", "init")
        (repo / "b.txt").write_text("uncommitted\n")
        agent = TMP / "local-agent.sh"
        agent.write_text('#!/bin/bash\ncat >/dev/null; test -f b.txt || exit 7; test -n "$SCALEOUT_JOB_ID" || exit 8; echo two >> a.txt; echo \'{"result":"edited here"}\'\n')
        so.cmd_local(argparse.Namespace(action="on", max_jobs=1))
        lh = so.local_home()
        (lh / "node.conf").write_text(f"NAME=orchestrator\nRUNNER=manual\nKEEP_DEVICES=1\nMAX_JOBS=1\nAGENT=custom\nAGENT_CMD='bash {agent}'\n"
                                      "MIN_FREE_GB=0\nMIN_FREE_MEM_PCT=0\nMAX_LOAD_PER_CPU=1000\nDEVICE_SCAN=off\nLINGER_S=0\n")
        (lh / "devices.cache").write_text('{"android":0,"ios":0}')
        cfg = so.load_cfg()
        try:
            self.assertTrue(json.loads((lh / "settings/claude.json").read_text())["permissions"]["deny"][-1].startswith("Edit("))
            jid = so.submit_job(cfg, "edit here", node=so.LOCAL, repo=str(repo))
            for _ in range(30):
                so.tick(); time.sleep(1)
                if so.load_job(jid)["status"] in ("done", "failed"):
                    break
            so.tick()
            j = so.load_job(jid)
            self.assertEqual(j["status"], "done", j)
            self.assertEqual(j.get("repo_branch"), f"scaleout/{jid}", j)
            self.assertEqual(g("diff", "--name-only", j["repo_base"], j["repo_branch"]).stdout.decode().split(), ["a.txt"])
            self.assertEqual((repo / "a.txt").read_text(), "one\n", "without --apply the user's files stay as they were")
            self.assertTrue(list(lh.glob("repos/*.git")), "a copy on this machine's disk, reached by path")
            self.assertEqual(list(lh.glob("repos/*.lock")), [])
            self.assertEqual(g("worktree", "list").stdout.decode().count("\n"), 1, "the user's repository gets no work trees")
            # off: it takes no more jobs
            so.cmd_local(argparse.Namespace(action="off", dir=None, max_jobs=0))
            self.assertNotIn(so.LOCAL, so.load_cfg()["nodes"])
        finally:
            cfg = so.load_cfg()
            cfg["nodes"].pop(so.LOCAL, None)
            so.save_cfg(cfg)

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
        # CPU: a node with load above 2 × cores gets nothing more, idle or not (the load may be other work, e.g. CI)
        cfg["defaults"].pop("max_load_per_cpu", None)   # the shipped default (2)
        hot = dict(st2, load=17)   # above 2 × 8
        self.assertIsNone(so.pick_node(cfg, plain, {"big": hot}, {"big": 1}))
        self.assertIsNone(so.pick_node(cfg, plain, {"big": hot}, {"big": 0}))
        cfg["defaults"]["max_load_per_cpu"] = "2.5"   # a value saved as text still works
        self.assertEqual(so.pick_node(cfg, plain, {"big": hot}, {"big": 0}), "big")
        cfg["defaults"]["max_load_per_cpu"] = 1000
        # CPU units: a heavy job fills a 2-unit node alone, two light jobs share it
        cfg["nodes"]["big"]["cpu_slots"] = 2
        heavy = {"id": "x-heavy", "tags": ["emulator"], "agent": "default"}
        light = {"id": "x-light", "tags": [], "agent": "default"}
        self.assertEqual((so.job_cpu(heavy), so.job_cpu(light)), (2, 1))
        busy_cpu = dict(st2, load=9)   # cores really busy
        self.assertIsNone(so.pick_node(cfg, light, {"big": busy_cpu}, {"big": 1}, cpu_used={"big": 2}))
        self.assertEqual(so.pick_node(cfg, light, {"big": st2}, {"big": 1}, cpu_used={"big": 2}), "big")   # quiet phase: room
        self.assertEqual(so.pick_node(cfg, light, {"big": st2}, {"big": 1}, cpu_used={"big": 1}), "big")
        self.assertEqual(so.job_cpu({"id": "x", "tags": [], "cpu": 1}), 1)
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
        self.assertIn("SCALEOUT JOBS BY MACHINE", lines)
        self.assertIn("MACHINES", lines)
        self.assertIn("macOS", lines)   # every machine's OS is on the dashboard
        self.assertIn("orchestrator", lines)

    def test_98a_tokens_per_machine(self):
        """Every session's tokens on a node, per day: a message written twice counts once; the numbers reach the orchestrator."""
        proj = HOMES["n1"] / ".claude/projects/p"
        (proj / "s1/subagents").mkdir(parents=True)
        ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        row = lambda mid, out: json.dumps({"timestamp": ts, "message": {"id": mid, "usage": {
            "input_tokens": 1, "output_tokens": out, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 10}}})
        (proj / "s1.jsonl").write_text(row("m1", 5) + "\n" + row("m1", 5) + "\n" + row("m2", 7) + "\nnot json\n")
        (proj / "s1/subagents/a.jsonl").write_text(row("m3", 9) + "\n")
        so.write_json(so.USAGE_DAYS, {"machines": {so.LOCAL: {"days": {}, "tried": so.now()}}})   # this host's real transcripts stay out
        cfg = so.load_cfg()
        today = time.strftime("%Y-%m-%d")
        d = so.collect_usage(cfg, max_age=600)["machines"]["n1"]["days"][today]
        self.assertEqual((d["out"], d["msgs"], d["cache_read"]), (21, 3, 300))
        with open(proj / "s1.jsonl", "a") as f:
            f.write(row("m2", 7) + "\n" + row("m4", 1) + "\n")   # m2 again (another content block), then a new message
        self.assertEqual(so.collect_usage(cfg, max_age=600)["machines"]["n1"]["days"][today]["out"], 21)   # asked a moment ago: kept
        d = so.collect_usage(cfg, max_age=0)["machines"]["n1"]["days"][today]
        self.assertEqual((d["out"], d["msgs"]), (22, 4))
        text = "\n".join(so.tokens_lines(cfg, None, weeks=4))
        self.assertIn("n1", text)
        self.assertIn("Mon", text)
        self.assertEqual(so.day_tokens({"days": {today: d}}, today), 4 + 22 + 400 + 40)

    def test_98c_names_and_os(self):
        """A machine's own name works wherever its id does; its OS counts as a tag and shows in words."""
        cfg = so.load_cfg()
        cfg["nodes"]["n1"]["label"] = "studio"
        self.assertEqual((so.node_id(cfg, "studio"), so.node_id(cfg, "n1"), so.node_id(cfg, "nobody")), ("n1", "n1", "nobody"))
        self.assertEqual((so.label_of(cfg, "n1"), so.label_of(cfg, "n2"), so.role_of(cfg, "n1"), so.role_of(cfg, so.LOCAL)),
                         ("studio", "n2", "node", "orchestrator"))
        self.assertEqual(so.fmt_os({"os": "Linux", "arch": "x86_64"}), "Linux x64")
        self.assertEqual(so.fmt_os({"os": "Darwin", "arch": "arm64"}), "macOS arm64")
        self.assertEqual(so.fmt_os({}, {"os": "windows"}), "Windows")
        self.assertIn("mac", so.OS_TAGS["darwin"])
        self.assertEqual((so.vlen("미니"), so.vlen(so.pad("미니", 6)), so.vlen(so.clip("abcdefgh", 5))), (4, 6, 5))

    def test_98d_session_runs(self):
        """A heavy command a session runs here (not through scaleout) is remembered by the hooks and shown with the jobs."""
        hooks = ROOT / "agents/claude"
        (TMP / "hookhome/.config/scaleout").mkdir(parents=True, exist_ok=True)
        (TMP / "hookhome/.config/scaleout/hook.conf").write_text("OFFLOAD=off\n")   # runs here whatever this machine's load is
        env = dict(os.environ, SCALEOUT_STATE=str(so.STATE_DIR), SCALEOUT_CONF=str(so.CONF_DIR), HOME=str(TMP / "hookhome"))
        hook = lambda script, data: subprocess.run(["bash", str(hooks / script)], input=json.dumps(data).encode(), env=env,
                                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE).returncode
        pre = lambda tid, cmd, bg=False: hook("offload-hook.sh", {"session_id": "sess1", "cwd": "/p/app", "tool_use_id": tid,
                                                                   "tool_input": {"command": cmd, "run_in_background": bg}})
        self.assertEqual(pre("toolu_t1", "cd app && flutter test"), 0)
        self.assertEqual(pre("toolu_t2", "ls -la"), 0)                              # light: not remembered
        self.assertEqual(pre("toolu_t3", "SCALEOUT_LOCAL=1 xcodebuild test"), 0)
        self.assertEqual(pre("toolu_t4", "npm run build", bg=True), 0)
        self.assertEqual(sorted(p.stem for p in so.DIRECT_DIR.glob("*.json")), ["toolu_t1", "toolu_t3", "toolu_t4"])
        self.assertEqual(hook("post-hook.sh", {"hook_event_name": "PostToolUse", "tool_use_id": "toolu_t1", "duration_ms": 5000}), 0)
        hook("post-hook.sh", {"hook_event_name": "PostToolUseFailure", "tool_use_id": "toolu_t3", "is_interrupt": True})
        hook("post-hook.sh", {"hook_event_name": "PostToolUse", "tool_use_id": "toolu_t4", "tool_response": {"backgroundTaskId": "bgt4"}})
        hook("post-hook.sh", {"hook_event_name": "PostToolUse", "tool_use_id": "toolu_t2"})
        runs = {j["id"][-2:]: j for j in so.direct_runs()}
        self.assertEqual({k: v["status"] for k, v in runs.items()}, {"t1": "done", "t3": "cancelled", "t4": "running"})
        self.assertEqual(runs["t1"]["what"], "flutter test")
        self.assertAlmostEqual(runs["t1"]["finished_at"] - runs["t1"]["created"], 5, delta=0.5)
        out = Path(f"/tmp/claude-{os.getuid()}/scaleout-test/sess1/tasks/bgt4.output")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("ok\n[exited with code 2]\n")
        try:
            self.assertEqual({j["id"][-2:]: j["status"] for j in so.direct_runs()}["t4"], "failed")
        finally:
            shutil.rmtree(out.parent.parent.parent, ignore_errors=True)
        text = "\n".join(so.top_lines(so.load_cfg(), type("U", (), {"window": lambda self, s: {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0, "msgs": 0}})(), 5))
        self.assertIn("flutter test", text)
        self.assertIn("in a session", text)

    def test_98e_tunnel_prefers_lan(self):
        """The reverse tunnel goes straight to the orchestrator whenever its LAN address answers, else through the tunnel host."""
        cfg = so.load_cfg()
        cfg["orchestrator"].update({"lan": "127.0.0.1", "ssh_port": 22, "tunnel": {"host": "orch.example.com", "proxy": "cloudflared access ssh --hostname %h"}})
        args = so.tunnel_args(cfg, 2299)
        self.assertEqual(args[args.index("--tunnel-lan") + 1], "127.0.0.1:22")
        f = TMP / "tunnel-ssh-config"
        for lan, want in (("127.0.0.1:1", "orch.example.com"), ("", "orch.example.com")):
            env = dict(os.environ, OS=os.uname().sysname, TH="orch.example.com", TU="me", TP="2299", TSP="22",
                       TPROXY="/bin/false access ssh --hostname %h", TLAN=lan)
            text = subprocess.run(["bash", "-c", 'eval "$(sed -n "/^tunnel_block()/,/^}/p" "$1")"; tunnel_block', "_", str(ROOT / "node/unix/setup.sh")],
                                  env=env, stdout=subprocess.PIPE, check=True).stdout.decode()
            f.write_text(text)
            self.assertIn("RemoteForward 2299 localhost:22", text)
            self.assertEqual("Match originalhost" in text, bool(lan))
            got = subprocess.run(["ssh", "-G", "-F", str(f), "scaleout-orchestrator"], stdout=subprocess.PIPE).stdout.decode()
            self.assertIn(f"hostname {want}\n", got)   # port 1 on this host does not answer: the tunnel host
        cfg["orchestrator"]["tunnel"] = {}
        so.save_cfg(cfg)

    def test_98f_invite_short_line(self):
        """`scaleout invite` serves its script on the LAN for a short line to type; Linux basics are in it; using the invite stops it."""
        import contextlib, io, re, urllib.request
        cfg = so.load_cfg()
        cfg["orchestrator"].update({"lan": "127.0.0.1", "user": "me", "tunnel": {}})
        so.save_cfg(cfg)
        args = so.main.__globals__["argparse"].Namespace(name="newpc", os="unix", hours=1, no_brew=True, no_claude=True, no_prep=False,
                                                          autologin=True, serve=1, dir="~/w", max_jobs=None, price=None, agent="claude", tags=None)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            so.cmd_invite(args)
        text = out.getvalue()
        url = re.search(r"curl -fsSL (http://127\.0\.0\.1:\d+/\S+) ", text).group(1)
        self.assertIn("Not on the same network? Paste this instead:", text)
        body = ""
        for _ in range(50):   # the server starts in the background
            try:
                body = urllib.request.urlopen(url, timeout=2).read().decode()
                break
            except OSError:
                time.sleep(0.1)
        self.assertIn("[0/5] Linux node basics", body)
        self.assertIn("SO_PREP=1; SO_AUTOLOGIN=1", body)
        self.assertIn("loginctl enable-linger", body)
        with self.assertRaises(OSError):
            urllib.request.urlopen(url.rsplit("/", 1)[0] + "/wrong", timeout=2)
        shutil.rmtree(so.CONF_DIR / "invites" / "newpc", ignore_errors=True)   # what enrolling does: the server stops
        time.sleep(6)
        with self.assertRaises(OSError):
            urllib.request.urlopen(url, timeout=2)
        cfg = so.load_cfg(); cfg["nodes"].pop("newpc", None); so.save_cfg(cfg)

    def test_98h_extra_tokens(self):
        """A job's starting context (first request's whole input) is counted as tokens spent only because it ran apart."""
        sid = "11111111-2222-3333-4444-555555555555"
        proj = HOMES["n2"] / ".claude/projects/q"
        proj.mkdir(parents=True, exist_ok=True)
        row = lambda mid, i, cw, cr: json.dumps({"type": "assistant", "message": {"id": mid, "usage": {
            "input_tokens": i, "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr, "output_tokens": 5}}})
        (proj / f"{sid}.jsonl").write_text("\n".join([row("a", 3, 20000, 0), row("a", 3, 20000, 0), row("b", 2, 500, 20003)]) + "\n")
        out = json.loads(fake_ctl(so.Node("n2", so.load_cfg()), "job-tokens", sid).stdout.decode().strip())
        self.assertEqual(out, {"start": 20003, "requests": 2})
        j = so.load_job(so.submit_job(so.load_cfg(), "measured"))
        j.update({"status": "done", "node": "n2", "dispatched_at": so.now() - 60, "finished_at": so.now(),
                  "usage": {"in": 5, "out": 10, "cache_read": 20003, "cache_write": 20500}, "start_tokens": 20003, "requests": 2})
        so.save_job(j)
        per = so.job_usage(3600)["n2"]
        self.assertGreaterEqual(per["extra"], 20003)
        self.assertGreaterEqual(per["measured"], 1)
        self.assertIn("wasted 7d", "\n".join(so.tokens_lines(so.load_cfg(), None, weeks=2, grass=False)))

    def test_98i_offload_always(self):
        """By default a heavy command is sent to the pool whenever a node has room, naming the free machines."""
        home = TMP / "hookhome2"; home.mkdir(exist_ok=True)
        env = dict(os.environ, SCALEOUT_STATE=str(so.STATE_DIR), SCALEOUT_CONF=str(so.CONF_DIR), HOME=str(home))
        for n in ("n1", "n2"):   # healthy and fresh
            st = so.read_json(so.node_state_path(n), {}); st.update(online=True, ts=so.now(), mem_free_pct=80, disk_free_gb=50, runner=True, load=0.1, cpus=8)
            so.write_json(so.node_state_path(n), st)
        cmd = "cd app && " + "flutter" + " test"
        r = subprocess.run(["bash", str(ROOT / "agents/claude/offload-hook.sh")], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           input=json.dumps({"session_id": "s", "tool_use_id": "toolu_off1", "tool_input": {"command": cmd}}).encode())
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("scaleout submit", r.stderr.decode())
        self.assertIn("free now:", r.stderr.decode())
        (home / ".config/scaleout").mkdir(parents=True, exist_ok=True)
        (home / ".config/scaleout/hook.conf").write_text("OFFLOAD=off\n")
        r = subprocess.run(["bash", str(ROOT / "agents/claude/offload-hook.sh")], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           input=json.dumps({"session_id": "s", "tool_use_id": "toolu_off2", "tool_input": {"command": cmd}}).encode())
        self.assertEqual(r.returncode, 0)

    def test_98j_even_by_capacity(self):
        """Jobs go to the machine least full for its size: a node allowed 4 jobs takes about four for each one a 1-job node takes."""
        cfg = so.load_cfg()
        cfg["nodes"]["n1"]["max_jobs"], cfg["nodes"]["n2"]["max_jobs"] = 4, 1
        m = {n: {"online": True, "ts": so.now(), "mem_free_pct": 80, "mem_total_gb": 64, "disk_free_gb": 50, "load": 0.1, "cpus": 16,
                 "agents": ["claude"], "runner": True} for n in ("n1", "n2")}
        load, picks = {}, []
        for k in range(5):
            j = {"tags": [], "agent": "default", "id": f"e{k}"}
            n = so.pick_node(cfg, j, m, load, {}, {}, {}, {})
            picks.append(n); load[n] = load.get(n, 0) + 1
        self.assertEqual(picks.count("n1"), 4)
        self.assertEqual(picks.count("n2"), 1)
        cfg["nodes"]["n1"]["max_jobs"] = cfg["nodes"]["n2"]["max_jobs"] = 1

    def test_98k_cap_is_threads_load_decides(self):
        """A node takes jobs up to its thread count (load and memory decide), and stops at 2x load; a fixed max_jobs stays the cap."""
        cfg = so.load_cfg()
        cfg["nodes"]["n1"]["max_jobs"] = 2; cfg["nodes"]["n1"].pop("max_jobs_fixed", None)
        st = {"online": True, "ts": so.now(), "mem_free_pct": 90, "mem_total_gb": 128, "disk_free_gb": 50, "load": 1.0, "cpus": 8,
              "agents": ["claude"], "runner": True}
        self.assertEqual(so.job_cap("n1", cfg["nodes"]["n1"], st), 8)
        m = {"n1": dict(st)}
        load, n = {}, 0
        while so.pick_node(cfg, {"tags": ["n1"], "agent": "default", "id": f"c{n}"}, m, load, {}, {}, {}, {}) == "n1" and n < 20:
            load["n1"] = load.get("n1", 0) + 1; n += 1
        self.assertEqual(n, 8)
        why = {}
        m["n1"]["load"] = 17.0
        keep = cfg["defaults"]["max_load_per_cpu"]; cfg["defaults"]["max_load_per_cpu"] = 2   # the tests run with it off
        self.assertIsNone(so.pick_node(cfg, {"tags": ["n1"], "agent": "default", "id": "c99"}, m, {}, {}, {}, {}, why))
        cfg["defaults"]["max_load_per_cpu"] = keep
        self.assertIn("load 17.0 > 16", why["n1"])
        cfg["nodes"]["n1"]["max_jobs_fixed"] = True
        self.assertEqual(so.job_cap("n1", cfg["nodes"]["n1"], st), 2)
        cfg["nodes"]["n1"]["max_jobs"] = 1; cfg["nodes"]["n1"].pop("max_jobs_fixed", None)

    def test_98l_plugin_version_follows(self):
        """The plugin's version is the scaleout version (it is read in place from the installed package)."""
        self.assertEqual(json.loads((ROOT / "plugin/.claude-plugin/plugin.json").read_text())["version"], so.VERSION)

    def test_98m_agent_limit_moves_job(self):
        """A job that failed on a node's usage limit or lost login is not a failure: that agent is skipped and the job goes on."""
        t = so.agent_trouble('"api_error":"usage_limit_reached","result":"You\'ve hit your weekly limit · resets Nov 23 at 12pm"')
        self.assertEqual(t[0], "usage limit")
        self.assertEqual(so.agent_trouble("refresh token has expired. Please log out and sign in again.")[0], "signed out")
        self.assertIsNone(so.agent_trouble("3 tests failed"))
        so.block_agent("n1", "claude", "usage limit", so.now() + 600)
        why = {}
        m = {n: {"online": True, "ts": so.now(), "mem_free_pct": 80, "mem_total_gb": 64, "disk_free_gb": 50, "load": 0.1, "cpus": 8,
                 "agents": ["claude"], "runner": True} for n in ("n1", "n2")}
        self.assertEqual(so.pick_node(so.load_cfg(), {"tags": [], "agent": "default", "id": "lim"}, m, {}, {}, {}, {}, why), "n2")
        self.assertIn("usage limit", why["n1"])
        so.write_json(so.AGENT_BLOCKS, {})
        text = "\n".join(so.top_lines(so.load_cfg(), type("U", (), {"window": lambda self, s: {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0, "msgs": 0}})(), 5))
        self.assertIn("EFFICIENCY", text)
        self.assertIn("BOTTLENECK", text)

    def test_98n_log_insights_housekeep(self):
        """A finished job leaves a line in the job log; housekeeping drops old jobs with their results and keeps the log;
        insights reads the log back."""
        t0 = so.now() - 10 * 86400
        j = {"id": "j20200101-000000-aaaa", "status": "done", "rc": 0, "created": t0, "dispatched_at": t0 + 30, "finished_at": t0 + 330,
             "node": "n1", "tags": ["emulator"], "agent": "default", "history": [{"t": t0, "event": "moved", "why": "claude on n2: usage limit until x", "node": "n2"}],
             "mem_mb": 3000}
        so.save_job(j)
        rows = so.read_jsonl(so.JOB_LOG)
        mine = [r for r in rows if r["id"] == j["id"]]
        self.assertEqual(len(mine), 1)
        self.assertEqual((mine[0]["wait_s"], mine[0]["run_s"]), (30, 300))
        so.save_job(so.load_job(j["id"]))
        self.assertEqual(len([r for r in so.read_jsonl(so.JOB_LOG) if r["id"] == j["id"]]), 1)   # logged once
        res = so.STATE_DIR / "results" / j["id"]
        res.mkdir(parents=True, exist_ok=True); (res / "shot.png").write_bytes(b"x" * 1000)
        rec = so.housekeep(so.load_cfg())
        self.assertGreaterEqual(rec["jobs"], 1)
        self.assertFalse(so.job_path(j["id"]).exists())
        self.assertFalse(res.exists())
        old_row = {"id": "j-old", "t": int(so.now() - 200 * 86400)}
        with open(so.JOB_LOG, "a") as f:
            f.write(json.dumps(old_row) + "\n")
        so.trim_jsonl(so.JOB_LOG, 90)
        ids = [r["id"] for r in so.read_jsonl(so.JOB_LOG)]
        self.assertNotIn("j-old", ids)
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            so.cmd_insights(argparse.Namespace(days=30, json=False))
        self.assertIn("SUGGESTIONS", buf.getvalue())

    def test_98b_claude_now(self):
        """Sessions working now, their running subagents (finished or stale ones left out), names from /rename."""
        home = TMP / "claude-home"
        proj = home / ".claude" / "projects" / "-repo"
        (proj / "s1" / "subagents").mkdir(parents=True)
        (proj / "s1.jsonl").write_text('{"type":"user"}\n{"type":"custom-title","customTitle":"아이디어"}\n')
        (proj / "s2.jsonl").write_text('{"type":"user"}\n')
        old = time.time() - 4000
        os.utime(proj / "s2.jsonl", (old, old))                      # idle an hour, no subagents: not listed
        sub = proj / "s1" / "subagents"
        for aid, finished, age in (("a1", False, 0), ("a2", True, 0), ("a3", False, 1200)):
            (sub / f"agent-{aid}.meta.json").write_text(json.dumps({"agentType": "general-purpose", "description": f"job {aid}", "finished": finished}))
            (sub / f"agent-{aid}.jsonl").write_text('{"message":{"id":"m-' + aid + '","usage":{"input_tokens":5,"output_tokens":7}},"timestamp":"2026-11-16T08:00:00Z"}\n')
            if age:
                os.utime(sub / f"agent-{aid}.jsonl", (time.time() - age, time.time() - age))
        saved = so.HOME
        so.HOME = home
        try:
            cn = so.claude_now()
            self.assertEqual([x["name"] for x in cn["sessions"]], ["아이디어"])
            self.assertEqual([s["desc"] for s in cn["sessions"][0]["subs"]], ["job a1"], "finished and stale subagents are not running")
            self.assertIn("1 subagent(s): job a1", "\n".join(so.claude_now_lines(cn)))
            self.assertEqual((so.cols("아이디어"), so.fit("아이디어", 10), so.fit("아이디어", 5)), (8, "아이디어  ", "아이 "), "Korean takes two columns")
            cn["nodes"] = {"m1": {"running": 1, "claude": {"subagents": 2, "descs": ["build", "test"]}}, "m2": {"running": 0, "claude": {"subagents": 0}}}
            text = "\n".join(so.claude_now_lines(cn))
            self.assertIn("m1 (node)", text)
            self.assertIn("2 subagent(s): build · test", text)
            self.assertNotIn("m2 (node)", text, "an idle node is not listed")
            # node jobs: the whole run counts (modelUsage), not the last turn (usage)
            rep = {"usage": {"output_tokens": 9}, "modelUsage": {"a": {"outputTokens": 50, "inputTokens": 1}, "b": {"outputTokens": 2}},
                   "total_cost_usd": 1.5, "num_turns": 3, "subagent_stats": {"spawned": 4}}
            self.assertEqual(so.report_usage(rep)["usage"]["out"], 52)
            self.assertEqual((so.report_usage(rep)["subagents"], so.report_usage({"usage": {"output_tokens": 9}})["usage"]["out"]), (4, 9))
            lu = so.LocalUsage()
            lu.CACHE = TMP / "usage-cache-test.json"
            lu.offsets, lu.hours = {}, {}
            lu.refresh()
            self.assertEqual(lu.window(10 ** 9)["out"], 21, "subagent transcripts count toward tokens")
        finally:
            so.HOME = saved

    def test_98o_job_sent_back_to_the_same_node_is_not_deleted(self):
        # a job taken from a node (offline) and handed back to it must not be cancelled by the orphan clean-up of that tick
        cfg = so.load_cfg()
        for n in ("n1", "n2"):
            (HOMES[n] / ".local/bin/claude").write_text(STUB)
        jid = so.submit_job(cfg, "comes back", node="n1")
        OFFLINE.add("n1")
        so.tick()
        j = so.load_job(jid); j.update({"status": "dispatched", "node": "n1", "last_seen": so.now() - 3600}); so.save_job(j)
        so.tick()   # n1 offline: requeued, the old copy noted as an orphan
        self.assertIn(["n1", jid], so.read_json(so.STATE_DIR / "orphans.json", []))
        OFFLINE.discard("n1")
        so.tick()   # back online: handed to n1 again, the clean-up at the end of the tick must leave it alone
        self.assertEqual(so.load_job(jid)["status"], "dispatched")
        self.assertTrue((HOMES["n1"] / ".scaleout/jobs" / jid).exists())
        for _ in range(6):
            settle(); so.tick()
            if so.load_job(jid)["status"] == "done":
                break
        j = so.load_job(jid)
        self.assertEqual(j["status"], "done", j["history"])
        self.assertFalse(any("vanished" in h.get("why", "") for h in j["history"]), j["history"])
        # a node that lost a job is tried last for a while
        k = {"id": "jx", "node": "n2", "history": [], "attempts": 0}
        so.requeue(so.load_cfg(), k, "job vanished on n2")
        self.assertGreater(k["avoid"]["n2"], so.now())
        so.write_json(so.STATE_DIR / "orphans.json", [])

    def test_99e_linux_node_state_and_runner(self):
        # GNU stat: -f is "file system". The heartbeat age of a running job must still be a number on Linux.
        h = TMP / "linuxnode"; b = h / ".scaleout"; fb = TMP / "linuxbin"
        for d in (b / "bin", b / "jobs/jl", b / "queue", fb):
            d.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / "node/unix/ctl.sh", b / "bin/ctl")
        shutil.copy(ROOT / "node/unix/runner.sh", b / "bin/runner.sh")
        (b / "node.conf").write_text("RUNNER=manual\nDEVICE_SCAN=off\nLINGER_S=0\nKEEP_DEVICES=1\n")
        (fb / "uname").write_text('#!/bin/bash\ncase "$1" in -m) echo x86_64 ;; -r) echo 6.8 ;; *) echo Linux ;; esac\n')
        (fb / "stat").write_text('#!/bin/bash\nif [ "$1" = -f ]; then printf \'  File: "%s"\\n    ID: 1 Namelen: 255\\n\' "$3"; exit 1; fi\n'
                                 'python3 -c \'import os,sys; print(int(os.path.getmtime(sys.argv[1])))\' "$3"\n')
        for f in (fb / "uname", fb / "stat"):
            f.chmod(0o755)
        (b / "jobs/jl/started").write_text("1"); (b / "jobs/jl/heartbeat").write_text("")
        env = dict(os.environ, HOME=str(h), PATH=f"{fb}:/usr/bin:/bin:/usr/sbin:/sbin")
        r = subprocess.run(["bash", str(b / "bin/ctl"), "state", "jl"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        s = json.loads(r.stdout.decode().strip().splitlines()[-1])
        self.assertEqual(s["state"], "running")
        self.assertLess(s["heartbeat_age"], 60)
        # a runner started by systemd while another one holds the lock waits for it instead of quitting at once
        # the holder is a grandchild that init reaps: a child of this test would stay a zombie that `kill -0` still finds
        holder = subprocess.run(["bash", "-c", "sleep 3 >/dev/null 2>&1 & echo $!"], stdout=subprocess.PIPE).stdout.decode().strip()
        (b / "runner.lock").mkdir(); (b / "runner.lock/pid").write_text(holder)
        env2 = dict(os.environ, HOME=str(h))
        t0 = time.time()
        subprocess.run(["bash", str(b / "bin/runner.sh")], env=env2, timeout=30)
        self.assertLess(time.time() - t0, 2)   # outside systemd: quits at once, as before
        t0 = time.time()
        subprocess.run(["bash", str(b / "bin/runner.sh")], env=dict(env2, INVOCATION_ID="x"), timeout=30)
        self.assertGreaterEqual(time.time() - t0, 2)   # under systemd: waits for the holder, then runs and exits
        self.assertFalse((b / "runner.lock").exists())

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

    def test_99c_stale_tunnel_freed(self):
        # a node that slept leaves its reverse tunnel's sshd session holding the port: free it after a minute
        cfg = so.load_cfg()
        cfg["nodes"]["n3"] = {"os": "Darwin", "user": "me", "port": 22999, "max_jobs": 1, "agent": "claude"}
        so.save_cfg(cfg)
        freed = []
        orig = (so.Node.ctl, so.socket_busy, so.free_stale_tunnel)
        so.Node.ctl = lambda self, verb, *a, **k: subprocess.CompletedProcess([], 255, b"", b"Connection timed out during banner exchange")
        so.socket_busy = lambda port: True
        so.free_stale_tunnel = lambda port: freed.append(port) or [4242]
        try:
            st = so.refresh_metrics(so.load_cfg(), names=["n3"], max_age=0)["n3"]
            self.assertFalse(st["online"])
            self.assertEqual(freed, [], "not on the first miss: a busy node can be slow")
            self.assertIn("stale_tunnel_since", st)
            st["stale_tunnel_since"] = so.now() - 61
            so.write_json(so.node_state_path("n3"), st)
            st = so.refresh_metrics(so.load_cfg(), names=["n3"], max_age=0)["n3"]
            self.assertEqual(freed, [22999])
            self.assertNotIn("stale_tunnel_since", st)
            # no route works at all (ctl answers "unreachable" without trying ssh): the same stale tunnel
            so.Node.ctl = lambda self, verb, *a, **k: subprocess.CompletedProcess([], 255, b"", b"unreachable")
            so.refresh_metrics(so.load_cfg(), names=["n3"], max_age=0)
            st = so.read_json(so.node_state_path("n3"), {})
            st["stale_tunnel_since"] = so.now() - 61
            so.write_json(so.node_state_path("n3"), st)
            so.refresh_metrics(so.load_cfg(), names=["n3"], max_age=0)
            self.assertEqual(freed, [22999, 22999])
            # an unreachable node with no tunnel listener is just offline: nothing to free
            so.Node.ctl = lambda self, verb, *a, **k: subprocess.CompletedProcess([], 255, b"", b"Connection refused")
            st = so.refresh_metrics(so.load_cfg(), names=["n3"], max_age=0)["n3"]
            self.assertNotIn("stale_tunnel_since", st)
        finally:
            so.Node.ctl, so.socket_busy, so.free_stale_tunnel = orig
            cfg = so.load_cfg()
            cfg["nodes"].pop("n3", None)
            so.save_cfg(cfg)
        # the real helper never kills a program that is not an sshd session
        import socket
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        try:
            self.assertEqual(so.free_stale_tunnel(srv.getsockname()[1]), [])
        finally:
            srv.close()

    def test_99d_idle_work_trees_reclaimed(self):
        # repo work trees keep build caches between jobs; idle ones go (least recently used first), busy ones stay
        home = TMP / "reclaim-home"
        (home / ".scaleout/bin").mkdir(parents=True)
        ctl = home / ".scaleout/bin/ctl"
        shutil.copy(ROOT / "node/unix/ctl.sh", ctl)
        ctl.chmod(0o755)
        env = {k: v for k, v in os.environ.items() if k != "SCALEOUT_NODE_HOME"}
        env["HOME"] = str(home)   # cleanup empties caches under $HOME: only the temp one
        c = lambda *a: subprocess.run([str(ctl), *a], env=env, check=True, stdout=subprocess.PIPE).stdout.decode().strip()
        src = TMP / "reclaim-src"
        src.mkdir()
        g = lambda *a: subprocess.run(["git", "-C", str(src), *a], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.decode().strip()
        g("init", "-q"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
        (src / "a.txt").write_text("one\n"); g("add", "a.txt"); g("commit", "-qm", "init")
        bare = c("repo-init", "r")
        g("push", "-q", bare, "HEAD:refs/scaleout/base")
        sha = g("rev-parse", "HEAD")
        w1, w2, w3 = (c("repo-checkout", "r", sha) for _ in range(3))
        self.assertEqual([Path(w).name for w in (w1, w2, w3)], ["r-wt1", "r-wt2", "r-wt3"])
        c("repo-unlock", w1); c("repo-unlock", w2)          # w3: a job still works in it
        old = time.time() - 5 * 86400
        os.utime(w1, (old, old))
        self.assertIn("work trees 1", c("gc", "3"))          # idle 5 days: gone; idle today: kept for its build cache
        self.assertFalse(Path(w1).exists())
        self.assertTrue(Path(w2).exists() and Path(w3).exists())
        self.assertIn("work trees dropped 1", c("cleanup", "", "999999"))   # short of disk: every idle one goes
        self.assertFalse(Path(w2).exists())
        self.assertTrue(Path(w3).exists(), "never the work tree of a running job")
        stale = time.time() - 13 * 3600                       # a crashed job's lock is stale after 12 hours
        os.utime(w3 + ".lock", (stale, stale))
        self.assertIn("work trees dropped 1", c("cleanup", "", "999999"))
        self.assertEqual(list((home / ".scaleout/repos").iterdir()), [Path(bare)])
        wl = subprocess.run(["git", "-C", bare, "worktree", "list"], check=True, stdout=subprocess.PIPE).stdout.decode()
        self.assertEqual(wl.count("\n"), 1, wl)
        self.assertEqual(Path(c("repo-checkout", "r", sha)).name, "r-wt1", "the next job makes its work tree again")



class DispatchFailure(unittest.TestCase):
    """A node has room but the hand-over fails: the job must say why instead of waiting with nothing shown."""

    def test_error_line_picks_the_git_reason(self):
        text = "To ssh://node/repo.git\n ! [remote rejected]   abc -> refs/scaleout/base/j1 (shallow update not allowed)\nerror: failed to push some refs to 'ssh://node/repo.git'"
        self.assertIn("failed to push", so.dispatch_error_line(text))
        self.assertEqual(so.dispatch_error_line(""), "")
        self.assertLessEqual(len(so.dispatch_error_line("fatal: " + "x" * 500)), 160)

    def test_tick_records_why_the_hand_over_failed(self):
        job = {"id": "j-test", "status": "queued", "history": []}
        saved = []
        real = (so.TickLock, so.load_cfg, so.refresh_metrics, so.all_jobs, so.pick_node, so.dispatch, so.save_job, so.maintain)

        class Lock:
            def __enter__(self): return True
            def __exit__(self, *a): return False

        def failing(cfg, j, name):
            j["dispatch_error"] = "could not send the repository: shallow update not allowed"
            return False

        so.TickLock, so.load_cfg, so.refresh_metrics = Lock, (lambda: {"nodes": {}}), (lambda cfg: {})
        so.all_jobs, so.pick_node, so.dispatch = (lambda: [job]), (lambda *a, **k: "n1"), failing
        so.save_job, so.maintain = (lambda j: saved.append(dict(j))), (lambda cfg, metrics: None)
        try:
            so.tick()
        finally:
            (so.TickLock, so.load_cfg, so.refresh_metrics, so.all_jobs, so.pick_node, so.dispatch, so.save_job, so.maintain) = real
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["wait"], "n1: could not send the repository: shallow update not allowed")
        self.assertNotIn("dispatch_error", job)
        self.assertTrue(saved)

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
