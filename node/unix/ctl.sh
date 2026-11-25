#!/bin/bash
# agent-scaleout node control (macOS / Linux). The orchestrator calls this over SSH:
#   ~/.scaleout/bin/ctl <verb> [args]
# Every verb prints plain text or one JSON line, so the orchestrator never needs to know the node's OS.
# Works with bash 3.2 (stock macOS): no `wait -n`, no associative arrays, and ${var} before non-ASCII text.
set -u
B="${SCALEOUT_NODE_HOME:-$HOME/.scaleout}"   # the orchestrator runs its own jobs from another folder
SDK_DIR="${ANDROID_HOME:-$HOME/Android/Sdk}"; [ "$(uname -s)" = Darwin ] && [ -z "${ANDROID_HOME:-}" ] && SDK_DIR="$HOME/Library/Android/sdk"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$SDK_DIR/platform-tools:$SDK_DIR/emulator:$HOME/.maestro/bin:$PATH"
export ANDROID_HOME="${ANDROID_HOME:-$SDK_DIR}" MAESTRO_CLI_NO_ANALYTICS=1 MAESTRO_CLI_ANALYSIS_NOTIFICATION_DISABLED=true
[ -f "$B/node.conf" ] && . "$B/node.conf"
OS="$(uname -s)"
VERSION="__VERSION__"

j() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }   # JSON-escape a short string
ms() { perl -MTime::HiRes=time -e 'printf "%d\n", time*1000' 2>/dev/null || python3 -c 'import time; print(int(time.time()*1000))' 2>/dev/null || echo "$(date +%s)000"; }
model() {
  if [ "$OS" = Darwin ]; then echo "$(sysctl -n machdep.cpu.brand_string 2>/dev/null) · $(sysctl -n hw.model 2>/dev/null)"
  else sed -n 's/^model name[[:space:]]*: //p' /proc/cpuinfo 2>/dev/null | head -1; fi
}

