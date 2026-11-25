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

    def test_98p_platform_speed_and_disk(self):
        """Android and any-machine work goes to a non-Mac first and iOS work to a Mac; the faster machine wins a tie; a busy
        node whose disk the job would push under its floor is skipped."""
        cfg = so.load_cfg()
        cfg["nodes"] = {"mac": {"max_jobs": 2, "agent": "claude"}, "pc": {"max_jobs": 2, "agent": "claude"}}
        base = {"online": True, "ts": so.now(), "mem_free_pct": 80, "mem_total_gb": 32, "disk_free_gb": 500, "load": 0.1, "cpus": 8,
                "agents": ["claude"], "runner": True}
        m = {"mac": dict(base, os="Darwin", caps=["emulator", "simulator", "ios-simulator"]), "pc": dict(base, os="Linux", caps=["emulator"])}
        pick = lambda j, load=None, why=None, disk=None: so.pick_node(cfg, j, m, load or {}, {}, {}, {}, why if why is not None else {}, disk)
        self.assertEqual(pick({"tags": ["emulator"], "agent": "default", "id": "p1"}), "pc")
        self.assertEqual(pick({"tags": [], "agent": "default", "id": "p2"}), "pc", "any machine: keep the Mac free")
        (so.STATE_DIR / "prompts").mkdir(parents=True, exist_ok=True)
        (so.STATE_DIR / "prompts" / "p3.md").write_text("iOS 시뮬레이터에서 xcodebuild test")
        (so.STATE_DIR / "prompts" / "p4.md").write_text("flutter E2E on the Android emulator and the iOS simulator")
        (so.STATE_DIR / "prompts" / "p5.md").write_text("안드로이드 에뮬레이터로 maestro")
        self.assertEqual([so.job_platform({"tags": [], "id": f"p{k}"}) for k in (3, 4, 5)], ["ios", "", "android"])
        self.assertEqual(so.job_platform({"tags": ["mac"], "id": "p5"}), "ios", "tags win over the prompt")
        self.assertEqual(pick({"tags": [], "agent": "default", "id": "p3"}), "mac", "untagged iOS work leans to the Mac")
        self.assertEqual(pick({"tags": [], "agent": "default", "id": "p6"}, load={"pc": 2}), "mac", "the Mac takes overflow")
        # the faster machine (bench, or its thread count) wins when both are equally free
        m2 = {"slow": dict(base, os="Linux"), "fast": dict(base, os="Linux", cpus=16)}
        cfg["nodes"] = {"slow": {"max_jobs": 2, "agent": "claude"}, "fast": {"max_jobs": 2, "agent": "claude"}}
        self.assertEqual(so.pick_node(cfg, {"tags": [], "agent": "default", "id": "p7"}, m2, {}), "fast")
        # disk: a heavy job needs ~8 GB on top of the 5 GB floor
        cfg["nodes"] = {"mac": {"max_jobs": 2, "agent": "claude"}, "pc": {"max_jobs": 8, "agent": "claude"}}
        cfg["defaults"]["min_free_disk_gb"] = 5   # the shipped floor
        heavy = {"tags": [], "agent": "default", "id": "p8", "cpu": 2}
        self.assertEqual(so.job_disk_gb(heavy), 4.0)
        m["pc"]["disk_free_gb"] = 8
        why = {}
        self.assertEqual(pick(heavy, load={"pc": 1}, why=why), "mac")
        self.assertIn("disk 8 GB free, job needs 4", why["pc"])
        m["pc"]["disk_free_gb"] = 500
        self.assertEqual(pick(heavy, load={"pc": 1}, disk={"pc": 492}), "mac", "disk of jobs just handed out counts")
        # a 22 GB laptop running one emulator job still takes a second one
        emu = {"tags": ["emulator"], "agent": "default", "id": "p9"}
        m["pc"]["disk_free_gb"] = 22
        self.assertEqual(so.pick_node(cfg, emu, m, {"pc": 1}, {}, {}, {}, {}, {"pc": so.job_disk_gb(emu)}), "pc")
        m["pc"]["disk_free_gb"] = 500
        self.assertEqual(pick(heavy, load={"pc": 1}), "pc")

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

    def test_99i_gpu_and_swap(self):
        """The GPU column: a card with its own memory, one that shares it, none; what `ctl metrics` reports."""
        nv = {"gpu": {"util": 45, "mem_used_mb": 300, "mem_total_mb": 8192, "temp": 50, "name": "GTX"}}
        self.assertEqual((so.gpu_of(nv), so.fmt_gpu(nv), so.fmt_vram(nv)), (45, "45%", "0.3/8G"))
        self.assertEqual((so.fmt_gpu({"gpu": {"util": 3}}), so.fmt_vram({"gpu": {"util": 3}})), ("3%", ""))
        for none in ({}, {"gpu": None}, {"gpu": {}}, {"gpu": {"util": "x"}}, {"gpu": "x"}):
            self.assertEqual(so.fmt_gpu(none), "-")
        ctl = str(Path(so.__file__).resolve().parents[1] / "node" / "unix" / "ctl.sh")
        out = subprocess.run(["bash", ctl, "metrics"], capture_output=True, text=True, timeout=60).stdout
        m = json.loads(out)
        self.assertIn("gpu", m)
        self.assertGreaterEqual(float(m["swap_gb"]), 0)

    def test_99j_machine_guide(self):
        """A machine's guide: what a job is told depends on the task and on what the machine has at that moment."""
        cfg = so.load_cfg()
        text = ("# title\n\nA card.\n\n## Emulator\n<!-- needs: gpu -->\n<!-- when: emulator|apk -->\nUse -gpu host.\n\n"
                "## Blender\n<!-- when: blender -->\nUse CUDA.\n\n## Always\nBe kind.\n")
        up, down = {"os": "Linux", "caps": ["gpu"]}, {"os": "Linux", "caps": []}
        so.GUIDES.mkdir(parents=True, exist_ok=True)
        f = so.GUIDES / "n1.md"
        try:
            self.assertEqual(so.guide_for(cfg, "n1", "x", up), ("", []))
            f.write_text(text)
            about, titles = so.guide_for(cfg, "n1", "Build the APK.", up, "~/g.md")
            self.assertEqual(titles, ["Emulator", "Always"])
            self.assertIn("A card.", about); self.assertIn("Use -gpu host.", about); self.assertNotIn("<!--", about); self.assertNotIn("# title", about)
            self.assertNotIn("CUDA", about); self.assertIn("~/g.md, read it before you do one of these: Blender", about)
            for other in ("Run the tests. Do not start an emulator.", "에뮬레이터 없이 돌린다. Start no emulator: 쓰지 않는다.", "Read the capkapk file."):
                self.assertEqual(so.guide_for(cfg, "n1", other, up, "~/g.md")[1], ["Always"], other)
            about, titles = so.guide_for(cfg, "n1", "Build the APK.", down, "~/g.md")
            self.assertEqual(titles, ["Always"])
            self.assertIn("Emulator (no gpu)", about); self.assertNotIn("-gpu host", about)
            self.assertEqual(so.guide_for(cfg, "n1", "Run the unit tests.", up)[1], ["Emulator", "Blender", "Always"], "no copy on the machine: all of it")
            f.write_text("<!-- needs: gpu -->\nA card.\n")
            self.assertEqual(so.guide_for(cfg, "n1", "x", down)[0], "")
            self.assertIn("A card.", so.guide_for(cfg, "n1", "x", up)[0])
            f.write_text(text)
            rules = so.guide_rules(cfg)
            self.assertIn("(`n1`): A card. Sections: Emulator (needs gpu", rules); self.assertNotIn("CUDA", rules)
            # a real job: its task carries what fits, the whole guide is on the node, the text scaleout adds does not count as the task
            f.write_text(text.replace("needs: gpu", "needs: n1"))
            for task, want in (("Build the apk.", True), ("Say hello.", False)):
                jid = so.submit_job(cfg, task, node="n1", pin=True)
                so.tick(); settle(); so.tick()
                j = so.load_job(jid)
                told = (HOMES["n1"] / ".scaleout/jobs" / jid / "prompt.md").read_text()
                self.assertEqual(("Use -gpu host." in told, "Be kind." in told), (want, True))
                self.assertEqual(j["guide"], ["Emulator", "Always"] if want else ["Always"])
            self.assertIn("Use CUDA.", (HOMES["n1"] / ".scaleout/guide.md").read_text())
            so.cmd_guide(argparse.Namespace(node="n1", file=None, task=None, off=True))
            self.assertFalse(f.exists()); self.assertFalse((HOMES["n1"] / ".scaleout/guide.md").exists())
        finally:
            if f.exists():
                f.unlink()

    def test_99k_guide_routing(self):
        """A guide can say which work goes to its machine; a full graphics card takes no more of it."""
        cfg = {"nodes": {"pc": {"os": "Linux", "user": "u", "max_jobs": 8, "cpu_slots": 8}, "mac": {"os": "Darwin", "user": "u", "max_jobs": 8, "cpu_slots": 8}},
               "defaults": {**so.load_cfg()["defaults"]}}
        st = lambda **k: {"online": True, "os": "Linux", "cpus": 16, "mem_total_gb": 64, "mem_free_pct": 90, "disk_free_gb": 500, "load": 1, "agents": ["claude"], "paused": False,
                          "agent_ok": True, "ts": so.now(), "caps": ["gpu"], "gpu": {"util": 10, "mem_used_mb": 500, "mem_total_mb": 8192}, **k}
        m = {"pc": st(), "mac": st(os="Darwin", caps=[], gpu=None, load=0)}
        f = so.GUIDES / "pc.md"
        f.parent.mkdir(parents=True, exist_ok=True)
        ids = []

        def job(text, ago=0, **kw):
            jid = so.submit_job(so.load_cfg(), text); ids.append(jid)
            return {**so.load_job(jid), "created": so.now() - ago * 60, **kw}

        def pick(j, load=None, why=None):
            return so.pick_node(cfg, j, m, load or {}, {}, {}, {}, why if why is not None else {})
        try:
            godot, plain = job("Run the Godot captures."), job("Run the unit tests. Do not start Godot.")
            f.write_text("x\n\n## Godot\n<!-- needs: gpu -->\n<!-- here: godot -->\nUse it.\n")
            self.assertEqual(pick(godot), "pc")
            self.assertEqual(pick(godot, {"pc": 4}), "pc", "also when the other one is empty")
            why = {}
            m["pc"]["load"] = 100000
            self.assertIsNone(pick(godot, {"pc": 4}, why)); self.assertIn("goes to pc", why["mac"])
            self.assertIsNone(pick({**godot, "created": so.now() - 86400}), "no wait given: only there")
            self.assertEqual(pick(plain), "mac")
            self.assertEqual(pick({**godot, "want_node": "mac"}), "mac"); self.assertEqual(pick({**godot, "only": ["mac"]}), "mac"); self.assertEqual(pick({**godot, "tags": ["mac"]}), "mac")
            f.write_text("x\n\n## Godot\n<!-- needs: gpu -->\n<!-- here: godot -->\n<!-- wait: 20 -->\nUse it.\n")
            self.assertIsNone(pick({**godot, "created": so.now() - 600})); self.assertEqual(pick({**godot, "created": so.now() - 1500}), "mac")
            m["pc"]["load"] = 1
            self.assertEqual(pick({**godot, "created": so.now() - 1500}, {"pc": 2}), "pc", "still first")
            # a full card
            for g in ({"util": 99, "mem_used_mb": 500, "mem_total_mb": 8192}, {"util": 5, "mem_used_mb": 7900, "mem_total_mb": 8192}):
                m["pc"]["gpu"] = g
                why = {}
                self.assertIsNone(pick(godot, {"pc": 1}, why)); self.assertIn("GPU full", why["pc"])
                self.assertEqual(pick(godot), "pc", "an idle machine takes one")
                self.assertEqual(pick({**plain, "only": ["pc"]}, {"pc": 1}), "pc", "work that does not use the card")
                self.assertIsNone(pick({**plain, "only": ["pc"], "tags": ["gpu"]}, {"pc": 1}))
        finally:
            f.unlink()
            for i in ids:
                so.job_path(i).unlink()

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

    def test_99h_slow_metrics_is_busy(self):
        """A machine that answered a moment ago but is too loaded to finish `ctl metrics` in time is busy, not offline: it
        takes no new job and keeps its running ones; a node that stays silent past the grace goes offline as before."""
        cfg = so.load_cfg()
        cfg["defaults"]["offline_grace_min"] = 10   # the shipped grace (the tests run with a shorter one)
        name = next(n for n in cfg["nodes"] if n in HOMES)
        so.refresh_metrics(cfg, [name], max_age=0)
        keep = so.Node.ctl
        so.Node.ctl = lambda self, verb, *a, **k: subprocess.CompletedProcess([], 124, b"", b"timeout") if verb == "metrics" else keep(self, verb, *a, **k)
        try:
            st = so.refresh_metrics(cfg, [name], max_age=0)[name]
            self.assertTrue(st["online"], st)
            self.assertEqual(so.node_health(cfg, name, st), "busy")
            st["last_ok"] = so.now() - 3600
            so.write_json(so.node_state_path(name), st)
            self.assertFalse(so.refresh_metrics(cfg, [name], max_age=0)[name]["online"], "silent past the grace: offline")
        finally:
            so.Node.ctl = keep
        st = so.refresh_metrics(cfg, [name], max_age=0)[name]
        self.assertEqual(so.node_health(cfg, name, st), "ok", "answers in time again: back to ok")

    def test_99d_check_model(self):
        """Jobs that only check and report go to check_model; what makes or judges stays. Their failures get counted, real or not."""
        yes = ("앱 E2E(Maestro)를 iOS 시뮬레이터에서 dev 로 돌리고 결과만 보고해 주세요. 앱 코드 · 흐름은 고치지 마세요(실패하면 원인과 스크린샷만).",
               "안드로이드 에뮬레이터 E2E 를 돌려 줘. 앱 코드는 고치지 말고, 실패하면 원인(앱 · 시험 · 환경)을 갈라 보고만 해.",
               "Run the full test suite in web/. Do not change code, report only.", "# 전체 테스트 · arm64 APK\n코드는 고치지 않는다.",
               "# 통합 E2E — iOS 전체 시나리오\n흐름(yaml)은 고쳐도 된다. 앱 코드(app/lib) · 서버는 고치지 않는다.",
               "에뮬레이터 E2E 를 돌려 줘. 앱 코드는 고치지 말고 보고만 해. 작업 사본은 브랜치 fix/notify 다.")
        no = ("게임별 주간 순위를 만들어 주세요. 서버 코드는 고치지 마세요. 테스트도 돌립니다.", "# AI 플레이테스트\n에뮬레이터에서 해 보고 평가한다. 코드는 고치지 않는다.",
              "폰에 새 게임이 있다. 아무 설명도 듣지 못했다. 에뮬레이터에서 해 보고 솔직한 소감. 코드는 고치지 않는다.",
              "E2E 를 돌리고 실패한 것은 고쳐 주세요.", "Review the tests in api/. Do not change code.", "에뮬레이터에서 돌려 본 뒤 남은 것을 고치는 일입니다. 서버 코드는 고치지 마세요.",
              "# E2E 도구 마무리 · 충돌 풀기\n앱 코드는 고치지 않는다.", "E2E 시나리오를 돌려 통과시켜 주세요.")
        for x in yes:
            self.assertTrue(so.is_check(x), x)
        for x in no:
            self.assertFalse(so.is_check(x), x)
        cfg = so.load_cfg()
        self.assertEqual(so.pick_model(cfg, yes[0], "default", "", "auto", False), ("", ""), "off until check_model is set")
        cfg["defaults"]["check_model"] = "claude-sonnet-5-5"
        pick = lambda p, agent="default", resume="", model="auto", check=False: so.pick_model(cfg, p, agent, resume, model, check)
        self.assertEqual(pick(yes[0]), ("claude-sonnet-5-5", "check"))
        self.assertEqual(pick(no[0]), ("", ""))
        self.assertEqual(pick(no[3], check=True), ("claude-sonnet-5-5", "check"))
        self.assertEqual(pick(yes[0], model="default"), ("", ""))
        self.assertEqual(pick(no[0], model="opus; ls"), ("opusls", "asked"))
        self.assertEqual(pick(yes[0], resume="abc"), ("", ""))
        self.assertEqual(pick(yes[0], agent="codex"), ("", ""))
        self.assertEqual(so.CHECK_LINE.findall("끝\n`SCALEOUT-CHECK: pass=12 fail=1 blocked=0`"), [("12", "1", "0")])
        jid = so.submit_job(so.load_cfg(), yes[0], node="n1", agent="default", model="auto")
        j = so.load_job(jid)
        self.assertNotIn("model", j)
        j.update({"model": "claude-sonnet-5-5", "model_why": "check", "status": "done", "finished_at": so.now(), "cost_usd": 0.5,
                  "check": {"pass": 3, "fail": 2, "blocked": 0}, "node": None})
        so.save_job(j)
        self.assertIn(f"scaleout check {jid} real=<n> false=<n>", so.check_text(j))
        so.cmd_check(argparse.Namespace(job=jid, counts=["real=1", "false=1"], days=14))
        r = so.check_rows(14)["claude-sonnet-5-5"]
        self.assertEqual((r["jobs"], r["fail"], r["real"], r["false"], r["open"]), (1, 2, 1, 1, 0))
        self.assertIn("real=1 false=1", so.check_text(so.load_job(jid)))
        with self.assertRaises(SystemExit):
            so.cmd_check(argparse.Namespace(job=jid, counts=["maybe=1"], days=14))
        self.assertEqual(so.job_answer(b'{"result":"x\\nSCALEOUT-CHECK: pass=1 fail=0 blocked=2"}').splitlines()[-1], "SCALEOUT-CHECK: pass=1 fail=0 blocked=2")
        self.assertEqual((so.pick_effort(cfg), so.pick_effort(cfg, "xhigh")), ("high", "xhigh"))
        with self.assertRaises(SystemExit):
            so.pick_effort(cfg, "huge")
        self.assertEqual(so.check_name({"model": "m", "effort": "high"}), "m (high)")
        ej = so.load_job(so.submit_job(cfg, yes[0], node="n1"))
        self.assertEqual((ej["model"], ej["model_why"], ej["effort"]), ("claude-sonnet-5-5", "check", "high"), "never without an effort")
        so.cmd_cancel(argparse.Namespace(job=ej["id"]))
        pj = so.load_job(so.submit_job(cfg, no[0], node="n1"))
        self.assertEqual((pj.get("model"), pj.get("effort")), (None, None), "the node's own model keeps its own effort")
        so.cmd_cancel(argparse.Namespace(job=pj["id"]))
        self.assertIn('--effort $effort', (ROOT / "node/unix/runner.sh").read_text())
        self.assertIn('--model $model', (ROOT / "node/unix/runner.sh").read_text())
        self.assertIn("MODEL=%s\\nEFFORT=%s", (ROOT / "node/unix/ctl.sh").read_text())

    def test_99h_unpin_and_hold(self):
        """A -w job that waited long enough and names nothing of its machine may go elsewhere; a machine on hold takes nothing."""
        cfg = so.load_cfg()
        keep = cfg["defaults"].get("unpin_min")
        cfg["defaults"]["unpin_min"] = 10
        ids = []
        def job(text, ago=15, **kw):
            jid = so.submit_job(cfg, text, node="n1", **kw)
            j = so.load_job(jid); j["created"] = so.now() - ago * 60
            j["repo"] = j.get("repo") or "/r"; so.save_job(j); ids.append(jid)
            return j
        try:
            self.assertTrue(so.may_move(cfg, job("Run the unit tests of app/ and report.")))
            self.assertTrue(so.may_move(cfg, job("전체 시험을 돌린다. 에뮬레이터 · 시뮬레이터 · apk 빌드는 하지 않는다(위젯 시험만). 커밋 금지.")))
            self.assertFalse(so.may_move(cfg, job("Run the unit tests.", ago=5)), "not yet")
            self.assertFalse(so.may_move(cfg, job("에뮬레이터를 띄워 화면을 찍는다.")))
            self.assertFalse(so.may_move(cfg, job("이 기계에서 걸리는 시간을 잰다.")))
            self.assertFalse(so.may_move(cfg, job("Files are in /home/me/out.")))
            self.assertFalse(so.may_move(cfg, job("Compare n1 with the rest of the macbook runs.")))
            self.assertFalse(so.may_move(cfg, job("Run the unit tests.", pin=True)), "--pin")
            self.assertFalse(so.may_move(cfg, job("Run the unit tests.", workdir="/w")), "a folder on that machine")
            j = job("Run the unit tests."); j["want_node"] = None
            self.assertFalse(so.may_move(cfg, j), "no pin, nothing to undo")
            j = job("Run the unit tests."); j["repo"] = ""
            self.assertFalse(so.may_move(cfg, j), "it works on what that machine has")
            cfg["defaults"]["unpin_min"] = 0
            self.assertFalse(so.may_move(cfg, job("Run the unit tests.")), "off")
            cfg["defaults"]["unpin_min"] = 10
            m = {n: {"online": True, "ts": so.now(), "mem_free_pct": 80, "mem_total_gb": 64, "disk_free_gb": 50, "load": 0.1, "cpus": 16,
                     "agents": ["claude"], "runner": True} for n in ("n1", "n2")}
            j = {"tags": [], "agent": "default", "id": "h1", "want_node": "n1"}
            self.assertEqual(so.pick_node(cfg, j, m, {}, {}, {}, {}, {}), "n1")
            cfg["nodes"]["n1"].update(hold_from=so.now() - 5, hold_until=so.now() + 600)
            why = {}
            self.assertIsNone(so.pick_node(cfg, j, m, {}, {}, {}, {}, why))
            self.assertIn("on hold until", why["n1"])
            self.assertEqual(so.pick_node(cfg, {**j, "want_node": None, "only": ["n1", "n2"]}, m, {}, {}, {}, {}, {}), "n2")
            cfg["nodes"]["n1"]["hold_from"] = so.now() + 300
            self.assertEqual(so.pick_node(cfg, j, m, {}, {}, {}, {}, {}), "n1", "the hold has not begun")
            lt = __import__("time").localtime(so.clock_time("07:30"))
            self.assertEqual((lt.tm_hour, lt.tm_min), (7, 30))
            self.assertGreater(so.clock_time("07:30"), so.now())
            lt = __import__("time").localtime(so.clock_time("11-25 09:00"))
            self.assertEqual((lt.tm_mon, lt.tm_mday, lt.tm_hour), (11, 25, 9))
        finally:
            for i in ids:
                (so.STATE_DIR / "jobs" / f"{i}.json").unlink()
            cfg["defaults"]["unpin_min"] = keep

    def test_99g_own_memory(self):
        """Once a node measures a job's own processes, what the whole machine grew by no longer decides what a job is reserved."""
        cfg = so.load_cfg()
        ids = []
        def done(mb, own):
            j = so.load_job(so.submit_job(cfg, "memory sample", node="n2"))
            j.update({"status": "done", "node": "n2", "mem_mb": mb, "finished_at": so.now(), **({"mem_own": True} if own else {})})
            so.save_job(j); ids.append(j["id"])
        try:
            for _ in range(4):
                done(14 * 1024, False)
            so._LEARNED["t"] = 0
            self.assertGreaterEqual(so.learned_mem("n2", "default"), 14)
            for _ in range(2):
                done(1500, True)
            so._LEARNED["t"] = 0
            self.assertIsNone(so.learned_mem("n2", "default"), "too few of the right kind: the default, not the old samples")
            done(2048, True)
            so._LEARNED["t"] = 0
            self.assertEqual(so.learned_mem("n2", "default"), 2.0)
        finally:
            for i in ids:
                (so.STATE_DIR / "jobs" / f"{i}.json").unlink()
            so._LEARNED["t"] = 0
        ids.clear()
        cfg = so.load_cfg(); was = cfg["nodes"]["n2"].get("os"); cfg["nodes"]["n2"]["os"] = "Linux"; so.save_cfg(cfg)
        try:
            for _ in range(4):
                done(14 * 1024, False)
            so._LEARNED["t"] = 0
            self.assertIsNone(so.learned_mem("n2", "default"), "a Linux node drops the old samples at once")
            self.assertEqual(so.job_mem_gb({"id": "x", "tags": []}, "n2"), so.DEFAULT_GB + 3)
            for _ in range(3):
                done(700, True)
            so._LEARNED["t"] = 0
            self.assertEqual(so.job_mem_gb({"id": "x", "tags": []}, "n2"), so.DEFAULT_GB, "never below the default")
        finally:
            for i in ids:
                (so.STATE_DIR / "jobs" / f"{i}.json").unlink()
            cfg = so.load_cfg(); cfg["nodes"]["n2"]["os"] = was; so.save_cfg(cfg); so._LEARNED["t"] = 0
        ctl = (ROOT / "node/unix/ctl.sh").read_text()
        self.assertIn('own_peak "$1"; printf', ctl)
        d = so.DEFAULTS if hasattr(so, "DEFAULTS") else None
        if d:
            self.assertGreater(d["cleanup_below_disk_gb"], d["min_free_disk_gb"] + 4)

    def test_99e_whatif_compactions(self):
        """A session that compacted at its own window is not free at a larger one: it goes on and compacts there later."""
        run = lambda n, start, step: [start + i * step for i in range(n)]
        ctx = run(27, 60000, 10000) + run(27, 60000, 10000) + run(27, 60000, 10000)     # compacted twice, at 320k
        reqs = {"s": [(str(i), c) for i, c in enumerate(ctx)]}
        small, big = so.whatif(reqs, 250000, 60000), so.whatif(reqs, 400000, 60000)
        self.assertGreaterEqual(big[1], 2, "the 780k it grew do not fit in one 400k window")
        self.assertGreater(small[1], big[1])
        self.assertGreater(big[0], small[0], "a larger window reads more on every step")

    def test_99f_job_skills(self):
        """With token saving on, a job loads no skill list unless its task names a skill or is about documents or charts."""
        src = (ROOT / "node/unix/runner.sh").read_text()
        fn = src[src.index("skills_off() {"):src.index("run_job() {")]
        home = TMP / "skills-home"
        (home / ".claude/skills/code-gen").mkdir(parents=True, exist_ok=True)
        (home / ".claude/skills/synced").mkdir(exist_ok=True)
        prompt = TMP / "skills-prompt.md"

        def off(text, **env):
            prompt.write_text(text)
            r = subprocess.run(["bash", "-c", fn + f'\nskills_off "{prompt}"'], env={"PATH": os.environ["PATH"], "HOME": str(home), "TOKEN_SAVING": "1", **env})
            return r.returncode == 0

        self.assertTrue(off("앱 화면을 고치고 테스트를 돌려 보고해 주세요."))
        self.assertTrue(off("Fix the login test. Files are synced already."))   # `synced` is a folder of skills, not a skill
        for text in ("code-gen 으로 화면을 만들어 주세요", "e2e 스킬대로", "use the review Skill", "지표 차트를 그려", "report.pdf 로 저장"):
            self.assertFalse(off(text), text)
        self.assertFalse(off("앱 화면을 고쳐 주세요.", TOKEN_SAVING="0"))
        self.assertFalse(off("앱 화면을 고쳐 주세요.", JOB_SKILLS="on"))
        self.assertIn('skills_off "$d/prompt.md" && cmd="$cmd --disable-slash-commands"', src)

    def test_99g_token_saving(self):
        """scaleout tokens: the window goes into Claude's settings here and on the nodes (node.conf too, so jobs get the
        rules); before a compaction a ledger is taken from the transcript, after it the notes and ledger come back word for
        word; near the window the agent is told once to write its notes; a fork from a large session is refused; off undoes it."""
        so.CLAUDE_SETTINGS = TMP / "claude-home/settings.json"
        so.CLAUDE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        so.CLAUDE_SETTINGS.write_text(json.dumps({"model": "opus", "env": {"KEEP": "1"}}))
        nodes = [n for n in so.load_cfg()["nodes"] if n in HOMES]
        so.cmd_tokens(argparse.Namespace(action="on", window="250k", auto=None, fork_max=None))
        s = json.loads(so.CLAUDE_SETTINGS.read_text())
        self.assertEqual((s["model"], s["env"]), ("opus", {"KEEP": "1", "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "250000"}))
        for name in nodes:
            self.assertIn("TOKEN_SAVING=1\nCOMPACT_WINDOW=250000", (HOMES[name] / ".scaleout/node.conf").read_text())
            self.assertEqual(json.loads((HOMES[name] / ".claude/settings.json").read_text())["env"][so.WINDOW_ENV], "250000")
        # a session transcript at 150k
        proj = TMP / "tok-proj"
        (proj / ".git/info").mkdir(parents=True)
        tr = TMP / "tok-transcript.jsonl"
        rows = [{"type": "user", "timestamp": "2026-11-23T01:00:00Z", "message": {"role": "user", "content": "앱 화면 고쳐줘, 버튼은 노랑으로"}},
                {"type": "assistant", "timestamp": "2026-11-23T01:01:00Z", "message": {"id": "m1", "usage": {"input_tokens": 10, "cache_read_input_tokens": 150000},
                 "content": [{"type": "tool_use", "name": "Edit", "input": {"file_path": "/p/a.dart"}},
                             {"type": "tool_use", "name": "Bash", "input": {"command": "scaleout wait j20261123-010203-abcd -m 0"}},
                             {"type": "tool_use", "name": "TaskCreate", "input": {"subject": "버튼 색 바꾸기"}}]}},
                {"type": "user", "isCompactSummary": True, "timestamp": "2026-11-23T01:02:00Z", "message": {"role": "user", "content": "1. Primary Request and Intent"}},
                {"type": "user", "timestamp": "2026-11-23T01:02:00Z", "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}}]
        tr.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in rows) + "\n")
        env = dict(os.environ, CLAUDE_PROJECT_DIR=str(proj))
        hook = lambda ev, payload: subprocess.run([sys.executable, str(ROOT / "orchestrator/scaleout.py"), "hook", ev], env=env,
                                                  input=json.dumps(payload).encode(), stdout=subprocess.PIPE, check=True).stdout.decode()
        self.assertEqual(so.transcript_context(str(tr)), 150010)
        ses = {"transcript_path": str(tr), "session_id": "s1s1s1s1-0000"}
        ctx_of = lambda out: json.loads(out)["hookSpecificOutput"]["additionalContext"]
        hook("precompact", dict(ses, trigger="auto"))
        ledger = (proj / ".scaleout/ledger-s1s1s1s1.md").read_text()
        for x in ("버튼은 노랑으로", "/p/a.dart", "j20261123-010203-abcd", "[pending] 버튼 색 바꾸기", "context 150k"):
            self.assertIn(x, ledger)
        self.assertNotIn("tool_result", ledger)
        self.assertNotIn("Primary Request", ledger, "a compaction's summary is not the user's words")
        self.assertIn(".scaleout/", (proj / ".git/info/exclude").read_text(), "kept out of git without touching .gitignore")
        notes = proj / ".scaleout/notes-s1s1s1s1.md"
        notes.write_text("결정: 노랑 버튼 (사용자)\n")
        # after a compaction each part comes back through a hook of its own, none longer than Claude Code takes from one hook
        rules = ctx_of(hook("session", dict(ses, source="compact")))
        for x in (f"{(250000 - so.RESERVE) // 1000}k tokens", str(notes), str(proj / ".scaleout/ledger-s1s1s1s1.md")):
            self.assertIn(x, rules)
        self.assertNotIn("결정: 노랑 버튼", rules)
        self.assertIn(f"read them now: {notes}, {proj / '.scaleout/ledger-s1s1s1s1.md'}", rules, "a session without the new hooks reads them itself")
        self.assertNotIn("read them now", ctx_of(hook("session", dict(ses, source="startup"))))
        self.assertLess(len(rules), so.HOOK_MAX)
        self.assertIn("결정: 노랑 버튼", ctx_of(hook("notes", dict(ses, source="compact"))))
        self.assertIn("버튼은 노랑으로", ctx_of(hook("ledger", dict(ses, source="compact"))))
        notes.write_text("# 긴 노트\n" + "가나다라 " * 4000)
        big = ctx_of(hook("notes", dict(ses, source="compact")))
        for x in (rules, big):
            self.assertLess(len(x), 10000)
        self.assertIn(f"the rest is in {notes}", big)
        notes.write_text("결정: 노랑 버튼 (사용자)\n")
        self.assertEqual(hook("notes", dict(ses, source="resume")).strip(), "", "a resumed conversation still has them")
        self.assertEqual(hook("ledger", dict(ses, source="resume")).strip(), "")
        # another session in the same folder: told where they are, never given them, and its own files stay apart
        other = ctx_of(hook("notes", {"source": "startup", "session_id": "zzzzzzzz-1"}))
        self.assertIn(str(notes), other)
        self.assertEqual(other.count("결정: 노랑 버튼"), 1, "its first line only, to recognize it")
        self.assertEqual(hook("ledger", {"source": "startup", "session_id": "zzzzzzzz-1"}).strip(), "")
        self.assertEqual(hook("notes", {"source": "compact", "session_id": "zzzzzzzz-1", "transcript_path": str(tr)}).strip(), "")
        # a session older than the PreCompact hook: its ledger is taken right after the compaction
        self.assertIn("ledger-oldsess1.md", ctx_of(hook("session", {"source": "compact", "session_id": "oldsess1-1", "transcript_path": str(tr)})))
        self.assertIn("버튼은 노랑으로", (proj / ".scaleout/ledger-oldsess1.md").read_text())
        n = len((so.TOKENS_DIR / "compactions.jsonl").read_text().splitlines())
        self.assertIn("버튼은 노랑으로", ctx_of(hook("ledger", {"source": "compact", "session_id": "oldsess1-1", "transcript_path": str(tr)})))
        self.assertEqual(len((so.TOKENS_DIR / "compactions.jsonl").read_text().splitlines()), n, "taken once")
        # notes.md of earlier versions: only for the session that wrote it
        legacy = proj / ".scaleout/notes.md"
        legacy.write_text("예전 노트\n")
        tr2 = TMP / "tok-transcript2.jsonl"
        tr2.write_text(json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Write", "input": {"file_path": str(legacy)}}]}}) + "\n")
        self.assertIn("예전 노트", ctx_of(hook("notes", {"source": "compact", "session_id": "legacy01", "transcript_path": str(tr2)})))
        self.assertEqual(hook("notes", {"source": "compact", "session_id": "legacy02", "transcript_path": str(tr)}).strip(), "")
        # in a worktree: its own folder (a session there may not write outside it)
        wt = proj / ".claude/worktrees/w1/lib"
        self.assertIn(str(proj / ".claude/worktrees/w1/.scaleout/notes-s1s1s1s1.md"), ctx_of(hook("session", dict(ses, source="startup", cwd=str(wt)))))
        # a long session: every part has its share and the whole fits
        tr3 = TMP / "tok-transcript3.jsonl"
        use = lambda name, inp: {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}}
        tr3.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in
                                 [{"type": "user", "timestamp": "2026-11-23T01:00:00Z", "message": {"role": "user", "content": f"말 {i} " + "하" * 900}} for i in range(40)]
                                 + [use("TaskCreate", {"subject": f"할 일 {i} " + "나" * 200}) for i in range(40)]
                                 + [use("Edit", {"file_path": str(proj) + f"/lib/{'d' * 80}{i}.dart"}) for i in range(60)]
                                 + [use("Bash", {"command": f"echo {i} " + "x" * 400}) for i in range(40)]) + "\n")
        text = so.build_ledger(str(tr3), 0, str(proj))
        self.assertLessEqual(len(text), so.LEDGER_MAX)
        for x in ("## Task list", "할 일 39", "말 39", "## Files changed", "d59.dart", "## Recent commands", "echo 39"):
            self.assertIn(x, text)
        self.assertNotIn(str(proj) + "/lib", text, "paths in the project are short")
        # near the point where Claude Code compacts (250k - 35k): told once (notes older than 10 minutes), also by the Bash hook
        rows[1]["message"]["usage"]["cache_read_input_tokens"] = 200000
        tr.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in rows) + "\n")
        old = time.time() - 3600
        os.utime(notes, (old, old))
        self.assertIn(str(notes), hook("ctx", ses))
        self.assertEqual(hook("ctx", ses).strip(), "")
        for f in (so.TOKENS_DIR / "nudge").glob("*.json"):
            f.unlink()
        self.assertIn(str(notes), hook("post", dict(ses, hook_event_name="PostToolUse", tool_use_id="t1")), "a session that has only the Bash hook")
        self.assertEqual(hook("post", dict(ses, tool_use_id="t2")).strip(), "")
        # forks: refused from a 150k session, other subagents pass
        deny = json.loads(hook("agent", {"transcript_path": str(tr), "tool_input": {"subagent_type": "fork"}}))
        self.assertEqual(deny["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(hook("agent", {"transcript_path": str(tr), "tool_input": {"subagent_type": "general-purpose"}}).strip(), "")
        # replay: compacting at 250k reads less than the real 50k → 850k session, and costs compactions
        reqs = {"s": [(f"t{i:02d}", 50000 * (i + 1)) for i in range(17)]}
        real = sum(c for _, c in reqs["s"])
        tot, comps = so.whatif(reqs, 250000, 92000)
        self.assertLess(tot, real)
        self.assertGreaterEqual(comps, 2)
        self.assertGreater(so.whatif(reqs, 150000, 92000)[1], comps)
        # steps of 2k from a 92k start, as sessions really grow: a window that leaves no room costs more than one that does
        steps = {"s": [(f"t{i:04d}", 92000 + 2000 * i) for i in range(400)]}
        cc = so.compaction_cost([], 250000)
        cost = lambda w: (lambda r: r[0] + r[1] * cc["cost"])(so.whatif(steps, w, cc["post"]))
        self.assertGreater(cost(150000), cost(250000))
        keep = so._today_requests, so.compaction_stats
        so._today_requests = lambda days=1.0: steps
        ev = lambda n: {"t": 1, "session": "s", "window": 250000, "ctx": 215000, "post": 92000, "write": 58000, "requests": n, "open": False, "reread": 0}
        try:
            for small in (120000, 150000):   # what an earlier version tuned itself down to: straight back to the floor
                cfg = so.load_cfg()
                cfg["tokens"]["window"] = small
                so.compaction_stats = lambda days=7: []
                self.assertGreaterEqual(so.tokens_autotune(cfg, apply=False)["to"], 200000)
            cfg = so.load_cfg()
            so.compaction_stats = lambda days=7: [ev(60)] * 4
            calm = so.tokens_autotune(cfg, apply=False)["to"]
            self.assertTrue(200000 <= calm <= 300000, calm)
            so.compaction_stats = lambda days=7: [ev(8)] * 4
            self.assertEqual(so.tokens_autotune(cfg, apply=False)["to"], 300000, "compacting every few steps: up")
            so.compaction_stats = lambda days=7: [dict(ev(60), post=160000)] * 4
            self.assertGreaterEqual(so.tokens_autotune(cfg, apply=False)["to"], 270000, "never under what a compaction leaves + room")
        finally:
            so._today_requests, so.compaction_stats = keep
        # what followed a compaction, measured from the transcript's own boundaries: a hook called ahead of time or twice does not count
        tr4 = TMP / "tok-transcript4.jsonl"
        iso = lambda sec: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(sec))
        t0 = int(time.time()) - 5000
        req = lambda t, n, rid, path=None: {"type": "assistant", "timestamp": iso(t), "message": {"id": rid, "usage": {"cache_read_input_tokens": n - 50000, "cache_creation_input_tokens": 50000},
                                            "content": [{"type": "tool_use", "name": "Read", "input": {"file_path": path}}] if path else []}}
        bound = lambda t, pre: {"type": "system", "subtype": "compact_boundary", "timestamp": iso(t), "compactMetadata": {"trigger": "auto", "preTokens": pre}}
        tr4.write_text("\n".join(json.dumps(x) for x in [req(t0, 192000, "a", "/p/x"), req(t0 + 60, 215000, "a2"), bound(t0 + 99, 218000), req(t0 + 100, 90000, "b"),
                                                          req(t0 + 110, 92000, "c", "/p/x"), req(t0 + 110, 92000, "c"), bound(t0 + 205, 216000), req(t0 + 210, 88000, "d")]) + "\n")
        log = so.TOKENS_DIR / "compactions.jsonl"
        kept = log.read_text()
        log.write_text("".join(json.dumps({"t": t, "session": "q", "transcript": str(tr4), "ctx": c, "window": 250000}) + "\n"
                               for t, c in ((t0 + 49, 215000), (t0 + 50, 215000), (t0 + 200, 214000))))
        st = so.compaction_stats()
        log.write_text(kept)
        self.assertEqual([(e["ctx"], e["post"], e["write"], e["requests"], e["reread"], e["open"]) for e in st],
                         [(218000, 90000, 50000, 2, 92000, False), (216000, 88000, 50000, 1, 0, True)])
        self.assertEqual(so.compaction_cost([dict(ev(60), window=150000)] * 4, 250000)["measured"], 0, "what a far smaller window left says nothing about this one")
        # the working folder changed since: the notes of the worktree are still found
        self.assertIn("결정: 노랑 버튼", ctx_of(hook("notes", dict(ses, source="compact", cwd=str(TMP)))))
        # off: everything undone, other settings kept
        so.cmd_tokens(argparse.Namespace(action="off", window=None, auto=None, fork_max=None))
        self.assertEqual(json.loads(so.CLAUDE_SETTINGS.read_text())["env"], {"KEEP": "1"})
        for name in nodes:
            self.assertNotIn("TOKEN_SAVING", (HOMES[name] / ".scaleout/node.conf").read_text())
        self.assertEqual(hook("agent", {"transcript_path": str(tr), "tool_input": {"subagent_type": "fork"}}).strip(), "")
        self.assertNotIn("token saving", hook("session", {"source": "startup"}).lower())

    @unittest.skipUnless(sys.platform == "darwin", "Xcode build caches live on macOS (plutil)")
    def test_99f_gc_drops_xcode_caches_and_dead_jobs(self):
        """Every work tree that built for iOS leaves a 4-8 GB Xcode cache: it goes with the work tree, and caches of
        workspaces that are gone go in gc. A job folder left running by a dead runner goes too, and a killed load job."""
        home = TMP / "dd-home"
        b = home / ".scaleout"
        for d in ("bin", "jobs", "work", "repos"):
            (b / d).mkdir(parents=True)
        ctl = b / "bin/ctl"
        shutil.copy(ROOT / "node/unix/ctl.sh", ctl)
        ctl.chmod(0o755)
        dd = home / "Library/Developer/Xcode/DerivedData"
        env = {k: v for k, v in os.environ.items() if k not in ("SCALEOUT_NODE_HOME", "DERIVED_DATA")}
        env["HOME"] = str(home)
        c = lambda *a: subprocess.run([str(ctl), *a], env=env, check=True, stdout=subprocess.PIPE).stdout.decode().strip()

        def cache(name: str, ws: str) -> None:
            (dd / name).mkdir(parents=True)
            plist = str(dd / name / "info.plist")
            subprocess.run(["plutil", "-create", "xml1", plist], check=True)
            subprocess.run(["plutil", "-insert", "WorkspacePath", "-string", ws, plist], check=True)
        live = home / "proj/ios/Runner.xcworkspace"
        live.mkdir(parents=True)
        wt = b / "repos/r-wt1"            # an idle repo work tree, five days old
        (wt / "ios").mkdir(parents=True)
        (wt / ".git").write_text("gitdir: nowhere\n")
        cache("Live", str(live))
        cache("Gone", str(home / "deleted/ios/Runner.xcworkspace"))
        cache("Unmounted", "/Volumes/no-such-disk-xyz/app/ios/Runner.xcworkspace")
        cache("WorkTree", str(wt / "ios/Runner.xcworkspace"))
        old = time.time() - 5 * 86400
        os.utime(wt, (old, old))
        dead, alive = b / "jobs/j-dead", b / "jobs/j-alive"
        for d, pid in ((dead, "99999999"), (alive, str(os.getpid()))):
            d.mkdir()
            (d / "started").write_text("1")
            (d / "pid").write_text(pid)
            for p in (d / "started", d / "pid", d):
                os.utime(p, (old, old))
        load = b / "work/load.123"
        load.mkdir()
        (load / "hold").write_text("")
        os.utime(load, (time.time() - 3 * 3600,) * 2)
        out = c("gc", "3")
        self.assertIn("removed 1, work trees 1, xcode caches 1", out)
        self.assertEqual(sorted(p.name for p in dd.iterdir()), ["Live", "Unmounted"], "the work tree's cache went with it")
        self.assertFalse(dead.exists())
        self.assertTrue(alive.exists(), "never a job whose process still runs")
        self.assertFalse(load.exists())



class DispatchFailure(unittest.TestCase):
    """A node has room but the hand-over fails: the job must say why instead of waiting with nothing shown."""

    def test_99h_spec_placing(self):
        # parts: graphics by name, cores and memory; memory by kind, speed and modules
        self.assertEqual(so.gpu_power("NVIDIA GeForce GTX 1070 Ti", 8192), (8.2, "card"))
        self.assertEqual(so.gpu_power("NVIDIA GeForce GTX 1070", 8192), (6.5, "card"))
        self.assertEqual(so.gpu_power("Apple M1", 0, 8), (2.6, "built-in"))
        self.assertEqual(so.gpu_power("Apple M2", 0, 10), (3.6, "built-in"))
        self.assertEqual(so.gpu_power("NVIDIA RTX A9999", 24576), (28.8, "card"))   # unknown card: by its memory
        self.assertEqual(so.gpu_power("Intel Corporation UHD Graphics 630"), (0.5, "built-in"))
        self.assertEqual(so.gpu_power(""), (0.0, ""))
        ddr4 = {"mem_type": "DDR4", "mem_mts": 3200, "mem_modules": 2}
        self.assertEqual(so.mem_speed(ddr4, {}), (51.2, "DDR4-3200 x2"))
        self.assertEqual(so.mem_speed({**ddr4, "mem_mts": 2400}, {}), (38.4, "DDR4-2400 x2"))       # slower modules, less
        self.assertEqual(so.mem_speed({**ddr4, "mem_modules": 4}, {}), (51.2, "DDR4-3200 x4"))     # still two channels
        self.assertEqual(so.mem_speed(ddr4, {"mem_channels": 4})[0], 102.4)
        self.assertEqual(so.mem_speed({"gpu_name": "Apple M2", "mem_type": "LPDDR5"}, {}), (100.0, "LPDDR5"))
        self.assertEqual(so.mem_speed({"mem_type": "DDR5"}, {}), (76.8, "DDR5-4800"))
        self.assertEqual(so.mem_speed(ddr4, {"mem_gbps": 45})[0], 45.0)
        # kinds of work, from the task; "do not" sentences do not count
        need = lambda text, tags=(): so.job_needs({"text": text, "tags": list(tags)})
        self.assertEqual(need("Blender Cycles 로 렌더해서 PNG 저장"), "gpu")
        self.assertEqual(need("xvfb-run 으로 Godot 화면 캡처 31장"), "gpu")
        self.assertEqual(need("anything", ["gpu"]), "gpu")
        self.assertEqual(need("Godot --headless 로 C# 시험을 돌린다. 스크린샷은 찍지 않는다."), "build")
        self.assertEqual(need("dotnet test 전부 돌리고 보고만"), "build")
        self.assertEqual(need("이 함수가 어디서 불리는지 찾아 정리해줘"), "light")
        # guide words: "a+b" = both somewhere in the task
        self.assertTrue(so.asks("Godot 으로 띄워 스크린샷 31장", ["godot+스크린샷"]))
        self.assertFalse(so.asks("Godot --headless 로 C# 시험", ["godot+스크린샷"]))
        self.assertFalse(so.asks("Flutter E2E 마다 스크린샷", ["godot+스크린샷"]))
        self.assertTrue(so.asks("blender 렌더", ["godot+스크린샷", "blender"]))
        # placing: a graphics job goes to the card even when it is busier; a build goes where CPU is left
        cfg = so.load_cfg()
        cfg["nodes"] = {"card": {"max_jobs": 4, "agent": "claude"}, "mac": {"max_jobs": 2, "agent": "claude"}}
        cfg["defaults"]["max_load_per_cpu"] = 2
        base = {"online": True, "runner": True, "agents": ["claude"], "disk_free_gb": 200, "mem_free_pct": 60}
        card = {**base, "os": "Linux", "cpus": 16, "mem_total_gb": 32, "load": 8, "gpu": {"util": 30, "mem_used_mb": 900, "mem_total_mb": 8192},
                "spec": {"cpu": "AMD Ryzen 7 5700X", "gpu_name": "NVIDIA GeForce GTX 1070 Ti", "gpu_vram_mb": 8192, **ddr4}}
        mac = {**base, "os": "Linux", "cpus": 8, "mem_total_gb": 16, "load": 0, "gpu": {"util": 0},
               "spec": {"cpu": "Apple M1", "gpu_name": "Apple M1", "gpu_cores": 8, "mem_type": "LPDDR4"}}
        s = so.node_spec(cfg, "card", card)
        self.assertEqual((s["gpu"], s["gpu_tflops"], s["vram_gb"], s["mem"], s["mem_gbps"]), ("GTX 1070 Ti", 8.2, 8, "DDR4-3200 x2", 51.2))
        self.assertEqual(so.node_spec(cfg, "mac", mac)["gpu"], "Apple M1 8-core")
        self.assertIn("GTX 1070 Ti 8.2 TF 8 GB", so.spec_line(cfg, "card", card))
        render = {"id": "", "text": "Blender 렌더", "tags": [], "agent": "default"}
        build = {"id": "", "text": "dotnet test 전부", "tags": [], "agent": "default"}
        why: dict = {}
        self.assertEqual(so.pick_node(cfg, render, {"card": card, "mac": mac}, {"card": 1, "mac": 0}, why=why), "card")
        self.assertTrue(why["*"].startswith("gpu job"), why)
        self.assertEqual(so.pick_node(cfg, build, {"card": dict(card, load=0), "mac": mac}, {"card": 0, "mac": 0}), "card")   # idle: more cores
        self.assertEqual(so.pick_node(cfg, build, {"card": dict(card, load=30), "mac": mac}, {"card": 3, "mac": 0}), "mac")  # its CPU is taken
        # a full card takes no more graphics work while another machine can draw
        full = dict(card, gpu={"util": 99, "mem_used_mb": 900, "mem_total_mb": 8192})
        self.assertEqual(so.pick_node(cfg, render, {"card": full, "mac": mac}, {"card": 1, "mac": 0}), "mac")
        # a part the tables do not know is set by hand
        cfg["nodes"]["mac"]["gpu_tflops"] = 50
        self.assertEqual(so.node_spec(cfg, "mac", mac)["gpu_tflops"], 50.0)

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
