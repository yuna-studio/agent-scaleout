#!/bin/bash
# agent-scaleout node runner (macOS / Linux).
# Started by the node's service manager inside the user's login session (macOS LaunchAgent with QueueDirectories,
# Linux systemd --user path unit, or cron fallback) whenever ~/.scaleout/queue is not empty.
# Why a login-session service and not plain SSH: on macOS an SSH session cannot read the login keychain,
# so agents such as Claude Code look "not logged in" when started over SSH.
set -u
B="${SCALEOUT_NODE_HOME:-$HOME/.scaleout}"; Q="$B/queue"; J="$B/jobs"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
MAX_JOBS=2; AGENT=claude; AGENT_CMD=""; MIN_FREE_GB=2; MIN_FREE_MEM_PCT=12; MAX_LOAD_PER_CPU=2
[ -f "$B/node.conf" ] && . "$B/node.conf"
mkdir -p "$Q" "$J" "$B/work"

# single instance — the service manager may start us again while we run
LOCK="$B/runner.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  p="$(cat "$LOCK/pid" 2>/dev/null)"
  if [ -n "$p" ] && kill -0 "$p" 2>/dev/null; then
    # started by systemd while another runner holds the queue (one started outside the service): quitting at once makes the
    # path unit start us again and again until systemd gives up on both units, so wait for it and take over
    [ -n "${INVOCATION_ID:-}" ] || exit 0
    while kill -0 "$p" 2>/dev/null; do sleep 5; done
  fi
  rm -rf "$LOCK"; mkdir "$LOCK" || exit 0
fi
echo $$ > "$LOCK/pid"; trap 'rm -rf "$LOCK"' EXIT

running() { jobs -rp | wc -l | tr -d ' '; }
disk_free_gb() { df -Pk "$HOME" | awk 'NR==2 {printf "%d", $4/1048576}'; }
used_mb() {  # memory in use on this machine, MB
  if [ "$(uname -s)" = Darwin ]; then echo $(( $(sysctl -n hw.memsize) / 1048576 * (100 - $(sysctl -n kern.memorystatus_level 2>/dev/null || echo 50)) / 100 ))
  else awk '/MemTotal/ {t=$2} /MemAvailable/ {a=$2} END {printf "%d", (t-a)/1024}' /proc/meminfo; fi
}
overloaded() {  # 1-minute load above MAX_LOAD_PER_CPU × cores
  local l c
  if [ "$(uname -s)" = Darwin ]; then l="$(sysctl -n vm.loadavg | awk '{print $2}')"; c="$(sysctl -n hw.ncpu)"
  else l="$(cut -d' ' -f1 /proc/loadavg)"; c="$(nproc)"; fi
  awk -v l="$l" -v c="$c" -v m="$MAX_LOAD_PER_CPU" 'BEGIN {exit !(l > m * c)}'
}
mem_free_pct() {
  if [ "$(uname -s)" = Darwin ]; then sysctl -n kern.memorystatus_level 2>/dev/null || echo 100
  else awk '/MemTotal/ {t=$2} /MemAvailable/ {a=$2} END {if (t) printf "%d", a*100/t; else print 100}' /proc/meminfo; fi
}

# ── simulators / emulators / build daemons: what is running now, and stopping them ──
booted_sims() { command -v xcrun >/dev/null 2>&1 && xcrun simctl list devices booted 2>/dev/null | sed -n 's/.*(\([0-9A-F-]\{36\}\)) (Booted).*/\1/p'; }
running_emus() { command -v adb >/dev/null 2>&1 && adb devices 2>/dev/null | awk '/^emulator-[0-9]+/ {print $1}'; }
build_daemons() { pgrep -f 'GradleDaemon|KotlinCompileDaemon' 2>/dev/null; }
stop_sim() { xcrun simctl shutdown "$1" >/dev/null 2>&1; }
stop_emu() { adb -s "$1" emu kill >/dev/null 2>&1; }
quit_simulator_app() { [ -z "$(booted_sims)" ] && pgrep -x Simulator >/dev/null 2>&1 && osascript -e 'quit app "Simulator"' >/dev/null 2>&1; true; }