mem_free_pct() {
  if [ "$OS" = Darwin ]; then sysctl -n kern.memorystatus_level 2>/dev/null || echo 0
  else awk '/MemTotal/ {t=$2} /MemAvailable/ {a=$2} END {if (t) printf "%d", a*100/t; else print 0}' /proc/meminfo; fi
}
mem_total_gb() {
  if [ "$OS" = Darwin ]; then echo $(( $(sysctl -n hw.memsize) / 1073741824 ))
  else awk '/MemTotal/ {printf "%d", $2/1048576}' /proc/meminfo; fi
}
disk_free_gb() {  # free GB of the disk holding $1 (falls back to $HOME when the folder does not exist yet)
  local p="${1:-$HOME}" v; p="${p/#\~/$HOME}"; [ -e "$p" ] || p="$HOME"
  v="$(df -Pk "$p" 2>/dev/null | awk 'NR==2 {printf "%d", $4/1048576}')"; echo "${v:-0}"
}
swap_gb() {  # swap in use
  if [ "$OS" = Darwin ]; then sysctl -n vm.swapusage 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "used") {printf "%.1f", $(i + 2) / 1024; f = 1; exit}} END {if (!f) printf "0"}'
  else awk '/^SwapTotal/ {t = $2} /^SwapFree/ {f = $2} END {printf "%.1f", (t - f) / 1048576}' /proc/meminfo; fi
}
load1() { if [ "$OS" = Darwin ]; then sysctl -n vm.loadavg | awk '{print $2}'; else cut -d' ' -f1 /proc/loadavg; fi; }
cpus() { if [ "$OS" = Darwin ]; then sysctl -n hw.ncpu; else nproc; fi; }
gpu() {  # {"util":..,"mem_used_mb":..,"mem_total_mb":..,"temp":..,"name":".."} of the first graphics card, or null when it cannot be read
  local q u a f x out=""
  if [ "$OS" = Darwin ]; then   # memory is shared with the machine on Apple silicon: only how busy it is
    u="$(ioreg -r -d 1 -c IOAccelerator 2>/dev/null | sed -n 's/.*"Device Utilization %"=\([0-9][0-9]*\).*/\1/p' | head -1)"
    if [ -n "$u" ]; then printf '{"util":%s}' "$u"; else echo null; fi; return
  fi
  if command -v nvidia-smi >/dev/null 2>&1; then
    q="$(timeout 5 nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu,name --format=csv,noheader,nounits 2>/dev/null | head -1)"
    case "$q" in *,*,*,*,*)
      u="$(timeout 5 nvidia-smi -q -d UTILIZATION 2>/dev/null | awk '/GPU Utilization Samples/ {f = 1} f && /Avg/ {print $3; exit}')"   # over the last ~12 s
      case "$u" in ''|*[!0-9]*) u="${q%%,*}" ;; esac
      for x in util mem_used_mb mem_total_mb temp; do
        a="${q%%,*}"; q="${q#*,}"; [ "$x" = util ] && a="$u"
        a="$(printf '%s' "$a" | tr -dc '0-9')"; [ -n "$a" ] && out="$out\"$x\":$a,"
      done
      printf '{%s"name":"%s"}' "$out" "$(j "${q# }")"; return ;;
    esac
  fi
  for f in /sys/class/drm/card[0-9]*/device/gpu_busy_percent; do   # AMD
    u="$(cat "$f" 2>/dev/null)"; case "$u" in ''|*[!0-9]*) ;; *) printf '{"util":%s}' "$u"; return ;; esac
  done
  echo null
}
running_jobs() { local n=0 d; for d in "$B"/jobs/*/; do [ -f "$d/started" ] && [ ! -f "$d/rc" ] && n=$((n + 1)); done; echo "$n"; }
dd_drop() {  # dd_drop <folder>|orphans -> how many Xcode build caches (DerivedData) went
  # Xcode keeps one build cache per workspace path (4-8 GB for a Flutter app) and never removes it, so every repo work tree
  # that built for iOS leaves one behind. Drop the caches of workspaces under <folder>, or ("orphans") of workspaces
  # that no longer exist. A workspace on an external disk that is not mounted right now is not an orphan.
  local root="${DERIVED_DATA:-$HOME/Library/Developer/Xcode/DerivedData}" d ws vol n=0
  [ -d "$root" ] && command -v plutil >/dev/null 2>&1 || { echo 0; return; }
  for d in "$root"/*/; do
    [ -f "$d/info.plist" ] || continue
    ws="$(plutil -extract WorkspacePath raw "$d/info.plist" 2>/dev/null)" || continue
    [ -n "$ws" ] || continue
    if [ "$1" = orphans ]; then
      [ -e "$ws" ] && continue
      case "$ws" in /Volumes/*) vol="/Volumes/$(printf '%s' "$ws" | cut -d/ -f3)"; [ -d "$vol" ] || continue ;; esac
    else
      case "$ws" in "$1"/*) ;; *) continue ;; esac
    fi
    rm -rf "$d"; n=$((n + 1))
  done
  echo "$n"
}
alive() { kill -0 "$1" 2>/dev/null && ! ps -o stat= -p "$1" 2>/dev/null | grep -q "^Z"; }   # a stopped child not yet reaped is not alive
stale_lock() {  # stale_lock <work tree>.lock -> 0 when no job holds it any more. A lock keeps the id of the job that took it:
  # it is free once that job was cancelled or removed and nothing of it still runs. A lock without an id (from before), or one
  # whose job ended and was never collected, frees after 12 h as before.
  local l="$1" jid d
  [ -d "$l" ] || return 1
  [ -n "$(find "$l" -maxdepth 0 -mmin +720 2>/dev/null)" ] && return 0
  jid="$(cat "$l/job" 2>/dev/null)"; [ -n "$jid" ] || return 1
  d="$B/jobs/$jid"
  if [ -f "$d/pid" ] && alive "$(cat "$d/pid" 2>/dev/null)"; then return 1; fi   # its agent still works in it
  [ -f "$d/cancelled" ] && return 0
  [ ! -d "$d" ] && [ -n "$(find "$l" -maxdepth 0 -mmin +60 2>/dev/null)" ] && return 0   # job gone (moved, removed): not one being handed over
  return 1
}
free_locks() {  # free_locks <job> [<work tree>] -> drop the work tree locks this job holds, once nothing of it runs
  local d="$B/jobs/$1" l i=0
  while [ -f "$d/pid" ] && alive "$(cat "$d/pid" 2>/dev/null)" && [ "$i" -lt 10 ]; do sleep 1; i=$((i + 1)); done
  if [ -f "$d/pid" ] && alive "$(cat "$d/pid" 2>/dev/null)"; then return 1; fi   # still stopping: stale_lock frees it later
  for l in "$B"/repos/*.lock; do
    [ -d "$l" ] && [ "$(cat "$l/job" 2>/dev/null)" = "$1" ] && rm -rf "$l"
  done
  if [ -n "${2:-}" ] && [ -d "$2.lock" ]; then   # a lock taken by an older version keeps no id: the orchestrator names it
    case "$(cat "$2.lock/job" 2>/dev/null)" in "$1"|"") rm -rf "$2.lock" ;; esac
  fi
  return 0
}
idle_wt() {  # idle_wt <work tree> -> takes its lock when no job holds it
  stale_lock "$1.lock" && rm -rf "$1.lock"
  mkdir "$1.lock" 2>/dev/null
}
reclaim_wts() {  # reclaim_wts <free GB wanted> [idle days] -> how many repo work trees were dropped
  # An idle work tree of repo jobs keeps its build caches (often several GB) so the next job there builds faster. Drop them
  # least recently used first until <free GB wanted> is free; with [idle days], also every one idle longer than that.
  local want="$1" days="${2:-}" w r n=0
  for w in $(ls -dtr "$B"/repos/*-wt[0-9]* 2>/dev/null); do
    [ -f "$w/.git" ] || continue                     # work trees only: not bare copies, not locks
    if [ "$(disk_free_gb "$B")" -ge "$want" ]; then
      [ -n "$days" ] && [ -n "$(find "$w" -maxdepth 0 -mtime +"$days" 2>/dev/null)" ] || continue
    fi
    idle_wt "$w" || continue                         # a job works in it
    rm -rf "$w"; dd_drop "$w" >/dev/null; rmdir "$w.lock" 2>/dev/null; n=$((n + 1))
  done
  if [ "$n" -gt 0 ]; then for r in "$B"/repos/*.git; do [ -d "$r" ] && git -C "$r" worktree prune 2>/dev/null; done; fi
  echo "$n"
}
own_peak() {  # own_peak <jid>: keep the most memory the job's own processes held (Linux: they carry its SCALEOUT_JOB_ID), MB
  [ "$OS" = Darwin ] && return 0
  local d="$B/jobs/$1" f mb=0 l=()
  while IFS= read -r f; do l+=("${f%/environ}/smaps_rollup"); done < <(grep -slz "^SCALEOUT_JOB_ID=$1\$" /proc/[0-9]*/environ 2>/dev/null)
  [ "${#l[@]}" -gt 0 ] && mb="$(cat "${l[@]}" 2>/dev/null | awk '/^(Pss|SwapPss):/ {s += $2} END {printf "%d", s / 1024}')"
  [ -f "$d/own_since" ] || date +%s > "$d/own_since"
  [ "${mb:-0}" -gt "$(cat "$d/own_mb" 2>/dev/null || echo 0)" ] && echo "$mb" > "$d/own_mb"
  return 0
}
own_mb() {  # own_mb <jid>: that peak, 0 when the job was not watched from its start
  local d="$B/jobs/$1" a b
  a="$(cat "$d/started" 2>/dev/null || echo 0)"; b="$(cat "$d/own_since" 2>/dev/null || echo 0)"
  if [ "$b" -gt 0 ] && [ $((b - a)) -lt 180 ]; then cat "$d/own_mb" 2>/dev/null || echo 0; else echo 0; fi
}
queued_jobs() { ls "$B/queue" 2>/dev/null | wc -l | tr -d ' '; }
runner_ok() {
  if [ "${RUNNER:-}" = manual ]; then echo true; return; fi   # the user starts runner.sh some other way
  if [ "$OS" = Darwin ]; then launchctl print "gui/$(id -u)/dev.scaleout.runner" >/dev/null 2>&1 && echo true || echo false
  else
    if command -v systemctl >/dev/null 2>&1 && systemctl --user is-enabled scaleout-runner.path >/dev/null 2>&1; then
      # enabled is not enough: after "start request repeated too quickly" both units stay enabled but failed, and no job starts
      if systemctl --user is-failed scaleout-runner.path scaleout-runner.service >/dev/null 2>&1; then echo false; else echo true; fi
    elif crontab -l 2>/dev/null | grep -q scaleout; then echo true; else echo false; fi
  fi
}
devices_scan() {  # slow (xcrun devicectl can hang) — run in the background only
  local a=0 i=0
  command -v adb >/dev/null 2>&1 && a="$(adb devices 2>/dev/null | awk 'NR>1 && $2=="device" && $1 !~ /^emulator-/' | wc -l | tr -d ' ')"   # phones only
  if [ "$OS" = Darwin ] && [ -x /usr/bin/xcrun ] && xcode-select -p >/dev/null 2>&1; then
    i="$(xcrun devicectl list devices 2>/dev/null | grep -E 'connected|available \(paired\)' | grep -E -c 'iPhone|iPad' || true)"   # not watches
  elif command -v idevice_id >/dev/null 2>&1; then i="$(idevice_id -l 2>/dev/null | wc -l | tr -d ' ')"; fi
  printf '{"android":%s,"ios":%s}' "${a:-0}" "${i:-0}" > "$B/devices.cache.tmp" && mv "$B/devices.cache.tmp" "$B/devices.cache"
}
devices() {  # USB-attached real devices {"android":N,"ios":N} — never blocks: answers from cache, refreshes in the background
  local c="$B/devices.cache"
  # DEVICE_SCAN=off in node.conf: never scan (write devices.cache yourself, e.g. {"android":2,"ios":0})
  if [ "${DEVICE_SCAN:-on}" != off ] && { [ ! -f "$c" ] || [ -z "$(find "$c" -mmin -1 2>/dev/null)" ]; }; then
    if mkdir "$B/devices.scan" 2>/dev/null; then
      ( devices_scan; rm -rf "$B/devices.scan" ) >/dev/null 2>&1 &
      # a scan stuck for more than 2 minutes is abandoned so the next call can retry
    elif [ -n "$(find "$B/devices.scan" -maxdepth 0 -mmin +2 2>/dev/null)" ]; then rm -rf "$B/devices.scan"; fi
  fi
  cat "$c" 2>/dev/null || printf '{"android":0,"ios":0}'
}
# ── simulators / emulators / build daemons: what is running now, and stopping them ──
booted_sims() { command -v xcrun >/dev/null 2>&1 && xcrun simctl list devices booted 2>/dev/null | sed -n 's/.*(\([0-9A-F-]\{36\}\)) (Booted).*/\1/p'; }
running_emus() { command -v adb >/dev/null 2>&1 && adb devices 2>/dev/null | awk '/^emulator-[0-9]+/ {print $1}'; }
build_daemons() { pgrep -f 'GradleDaemon|KotlinCompileDaemon' 2>/dev/null; }
stop_sim() { xcrun simctl shutdown "$1" >/dev/null 2>&1; }
stop_emu() { adb -s "$1" emu kill >/dev/null 2>&1; }
quit_simulator_app() { [ -z "$(booted_sims)" ] && pgrep -x Simulator >/dev/null 2>&1 && osascript -e 'quit app "Simulator"' >/dev/null 2>&1; true; }
TEST_RE='maestro|xcodebuild .*test|flutter (test|drive)|flutter_tester|gradlew.*(connected|AndroidTest)|instrument|playwright|simctl (launch|install|io|spawn)|detox|appium'
skills_hash() { cat "$B/skills.hash" 2>/dev/null || echo none; }
agents() {
  local out="" a
  for a in claude codex gemini; do command -v "$a" >/dev/null 2>&1 && out="$out\"$a\","; done
  [ -n "${AGENT_CMD:-}" ] && out="$out\"custom\","
  echo "[${out%,}]"
}

