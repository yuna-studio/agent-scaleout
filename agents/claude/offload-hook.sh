#!/bin/bash
# agent-scaleout PreToolUse hook for Claude Code's Bash tool.
# When this machine is short on memory and a healthy node is available, a heavy build/test command is refused here
# (exit 2: Claude reads the message and sends the work to a node). Every other command passes untouched.
#   bypass for one command : put SCALEOUT_LOCAL=1 in it
#   threshold              : MIN_FREE=<percent free memory> in ~/.config/scaleout/hook.conf (default 25)
#   always offload         : MIN_FREE=101
set -u
input="$(cat)"
py="$(command -v python3 || echo /usr/bin/python3)"
cmd="$(printf '%s' "$input" | "$py" -c 'import json,sys; print(json.load(sys.stdin).get("tool_input",{}).get("command",""))' 2>/dev/null)"
[ -n "$cmd" ] || exit 0
case "$cmd" in *SCALEOUT_LOCAL=1*|scaleout\ *|*"| scaleout "*|*"/scaleout "*) exit 0 ;; esac

heavy='(^|[;&|[:space:]/])(flutter (build|test|drive)|dart test|xcodebuild|gradlew[^;&|]*(assemble|build|test|connected|bundle)|maestro test|playwright test|(npm|pnpm) (run )?(test|build|e2e)|yarn (test|build|e2e)|emulator -avd|simctl boot)'
printf '%s' "$cmd" | grep -Eq "$heavy" || exit 0

MIN_FREE=25
[ -f "$HOME/.config/scaleout/hook.conf" ] && . "$HOME/.config/scaleout/hook.conf"
if [ "$(uname -s)" = Darwin ]; then free="$(sysctl -n kern.memorystatus_level 2>/dev/null || echo 100)"
else free="$(awk '/MemTotal/ {t=$2} /MemAvailable/ {a=$2} END {if (t) printf "%d", a*100/t; else print 100}' /proc/meminfo)"; fi
[ "$free" -lt "$MIN_FREE" ] || exit 0

# a node the orchestrator saw healthy in the last 3 minutes
node="$("$py" - "${SCALEOUT_STATE:-$HOME/.local/state/scaleout}/nodes" <<'PY' 2>/dev/null
import json, pathlib, sys, time
for p in sorted(pathlib.Path(sys.argv[1]).glob("*.json")):
    try:
        s = json.loads(p.read_text())
    except Exception:
        continue
    if s.get("online") and time.time() - s.get("ts", 0) < 180 and s.get("mem_free_pct", 0) >= 15 \
            and s.get("disk_free_gb", 0) >= 5 and s.get("runner") is not False:
        print(p.stem)
        break
PY
)"
[ -n "$node" ] || exit 0

cat >&2 <<MSG
agent-scaleout: this machine has only ${free}% memory free, so heavy builds and tests go to a node (${node} is free).
Send it as a job instead of running it here:
  echo "<what to run and how to check it>" | scaleout run --repo <git repo> [--subdir <path>] [--apply]
or use the scaleout subagent. If it truly must run on this machine (a device attached here), prefix the command with SCALEOUT_LOCAL=1.
MSG
exit 2