baseline() {  # remember what was already running when the node got busy
  { booted_sims | sed 's/^/sim /'; running_emus | sed 's/^/emu /'; build_daemons | sed 's/^/pid /'; } > "$B/devices.baseline" 2>/dev/null
}
release() {  # node is idle again: stop simulators, emulators and build daemons the jobs started (never the ones from before)
  [ -f "$B/devices.baseline" ] || return 0
  [ "${KEEP_DEVICES:-0}" = 1 ] && { rm -f "$B/devices.baseline"; return 0; }
  local x n=0
  for x in $(booted_sims); do grep -qx "sim $x" "$B/devices.baseline" || { stop_sim "$x"; n=$((n + 1)); }; done
  for x in $(running_emus); do grep -qx "emu $x" "$B/devices.baseline" || { stop_emu "$x"; n=$((n + 1)); }; done
  for x in $(build_daemons); do grep -qx "pid $x" "$B/devices.baseline" || { kill "$x" 2>/dev/null; n=$((n + 1)); }; done
  quit_simulator_app
  rm -f "$B/devices.baseline"
  [ "$n" -gt 0 ] && echo "$(date '+%F %T') idle: stopped $n simulator/emulator/daemon(s) started by jobs" >> "$B/runner.log"
  return 0
}

agent_cmd() {  # agent_cmd <agent> -> command line that reads the prompt on stdin and writes the answer to stdout
  local s=""; [ -f "$B/settings/claude.json" ] && s="--settings $B/settings/claude.json"
  case "$1" in
    claude)  # PERMISSION_MODE=bypass in node.conf: never ask, the deny list in settings still applies
      local pm=""; [ "${PERMISSION_MODE:-}" = bypass ] && pm="--permission-mode bypassPermissions"
      local tr=""; [ "${TOKEN_SAVING:-0}" = 1 ] && [ -f "$B/bin/token-rules.md" ] && tr="--append-system-prompt \"\$(cat '$B/bin/token-rules.md')\""
      echo "claude -p --output-format json $pm $s $tr" ;;   # tr: `scaleout tokens on` adds the job rules for small contexts
    codex)  echo 'codex exec --skip-git-repo-check "$(cat)"' ;;
    gemini) echo 'gemini -p "$(cat)"' ;;
    custom) echo "${AGENT_CMD:-cat}" ;;
    bench)  echo "bash $B/bin/ctl loadjob" ;;   # `scaleout bench --load`: synthetic work, no agent account used
    *) echo "$1" ;;   # any other command line
  esac
}