claude_now() {  # subagents running under the agents here: {"subagents":N,"descs":[…]} (a transcript written in the last 10 min, not finished)
  local n=0 d="" f m desc
  if [ -d "$HOME/.claude/projects" ]; then
    while IFS= read -r f; do
      m="${f%.jsonl}.meta.json"
      [ -f "$m" ] && grep -q '"finished":true' "$m" && continue
      n=$((n + 1))
      desc="$(sed -n 's/.*"description":"\([^"]*\)".*/\1/p' "$m" 2>/dev/null | head -1 | cut -c1-40)"
      [ -n "$desc" ] && [ "$n" -le 3 ] && d="${d}\"$(j "$desc")\","
    done < <(find "$HOME/.claude/projects" -path '*/subagents/*.jsonl' -mmin -10 2>/dev/null)
  fi
  printf '{"subagents":%s,"descs":[%s]}' "$n" "${d%,}"
}

caps() {   # what this machine can run, as tags a job may ask for: an Android emulator (SDK emulator + an AVD), iOS simulators (Xcode)
  local c=""
  { [ -x "$SDK_DIR/emulator/emulator" ] && ls "$HOME"/.android/avd/*.ini >/dev/null 2>&1; } && c="$c\"emulator\","
  [ "$OS" = Darwin ] && xcrun simctl help >/dev/null 2>&1 && c="$c\"simulator\",\"ios-simulator\","
  command -v maestro >/dev/null 2>&1 && c="$c\"maestro\","
  [ "$OS" != Darwin ] && command -v nvidia-smi >/dev/null 2>&1 && timeout 5 nvidia-smi -L >/dev/null 2>&1 && c="$c\"gpu\","
  printf '[%s]' "${c%,}"
}

verb="${1:-}"; shift || true
case "$verb" in
  version) echo "$VERSION" ;;
  guide)   # the machine's guide on stdin (empty removes it): jobs read it at ~/.scaleout/guide.md
    cat > "$B/guide.md.new"
    if [ -s "$B/guide.md.new" ]; then mv -f "$B/guide.md.new" "$B/guide.md"; else rm -f "$B/guide.md.new" "$B/guide.md"; fi
    echo ok ;;
  metrics)
    printf '{"os":"%s","arch":"%s","host":"%s","mem_free_pct":%s,"mem_total_gb":%s,"swap_gb":%s,"disk_free_gb":%s,"work_free_gb":%s,"load":%s,"cpus":%s,"gpu":%s,"running":%s,"queued":%s,"runner":%s,"agents":%s,"devices":%s,"claude":%s,"caps":%s,"skills_hash":"%s","version":"%s"}\n' \
      "$(j "$OS")" "$(uname -m)" "$(j "$(hostname)")" "$(mem_free_pct)" "$(mem_total_gb)" "$(swap_gb)" "$(disk_free_gb "$HOME")" \
      "$(disk_free_gb "${WORK_ROOT:-$HOME}")" "$(load1)" "$(cpus)" "$(gpu)" "$(running_jobs)" "$(queued_jobs)" "$(runner_ok)" "$(agents)" "$(devices)" "$(claude_now)" "$(caps)" "$(skills_hash)" "$VERSION" ;;

  submit)  # submit <jid> <agent> <timeout_min> <workdir> [resume-session-id] [model] [effort]   (prompt on stdin)
    jid="$1"; d="$B/jobs/$jid"; mkdir -p "$d" "$B/queue"
    cat > "$d/prompt.md"
    printf 'AGENT=%s\nTIMEOUT_MIN=%s\nWORKDIR=%s\nRESUME=%s\nMODEL=%s\nEFFORT=%s\n' "$2" "$3" "$4" "${5:-}" "${6:-}" "${7:-}" > "$d/meta"
    touch "$B/queue/$jid"
    [ "$OS" = Darwin ] || "$B/bin/kick" >/dev/null 2>&1 &   # Linux: start the runner now (its systemd service when there is one)
    echo queued ;;

  state)  # state <jid>  -> {"state":..., "rc":..., "heartbeat_age":...}
    d="$B/jobs/$1"
    if [ ! -d "$d" ]; then echo '{"state":"missing"}'; exit 0; fi
    now="$(date +%s)"
    if [ "$OS" = Darwin ]; then hb="$(stat -f %m "$d/heartbeat" 2>/dev/null)"; else hb="$(stat -c %Y "$d/heartbeat" 2>/dev/null)"; fi
    case "$hb" in ''|*[!0-9]*) hb="$now" ;; esac   # GNU stat -f means "file system": never let its text reach the arithmetic
    if [ -f "$d/rc" ]; then printf '{"state":"done","rc":%s,"mem_mb":%s,"own_mb":%s,"artifacts":%s,"artifacts_kb":%s}\n' "$(tr -dc '0-9' < "$d/rc")" "$(cat "$d/mem_mb" 2>/dev/null || echo 0)" "$(own_mb "$1")" \
      "$(find "$d/artifacts" -type f 2>/dev/null | wc -l | tr -d ' ')" "$(du -sk "$d/artifacts" 2>/dev/null | cut -f1 || echo 0)"
    elif [ -f "$d/started" ]; then own_peak "$1"; printf '{"state":"running","heartbeat_age":%s}\n' "$((now - hb))"
    elif [ -f "$B/queue/$1" ]; then echo '{"state":"queued"}'
    elif [ -n "$(find "$d/taken" -mmin -2 2>/dev/null)" ]; then echo '{"state":"queued"}'   # a runner took it and is starting the agent
    else echo '{"state":"lost"}'; fi ;;

  result) cat "$B/jobs/$1/out" 2>/dev/null ;;
  artifacts) tar -cf - -C "$B/jobs/$1/artifacts" . 2>/dev/null ;;   # artifacts <jid> -> tar of the job's artifacts folder
  err) tail -n "${2:-40}" "$B/jobs/$1/err.log" 2>/dev/null ;;
  cancel)  # cancel <job> [<work tree>]: stop it, and once it has stopped give back the work tree it held
    d="$B/jobs/$1"; rm -f "$B/queue/$1"; touch "$d/cancelled" 2>/dev/null
    [ -f "$d/pid" ] && kill -TERM "$(cat "$d/pid")" 2>/dev/null
    [ -f "$d/rc" ] || [ -f "$d/started" ] || echo 130 > "$d/rc"
    if free_locks "$1" "${2:-}"; then echo cancelled; else echo "cancelled (still stopping)"; fi ;;
  rm) rm -rf "$B/jobs/$1" "$B/queue/$1" "$B/work/$1"; echo removed ;;

  tokens)  # tokens on <window> | off | status — `scaleout tokens`: Claude's auto-compact window on this machine and the job rules
    case "${1:-status}" in
      on|off)
        win="${2:-300000}"; case "$win" in *[!0-9]*|"") win=300000 ;; esac
        touch "$B/node.conf"
        grep -v -E '^(TOKEN_SAVING|COMPACT_WINDOW)=' "$B/node.conf" > "$B/node.conf.tmp" 2>/dev/null || true
        [ "$1" = on ] && printf 'TOKEN_SAVING=1\nCOMPACT_WINDOW=%s\n' "$win" >> "$B/node.conf.tmp"
        mv -f "$B/node.conf.tmp" "$B/node.conf"
        if command -v python3 >/dev/null 2>&1; then
          python3 - "$HOME/.claude/settings.json" "$1" "$win" <<'PY' || { echo "settings not changed"; exit 1; }
import json, os, sys
path, mode, win = sys.argv[1:]
s = {}
if os.path.exists(path):
    try:
        s = json.load(open(path))
    except ValueError:
        sys.exit(1)          # never rewrite a settings file we cannot read
env = s.setdefault("env", {})
if mode == "on":
    env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = win
else:
    env.pop("CLAUDE_CODE_AUTO_COMPACT_WINDOW", None)
if not env:
    s.pop("env")
os.makedirs(os.path.dirname(path), exist_ok=True)
tmp = path + ".scaleout-tmp"
with open(tmp, "w") as f:
    f.write(json.dumps(s, indent=2, ensure_ascii=False) + "\n")
os.replace(tmp, path)
PY
        fi
        [ "$1" = on ] && echo "tokens on $win" || echo "tokens off" ;;
      *) if [ "${TOKEN_SAVING:-0}" = 1 ]; then echo "on ${COMPACT_WINDOW:-300000}"; else echo off; fi ;;
    esac ;;

  gc)  # gc <days>  — drop finished job folders, and repo work trees idle, older than N days
    days="${1:-3}"; n=0
    for d in "$B"/jobs/*/; do
      [ -f "$d/rc" ] || continue
      if [ -n "$(find "$d/rc" -mtime +"$days" 2>/dev/null)" ]; then rm -rf "$d"; n=$((n + 1)); fi
    done
    for d in "$B"/jobs/*/; do   # left "running" by a runner that died: no rc, its process gone, nothing written for a day
      [ -f "$d/started" ] && [ ! -f "$d/rc" ] || continue
      p="$(cat "$d/pid" 2>/dev/null)"; [ -n "$p" ] && kill -0 "$p" 2>/dev/null && continue
      [ -n "$(find "$d" -maxdepth 1 -mmin -1440 2>/dev/null)" ] && continue
      rm -rf "$d"; n=$((n + 1))
    done
    find "$B/work" -mindepth 1 -maxdepth 1 -mtime +"$days" -exec rm -rf {} + 2>/dev/null
    find "$B/work" -mindepth 1 -maxdepth 1 -name 'load.*' -mmin +120 -exec rm -rf {} + 2>/dev/null   # a killed bench load job
    old="$(date -v-"${days}"d +%Y%m%d 2>/dev/null || date -d "-$days days" +%Y%m%d)"   # job refs of repo copies
    for r in "$B"/repos/*.git; do
      [ -d "$r" ] || continue
      git -C "$r" for-each-ref --format='%(refname)' refs/scaleout | while read -r ref; do
        jd="$(basename "$ref" | sed -n 's/^j\([0-9]\{8\}\)-.*/\1/p')"
        [ -n "$jd" ] && [ "$jd" -lt "$old" ] && git -C "$r" update-ref -d "$ref"
      done
      git -C "$r" gc --auto -q 2>/dev/null
    done
    echo "removed $n, work trees $(reclaim_wts 0 "$days"), xcode caches $(dd_drop orphans)" ;;

  cleanup)  # cleanup [aggressive|deep] [GB wanted, default 20]  — free disk safely; prints GB before -> after
    before="$(disk_free_gb)"; want="${2:-20}"
    "$0" gc 1 >/dev/null
    rm -rf "$B"/jobs/*/out.prev 2>/dev/null
    for p in "$HOME/Library/Caches/pip" "$HOME/.cache/pip" "$HOME/Library/Caches/Homebrew" "$HOME/.cache/yarn" \
             "$HOME/Library/Developer/Xcode/DerivedData" "$HOME/.npm/_cacache" "$HOME/.cache/go-build"; do
      [ -d "$p" ] && rm -rf "$p" 2>/dev/null
    done
    wts="$(reclaim_wts "$want")"   # idle repo work trees, least recently used first, until <GB wanted> is free
    if [ "${1:-}" = deep ] || { [ "${1:-}" = aggressive ] && [ "$(disk_free_gb)" -lt "$want" ]; }; then   # caches under $HOME only — rebuilt on demand
      for p in "$HOME/.gradle/caches" "$HOME/.cache" "$HOME/Library/Caches"; do [ -d "$p" ] && rm -rf "$p"/* 2>/dev/null; done
    fi
    if [ "${1:-}" = deep ]; then   # only when a human asks (scaleout cleanup --deep): machine-wide
      command -v xcrun >/dev/null && xcrun simctl delete unavailable 2>/dev/null
      command -v docker >/dev/null && docker system prune -af >/dev/null 2>&1
    fi
    echo "disk_free_gb $before -> $(disk_free_gb), work trees dropped $wts" ;;

  spec)  # what the machine is built from (read once a day): memory kind, speed and modules, graphics card and its cores or memory.
         # Placing weighs these with the free % so work goes where it runs fastest. Nothing here needs root.
    mt="" mm="" md=0 gn="" gv="" gc=""
    if [ "$OS" = Darwin ]; then
      sp="$(system_profiler SPDisplaysDataType SPMemoryDataType 2>/dev/null)"
      gn="$(printf '%s\n' "$sp" | sed -n 's/^ *Chipset Model: //p' | head -1)"
      gc="$(printf '%s\n' "$sp" | sed -n 's/^ *Total Number of Cores: \([0-9]*\).*/\1/p' | head -1)"
      gv="$(printf '%s\n' "$sp" | sed -n 's/^ *VRAM ([^)]*): \([0-9]*\) GB.*/\1/p' | head -1)"; [ -n "$gv" ] && gv=$((gv * 1024))
      mt="$(printf '%s\n' "$sp" | sed -n 's/^ *Type: \(.*DDR.*\)/\1/p' | head -1)"
      mm="$(printf '%s\n' "$sp" | sed -n 's/^ *Speed: \([0-9]*\) MHz.*/\1/p' | head -1)"   # Intel Macs; Apple silicon does not say
    else
      if command -v udevadm >/dev/null 2>&1; then   # systemd reads the memory modules from the firmware tables for everyone
        p="$(udevadm info -q property /sys/class/dmi/id 2>/dev/null)"
        mt="$(printf '%s\n' "$p" | sed -n 's/^MEMORY_DEVICE_[0-9]*_TYPE=\(.*DDR.*\)/\1/p' | head -1)"
        mm="$(printf '%s\n' "$p" | sed -n 's/^MEMORY_DEVICE_[0-9]*_CONFIGURED_SPEED_MTS=\([0-9][0-9]*\)$/\1/p' | head -1)"
        [ -n "$mm" ] || mm="$(printf '%s\n' "$p" | sed -n 's/^MEMORY_DEVICE_[0-9]*_SPEED_MTS=\([0-9][0-9]*\)$/\1/p' | head -1)"
        md="$(printf '%s\n' "$p" | grep -cE '^MEMORY_DEVICE_[0-9]+_SIZE=[1-9]')"
      fi
      if command -v nvidia-smi >/dev/null 2>&1; then
        q="$(timeout 5 nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits 2>/dev/null | head -1)"
        case "$q" in *,*) gn="${q%,*}"; gv="$(printf '%s' "${q##*,}" | tr -dc '0-9')" ;; esac
      fi
      [ -n "$gn" ] || gn="$(lspci 2>/dev/null | grep -iE 'vga|3d controller|display controller' | head -1 | sed 's/^[^:]*:[^:]*: //')"
    fi
    printf '{"cpu":"%s","cpus":%s,"mem_total_gb":%s,"mem_type":"%s","mem_mts":%s,"mem_modules":%s,"gpu_name":"%s","gpu_vram_mb":%s,"gpu_cores":%s}\n' \
      "$(j "$(model)")" "$(cpus)" "$(mem_total_gb)" "$(j "$mt")" "${mm:-0}" "${md:-0}" "$(j "$gn")" "${gv:-0}" "${gc:-0}" ;;

  bench)  # hardware probe for `scaleout bench`: gzip of random data on one core and on all cores, disk write, memory
    t="$B/bench.tmp"; rm -rf "$t"; mkdir -p "$t"
    head -c 33554432 /dev/urandom > "$t/r"
    a="$(ms)"; gzip -c "$t/r" > /dev/null; gzip -c "$t/r" > /dev/null; s1=$(( $(ms) - a ))
    n="$(cpus)"; a="$(ms)"; i=0
    while [ "$i" -lt "$n" ]; do ( gzip -c "$t/r" > /dev/null; gzip -c "$t/r" > /dev/null ) & i=$((i + 1)); done; wait
    s2=$(( $(ms) - a ))
    a="$(ms)"; i=0; while [ "$i" -lt 16 ]; do cat "$t/r" >> "$t/d"; i=$((i + 1)); done; sync; s3=$(( $(ms) - a ))
    rm -rf "$t"
    [ "$s1" -gt 0 ] || s1=1; [ "$s2" -gt 0 ] || s2=1; [ "$s3" -gt 0 ] || s3=1
    printf '{"single_mbps":%s,"multi_mbps":%s,"disk_write_mbps":%s,"cpus":%s,"mem_total_gb":%s,"mem_free_pct":%s,"disk_free_gb":%s,"model":"%s"}\n' \
      "$((64000 / s1))" "$((64000 * n / s2))" "$((512000 / s3))" "$n" "$(mem_total_gb)" "$(mem_free_pct)" "$(disk_free_gb "${WORK_ROOT:-$HOME}")" "$(j "$(model)")" ;;

  loadjob)  # synthetic mobile job for `scaleout bench --load`: holds ~2.5 GB the whole time and alternates
            # "waiting for the model" (sleep) with build bursts on 4 cores, like an agent that edits, builds, tests
    t="$B/work/load.$$"; mkdir -p "$t"; head -c 33554432 /dev/urandom > "$t/r"; touch "$t/hold"
    trap 'rm -rf "$t"' EXIT; trap 'exit 143' INT TERM HUP   # killed or timed out: the memory holder below ends too
    a="$(ms)"
    perl -e 'open(F, "<", $ARGV[0]) or exit; local $/; my $r = <F>; my $x = $r x 78; my $pp = getppid(); sleep 1 while -e $ARGV[1] && getppid() == $pp;' "$t/r" "$t/hold" &
    hp=$!; phase=0
    while [ "$phase" -lt 3 ]; do
      sleep "${LOAD_WAIT_S:-8}"; w=0; pids=""
      while [ "$w" -lt 4 ]; do ( i=0; while [ "$i" -lt "${LOAD_ROUNDS:-12}" ]; do gzip -c "$t/r" > /dev/null; i=$((i + 1)); done ) & pids="$pids $!"; w=$((w + 1)); done
      for p in $pids; do wait "$p"; done
      phase=$((phase + 1))
    done
    i=0; while [ "$i" -lt 16 ]; do cat "$t/r" >> "$t/d"; i=$((i + 1)); done; sync
    rm -f "$t/hold"; wait "$hp" 2>/dev/null; rm -rf "$t"
    e=$(( $(ms) - a ))
    printf '{"result":"load job: %s ms on %s","ms":%s}\n' "$e" "$(j "$(hostname)")" "$e" ;;

  repo-init)  # repo-init <name> -> the node's bare copy of an orchestrator repo (git sends only missing objects)
    r="$B/repos/$1.git"; [ -d "$r" ] || git init -q --bare "$r"; echo "$r" ;;
  repo-checkout)  # repo-checkout <name> <commit> [<job>] -> a locked work tree at <commit>; ignored files (build caches) are kept
    r="$B/repos/$1.git"; i=1
    top=$(( ${MAX_JOBS:-2} + 2 )); [ "$top" -lt 8 ] && top=8   # room for every job this node may run, and a spare
    while :; do
      w="$B/repos/$1-wt$i"
      if mkdir "$w.lock" 2>/dev/null; then printf '%s\n' "${3:-}" > "$w.lock/job"; break; fi
      if stale_lock "$w.lock"; then rm -rf "$w.lock"; continue; fi
      i=$((i + 1)); [ "$i" -gt "$top" ] && { echo "no free work tree (all $top held)" >&2; exit 1; }
    done
    if [ -e "$w/.git" ]; then
      git -C "$w" checkout -q -f --detach "$2" && git -C "$w" clean -fdq || { rm -rf "$w.lock"; echo "checkout failed" >&2; exit 1; }
    else
      git -C "$r" worktree prune 2>/dev/null; rm -rf "$w"
      git -C "$r" worktree add -q -f --detach "$w" "$2" >/dev/null 2>&1 || { rm -rf "$w.lock"; echo "worktree add failed" >&2; exit 1; }
    fi
    touch "$w"   # last use: reclaim_wts drops the least recently used first
    echo "$w" ;;
  repo-result)  # repo-result <worktree> <base> <job> -> commit holding everything the job changed (the base if nothing)
    cd "$1" || exit 1
    git add -A >/dev/null 2>&1
    if git diff --cached --quiet "$2"; then c="$2"
    else git -c user.name=agent-scaleout -c user.email=scaleout@localhost commit -q --no-verify -m "scaleout $3" && c="$(git rev-parse HEAD)"; fi
    git update-ref "refs/scaleout/result/$3" "$c" && echo "$c" ;;
  repo-unlock) rm -rf "$1.lock"; echo ok ;;
  repo-locks)  # the work tree locks here, one per line: <work tree> <job or -> <age in minutes>
    for l in "$B"/repos/*.lock; do
      [ -d "$l" ] || continue
      jid="$(cat "$l/job" 2>/dev/null)"; t="$(stat -c %Y "$l" 2>/dev/null || stat -f %m "$l" 2>/dev/null || echo 0)"
      printf '%s %s %s\n' "${l%.lock}" "${jid:--}" "$(( ($(date +%s) - t) / 60 ))"
    done ;;

  reap)  # reap <idle_min>: stop simulators/emulators when no test has run for that long
    m="${1:-20}"; st="$B/reap.active"
    s="$(booted_sims)"; e="$(running_emus)"
    if [ -z "$s$e" ]; then rm -f "$st"; echo "nothing running"; exit 0; fi
    # pgrep patterns are extended regexes already (macOS pgrep has no -E)
    if pgrep -f "$TEST_RE" >/dev/null 2>&1 || [ "$(running_jobs)" -gt 0 ]; then touch "$st"; echo "in use"; exit 0; fi
    [ -f "$st" ] || { touch "$st"; echo "idle clock started"; exit 0; }
    if [ -n "$(find "$st" -mmin +"$m" 2>/dev/null)" ]; then
      n=0; for x in $s; do stop_sim "$x"; n=$((n + 1)); done; for x in $e; do stop_emu "$x"; n=$((n + 1)); done
      quit_simulator_app; rm -f "$st"; echo "stopped $n idle simulator/emulator(s)"
    else echo "idle, not long enough"; fi ;;

  conf)  # conf KEY VALUE  — set one node.conf value
    f="$B/node.conf"; touch "$f"; grep -v "^$1=" "$f" > "$f.tmp"; echo "$1=$2" >> "$f.tmp"; mv "$f.tmp" "$f"; echo ok ;;

  recv)  # recv <dir-relative-to-home-or-abs>  — tar on stdin
    t="${1/#\~/$HOME}"; mkdir -p "$t" && tar -xf - -C "$t" && echo ok ;;
  send)  # send <dir>  — tar to stdout (excludes heavy build dirs)
    t="${1/#\~/$HOME}"; tar -cf - -C "$t" --exclude node_modules --exclude .gradle --exclude build --exclude .dart_tool --exclude DerivedData . ;;
  skills-hash) echo "$1" > "$B/skills.hash"; echo ok ;;
  job-tokens)  # job-tokens <session id>: the first request's whole input (what a fresh agent loads before any work) and how many requests
    python3 - "${1:-}" <<'JT'
import glob, json, os, sys
sid = sys.argv[1]
files = glob.glob(os.path.join(os.path.expanduser("~"), ".claude", "projects", "*", sid + ".jsonl")) if sid else []
first, seen = None, set()
for line in open(files[0]) if files else []:
    try:
        r = json.loads(line)
    except ValueError:
        continue
    m = r.get("message") or {}
    u = m.get("usage") or {}
    if r.get("type") != "assistant" or not u or r.get("isSidechain"):
        continue
    mid = m.get("id") or r.get("requestId") or ""
    if mid in seen:
        continue
    seen.add(mid)
    if first is None:
        first = int(u.get("input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0)
print(json.dumps({"start": first or 0, "requests": len(seen)}))
JT
    ;;
  relocate)  # relocate <orchestrator home>: skills that name the orchestrator's home (e.g. its ~/harness_workspace) point at this one
    [ "${1:-}" = "$HOME" ] && { echo same; exit 0; }
    python3 - "$1" "$HOME" <<'RELOC'
import os, sys
old, new = (sys.argv[1].rstrip("/") + "/").encode(), (sys.argv[2].rstrip("/") + "/").encode()
n = 0
for top in (".claude/skills", ".claude/agents", ".claude/commands", "harness_workspace/.bin", "harness_workspace/my"):
    for root, dirs, files in os.walk(os.path.join(sys.argv[2], top)):
        dirs[:] = [d for d in dirs if d not in ("log", "runs", ".harness-runs", ".git")]
        for f in files:
            p = os.path.join(root, f)
            try:
                b = open(p, "rb").read()
            except OSError:
                continue
            if b"\0" in b[:4096] or old not in b:
                continue
            open(p, "wb").write(b.replace(old, new))
            n += 1
print("relocated", n)
RELOC
    ;;

  repair)  # re-register the runner service
    bash "$B/bin/setup.sh" --repair && echo repaired ;;
  usage)  # daily Claude tokens of every session on this machine -> one JSON line (see usage.py)
    python3 "$(dirname "$0")/usage.py" 2>/dev/null || echo '{}' ;;
  doctor)
    echo "host: $(hostname) · $OS $(uname -r) · $(uname -m) · ${VERSION}"
    echo "memory: $(mem_total_gb)GB, free $(mem_free_pct)% · disk free $(disk_free_gb)GB · load $(load1)/$(cpus)"
    for t in claude codex gemini git gh node npm python3 flutter xcodebuild docker; do
      v="$(command -v "$t" >/dev/null 2>&1 && { [ "$t" = xcodebuild ] && xcodebuild -version 2>/dev/null | head -1 || "$t" --version 2>/dev/null | head -1; } || echo -)"
      printf '  %-10s %s\n' "$t" "$v"
    done
    echo "runner: $(runner_ok) · agent: ${AGENT:-claude}" ;;
  jobs)
    for d in $(ls -1dt "$B"/jobs/*/ 2>/dev/null | head -20); do
      n="$(basename "$d")"
      if [ -f "$d/rc" ]; then s="done rc=$(cat "$d/rc")"; elif [ -f "$d/started" ]; then s=running; else s=queued; fi
      echo "$n $s"
    done ;;
  runner-install) bash "$B/bin/gh-runner.sh" install "$@" ;;
  provision) env "$@" bash "$B/bin/provision.sh" ;;   # provision KEY=VALUE ...  (see provision.sh)
  runner-remove) bash "$B/bin/gh-runner.sh" remove "$@" ;;
  *) echo "unknown verb: $verb" >&2; exit 4 ;;
esac
