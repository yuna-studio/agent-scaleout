#!/bin/bash
# agent-scaleout PreToolUse hook for Claude Code's Bash tool.
# While a node in the pool has room, a heavy build/test command is refused here (exit 2: Claude reads the message and sends
# the work to the pool, where several machines run items at the same time). Every other command passes untouched.
# A heavy command that does run here is remembered (direct-run.py), so `scaleout top` shows it with the jobs.
#   bypass for one command : put SCALEOUT_LOCAL=1 in it (a device attached here, a tool only this machine has)
#   settings               : in ~/.config/scaleout/hook.conf
#                              OFFLOAD=always    (default) heavy work goes to the pool whenever a node has room
#                              OFFLOAD=pressure  only when this machine is short:
#                                MIN_FREE=<percent free memory> (default 25), MAX_LOAD_PER_CPU=<load / cores> (default 3)
#                              OFFLOAD=off       never refuse
set -u
input="$(cat)"
py="$(command -v python3 || echo /usr/bin/python3)"
cmd="$(printf '%s' "$input" | "$py" -c 'import json,sys; print(json.load(sys.stdin).get("tool_input",{}).get("command",""))' 2>/dev/null)"
[ -n "$cmd" ] || exit 0
[ -n "${SCALEOUT_JOB_ID:-}" ] && exit 0   # already a scaleout job (here or on a node): run it
case "$cmd" in scaleout\ *|*"| scaleout "*|*"/scaleout "*) exit 0 ;; esac
case "$cmd" in ssh\ *|*"&& ssh "*|*"; ssh "*) exit 0 ;; esac   # runs on another machine, not here

heavy='(^|[;&|[:space:]/])(flutter (build|test|drive)|dart test|xcodebuild|gradlew[^;&|]*(assemble|build|test|connected|bundle)|maestro test|playwright test|(npm|pnpm) (run )?(test|build|e2e)|yarn (test|build|e2e)|emulator -avd|simctl boot)'
printf '%s' "$cmd" | grep -Eq "$heavy" || exit 0
here() {   # it runs on this machine: remember it for the queue (post-hook.sh closes the record), then let it run
  printf '%s' "$input" | "$py" "$(dirname "$0")/direct-run.py" start >/dev/null 2>&1
  exit 0
}
case "$cmd" in *SCALEOUT_LOCAL=1*) here ;; esac

OFFLOAD=always
MIN_FREE=25
MAX_LOAD_PER_CPU=3
[ -f "$HOME/.config/scaleout/hook.conf" ] && . "$HOME/.config/scaleout/hook.conf"
[ "${MIN_FREE:-25}" -gt 100 ] 2>/dev/null && OFFLOAD=always   # the old way to say "always"
[ "$OFFLOAD" = off ] && here
if [ "$(uname -s)" = Darwin ]; then
  free="$(sysctl -n kern.memorystatus_level 2>/dev/null || echo 100)"
  load="$(sysctl -n vm.loadavg | awk '{printf "%d", $2}')"; cpus="$(sysctl -n hw.ncpu)"
else
  free="$(awk '/MemTotal/ {t=$2} /MemAvailable/ {a=$2} END {if (t) printf "%d", a*100/t; else print 100}' /proc/meminfo)"
  load="$(cut -d' ' -f1 /proc/loadavg | cut -d. -f1)"; cpus="$(nproc)"
fi
why=""
if [ "$OFFLOAD" = pressure ]; then
  [ "$free" -lt "$MIN_FREE" ] && why="this machine has only ${free}% memory free"
  [ -z "$why" ] && [ "$load" -ge $((MAX_LOAD_PER_CPU * cpus)) ] && why="this machine is overloaded (load ${load} on ${cpus} cores)"
  [ -n "$why" ] || here
else
  why="heavy work runs in the pool, where several machines take items at the same time"
fi

# nodes the orchestrator saw healthy in the last 3 minutes, the least busy first (its own name from `scaleout rename`)
node="$("$py" - "${SCALEOUT_STATE:-$HOME/.local/state/scaleout}/nodes" "${SCALEOUT_CONF:-$HOME/.config/scaleout}/config.json" <<'PY' 2>/dev/null
import json, pathlib, sys, time
try:
    nodes = json.loads(pathlib.Path(sys.argv[2]).read_text()).get("nodes", {})
except Exception:
    nodes = {}
free = []
for p in sorted(pathlib.Path(sys.argv[1]).glob("*.json")):
    if p.stem == "orchestrator" or (nodes.get(p.stem) or {}).get("local"):   # this machine itself
        continue
    try:
        s = json.loads(p.read_text())
    except Exception:
        continue
    if s.get("online") and time.time() - s.get("ts", 0) < 180 and s.get("mem_free_pct", 0) >= 15 \
            and s.get("disk_free_gb", 0) >= 5 and s.get("runner") is not False:
        busy = float(s.get("load") or 0) / max(1, int(s.get("cpus") or 1)) + int(s.get("running") or 0)
        free.append((busy, (nodes.get(p.stem) or {}).get("label") or p.stem))
print(", ".join(n for _, n in sorted(free)[:3]))
PY
)"
[ -n "$node" ] || here

cat >&2 <<MSG
agent-scaleout: ${why} (free now: ${node}).
Send it as a job instead of running it here. Several independent items: queue them all, keep working, collect:
  id=\$(echo "<what to run and how to check it>" | scaleout submit --repo <git repo> [--subdir <path>] [--tags mac])
  … then  scaleout wait \$id -m 0  with run_in_background (shows in the task list, notifies when done)
  (or one scaleout subagent per item, started in the same message)
One item you must wait for: echo "<…>" | scaleout run --repo <git repo> [--apply]
If it truly must run on this machine (a device attached here), prefix the command with SCALEOUT_LOCAL=1.
MSG
exit 2