skills_off() {  # skills_off <prompt file> -> 0 when the job can run without skills (about 11k tokens of list in every request)
  # Kept when the task names one of this node's skills, says "skill", or is about documents, charts or pages (built-in skills).
  [ "${TOKEN_SAVING:-0}" = 1 ] && [ "${JOB_SKILLS:-auto}" = auto ] || return 1
  grep -qiE '스킬|skill|차트|그래프|대시보드|엑셀|파워포인트|발표 ?자료|chart|dashboard|spreadsheet|artifact page|\.(pdf|pptx|docx|xlsx)' "$1" && return 1
  local x
  for x in "$HOME"/.claude/skills/*/; do
    [ -d "$x" ] || continue
    x="$(basename "$x")"; [ "$x" = synced ] || ! grep -qF -- "$x" "$1" || return 1
  done
  return 0
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
  # continue an existing Claude Code session (its transcript must be reachable here, e.g. a shared ~/.claude/projects)
  local resume; resume="$(sed -n 's/^RESUME=//p' "$d/meta" | tr -cd 'A-Za-z0-9-')"
  [ -n "$resume" ] && [ "$agent" = claude ] && cmd="$cmd --resume $resume"
  local model; model="$(sed -n 's/^MODEL=//p' "$d/meta" | tr -cd 'A-Za-z0-9._-')"   # checks run on `check_model`
  [ -n "$model" ] && [ "$agent" = claude ] && cmd="$cmd --model $model"
  local effort; effort="$(sed -n 's/^EFFORT=//p' "$d/meta" | tr -cd 'a-z')"
  [ -n "$effort" ] && [ "$agent" = claude ] && cmd="$cmd --effort $effort"
  [ -z "$resume" ] && [ "$agent" = claude ] && skills_off "$d/prompt.md" && cmd="$cmd --disable-slash-commands"
  # screenshots, recordings, logs, reports saved here are fetched back to the orchestrator
  mkdir -p "$d/artifacts"; export SCALEOUT_ARTIFACTS="$d/artifacts" SCALEOUT_JOB_ID="$(basename "$d")"
  date +%s > "$d/started"; touch "$d/heartbeat"
  local u0 peak u; u0="$(used_mb)"; peak="$u0"
  bash -c "$cmd" < "$d/prompt.md" > "$d/out" 2> "$d/err.log" &
  local cpid=$!; echo "$cpid" > "$d/pid"
  cd / 2>/dev/null   # the agent keeps working in $wd; the runner itself does not sit there (what is left there is stopped later)
  # heartbeat + timeout watchdog: the orchestrator treats a stale heartbeat as a dead runner
  local waited=0
  while kill -0 "$cpid" 2>/dev/null; do
    sleep 10; waited=$((waited + 10)); touch "$d/heartbeat"
    u="$(used_mb)"; [ "$u" -gt "$peak" ] && peak="$u" && echo $((peak - u0)) > "$d/mem_mb"   # peak memory this job added
    if [ "$waited" -ge $((timeout * 60)) ]; then
      echo "timed out after ${timeout} min" >> "$d/err.log"; kill -TERM "$cpid" 2>/dev/null; sleep 5; kill -KILL "$cpid" 2>/dev/null
    fi
  done
  wait "$cpid"; local rc=$?
  [ -f "$d/cancelled" ] && rc=130
  # what the job left running in its folder (a game window, a server, a watcher) is stopped now and listed with its results
  cd / 2>/dev/null
  local left; left="$(bash "$B/bin/ctl" stray "$wd" stop 2>/dev/null)"
  if [ -n "$left" ]; then
    mkdir -p "$d/artifacts"
    { echo "# processes the job left running in its folder, stopped when it ended: <pid> <minutes> <command>"; printf '%s\n' "$left"; } > "$d/artifacts/leftovers.txt"
    echo "$(date '+%F %T') $(basename "$d"): stopped $(printf '%s\n' "$left" | wc -l | tr -d ' ') process(es) it left in $wd" >> "$B/runner.log"
  fi
  echo "$rc" > "$d/rc"; date +%s > "$d/finished"
}

idle=0
while :; do
  set -- "$Q"/*
  if [ ! -e "$1" ]; then
    # linger a little before exiting: launchd will not start us again for ~10 s after we quit
    if [ "$(running)" -eq 0 ]; then release; idle=$((idle + 2)); [ "$idle" -ge "${LINGER_S:-30}" ] && break; else idle=0; fi
    sleep 2; continue
  fi
  idle=0
  for f in "$@"; do
    while [ "$(running)" -ge "$MAX_JOBS" ]; do sleep 2; done
    # do not start another agent while this machine is short on memory (other apps, other agents);
    # the job waits here and the orchestrator moves it elsewhere if it waits too long
    while [ "$(running)" -gt 0 ] && { [ "$(mem_free_pct)" -lt "$MIN_FREE_MEM_PCT" ] || overloaded; }; do sleep 5; done
    if [ "$(mem_free_pct)" -lt "$MIN_FREE_MEM_PCT" ]; then sleep 15; continue 2; fi
    [ "$(running)" -eq 0 ] && [ ! -f "$B/devices.baseline" ] && baseline
    id="$(basename "$f")"
    touch "$J/$id/taken" 2>/dev/null   # before the queue entry goes: between the two, `ctl state` must not see a lost job
    rm -f "$f"
    [ -d "$J/$id" ] || continue
    run_job "$J/$id" &
  done
done
wait
