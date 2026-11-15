#!/bin/bash
# agent-scaleout node runner (macOS / Linux).
# Started by the node's service manager inside the user's login session (macOS LaunchAgent with QueueDirectories,
# Linux systemd --user path unit, or cron fallback) whenever ~/.scaleout/queue is not empty.
# Why a login-session service and not plain SSH: on macOS an SSH session cannot read the login keychain,
# so agents such as Claude Code look "not logged in" when started over SSH.
set -u
B="$HOME/.scaleout"; Q="$B/queue"; J="$B/jobs"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
MAX_JOBS=2; AGENT=claude; AGENT_CMD=""; MIN_FREE_GB=2
[ -f "$B/node.conf" ] && . "$B/node.conf"
mkdir -p "$Q" "$J" "$B/work"

# single instance — the service manager may start us again while we run
LOCK="$B/runner.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  p="$(cat "$LOCK/pid" 2>/dev/null)"; [ -n "$p" ] && kill -0 "$p" 2>/dev/null && exit 0
  rm -rf "$LOCK"; mkdir "$LOCK" || exit 0
fi
echo $$ > "$LOCK/pid"; trap 'rm -rf "$LOCK"' EXIT

running() { jobs -rp | wc -l | tr -d ' '; }
disk_free_gb() { df -Pk "$HOME" | awk 'NR==2 {printf "%d", $4/1048576}'; }

agent_cmd() {  # agent_cmd <agent> -> command line that reads the prompt on stdin and writes the answer to stdout
  local s=""; [ -f "$B/settings/claude.json" ] && s="--settings $B/settings/claude.json"
  case "$1" in
    claude) echo "claude -p --output-format json $s" ;;
    codex)  echo 'codex exec --skip-git-repo-check "$(cat)"' ;;
    gemini) echo 'gemini -p "$(cat)"' ;;
    custom) echo "${AGENT_CMD:-cat}" ;;
    *) echo "$1" ;;   # any other command line
  esac
}

run_job() {
  local d="$1" agent timeout wd cmd
  agent="$(sed -n 's/^AGENT=//p' "$d/meta")"; [ -n "$agent" ] && [ "$agent" != default ] || agent="$AGENT"
  timeout="$(sed -n 's/^TIMEOUT_MIN=//p' "$d/meta")"; [ -n "$timeout" ] || timeout=180
  wd="$(sed -n 's/^WORKDIR=//p' "$d/meta")"; wd="${wd/#\~/$HOME}"; [ -n "$wd" ] || wd="$HOME"
  mkdir -p "$wd" 2>/dev/null
  cd "$wd" 2>/dev/null || { echo "workdir not found: $wd" > "$d/err.log"; echo 9 > "$d/rc"; return; }
  if [ "$(disk_free_gb)" -lt "$MIN_FREE_GB" ]; then echo "node disk almost full ($(disk_free_gb)GB)" > "$d/err.log"; echo 28 > "$d/rc"; return; fi
  cmd="$(agent_cmd "$agent")"
  date +%s > "$d/started"; touch "$d/heartbeat"
  bash -c "$cmd" < "$d/prompt.md" > "$d/out" 2> "$d/err.log" &
  local cpid=$!; echo "$cpid" > "$d/pid"
  # heartbeat + timeout watchdog: the orchestrator treats a stale heartbeat as a dead runner
  local waited=0
  while kill -0 "$cpid" 2>/dev/null; do
    sleep 10; waited=$((waited + 10)); touch "$d/heartbeat"
    if [ "$waited" -ge $((timeout * 60)) ]; then
      echo "timed out after ${timeout} min" >> "$d/err.log"; kill -TERM "$cpid" 2>/dev/null; sleep 5; kill -KILL "$cpid" 2>/dev/null
    fi
  done
  wait "$cpid"; local rc=$?
  [ -f "$d/cancelled" ] && rc=130
  echo "$rc" > "$d/rc"; date +%s > "$d/finished"
}

while :; do
  set -- "$Q"/*
  if [ ! -e "$1" ]; then [ "$(running)" -eq 0 ] && break; sleep 2; continue; fi
  for f in "$@"; do
    while [ "$(running)" -ge "$MAX_JOBS" ]; do sleep 2; done
    id="$(basename "$f")"; rm -f "$f"
    [ -d "$J/$id" ] || continue
    run_job "$J/$id" &
  done
done
wait
