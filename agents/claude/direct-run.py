#!/usr/bin/env python3
"""agent-scaleout: remember heavy builds and tests a Claude Code session ran on this machine itself, not through scaleout.

Claude Code's hooks call it with the hook's JSON on stdin:
  direct-run.py start   PreToolUse, from offload-hook.sh, once a heavy command is let through to run here
  direct-run.py end     PostToolUse / PostToolUseFailure (post-hook.sh calls it only when `start` wrote a record)

One small JSON file per command in <state>/direct/<tool_use_id>.json. `scaleout top` and `scaleout jobs` show them in the
queue next to scaleout's own jobs. A command sent to the background ends later: the dashboard reads its output file.
"""
import json
import os
import re
import sys
import time

STATE = os.environ.get("SCALEOUT_STATE") or os.path.join(os.path.expanduser("~"), ".local", "state", "scaleout")
DIR = os.path.join(STATE, "direct")
HEAVY = re.compile(r"(flutter (build|test|drive)|dart test|xcodebuild|gradlew[^;&|]*(assemble|build|test|connected|bundle)|maestro test"
                   r"|playwright test|(npm|pnpm) (run )?(test|build|e2e)|yarn (test|build|e2e)|emulator -avd|simctl boot)[^;&|]*")
SAFE = re.compile(r"[^A-Za-z0-9_-]")


def write(path, rec):
    tmp = path + ".tmp.%d" % os.getpid()
    with open(tmp, "w") as f:
        json.dump(rec, f, ensure_ascii=False)
    os.replace(tmp, path)


def main():
    what = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        h = json.load(sys.stdin)
    except ValueError:
        return
    tid = SAFE.sub("", str(h.get("tool_use_id") or ""))
    if not tid:
        return
    path = os.path.join(DIR, tid + ".json")
    ti = h.get("tool_input") or {}
    if what == "start":
        cmd = str(ti.get("command") or "")
        m = HEAVY.search(cmd)
        os.makedirs(DIR, exist_ok=True)
        write(path, {"id": tid, "session": str(h.get("session_id") or ""), "cwd": str(h.get("cwd") or os.getcwd()),
                     "cmd": cmd[:400], "what": re.sub(r"\s*\d*>.*$", "", (m.group(0) if m else cmd.split("\n")[0])).strip()[:80],
                     "background": bool(ti.get("run_in_background")), "started": time.time(), "status": "running"})
        return
    if what != "end" or not os.path.exists(path):
        return
    try:
        with open(path) as f:
            rec = json.load(f)
    except (OSError, ValueError):
        return
    resp = h.get("tool_response") if isinstance(h.get("tool_response"), dict) else {}
    task = resp.get("backgroundTaskId") or resp.get("background_task_id")
    if h.get("hook_event_name") == "PostToolUse" and (task or rec.get("background")):
        rec["background"] = True   # it goes on after the tool returns: the dashboard reads its output file to see the end
        if task:
            rec["task"] = SAFE.sub("", str(task))
    else:
        failed = h.get("hook_event_name") == "PostToolUseFailure"
        rec["status"] = "cancelled" if h.get("is_interrupt") else "failed" if failed else "done"
        rec["finished"] = time.time()
        if h.get("duration_ms"):
            rec["finished"] = rec["started"] + float(h["duration_ms"]) / 1000
    write(path, rec)


if __name__ == "__main__":
    try:
        main()
    except Exception:   # a hook must never get in the way of the command
        pass
