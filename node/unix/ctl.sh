#!/bin/bash
# agent-scaleout node control (macOS / Linux). The orchestrator calls this over SSH:
#   ~/.scaleout/bin/ctl <verb> [args]
# Every verb prints plain text or one JSON line, so the orchestrator never needs to know the node's OS.
# Works with bash 3.2 (stock macOS): no `wait -n`, no associative arrays, and ${var} before non-ASCII text.
set -u
B="$HOME/.scaleout"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$HOME/Library/Android/sdk/platform-tools:${ANDROID_HOME:-$HOME/Android/Sdk}/platform-tools:$PATH"
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
load1() { if [ "$OS" = Darwin ]; then sysctl -n vm.loadavg | awk '{print $2}'; else cut -d' ' -f1 /proc/loadavg; fi; }
cpus() { if [ "$OS" = Darwin ]; then sysctl -n hw.ncpu; else nproc; fi; }
running_jobs() { local n=0 d; for d in "$B"/jobs/*/; do [ -f "$d/started" ] && [ ! -f "$d/rc" ] && n=$((n + 1)); done; echo "$n"; }
queued_jobs() { ls "$B/queue" 2>/dev/null | wc -l | tr -d ' '; }
runner_ok() {
  if [ "${RUNNER:-}" = manual ]; then echo true; return; fi   # the user starts runner.sh some other way
  if [ "$OS" = Darwin ]; then launchctl print "gui/$(id -u)/dev.scaleout.runner" >/dev/null 2>&1 && echo true || echo false
  else
    if command -v systemctl >/dev/null 2>&1 && systemctl --user is-enabled scaleout-runner.path >/dev/null 2>&1; then echo true
    elif crontab -l 2>/dev/null | grep -q scaleout; then echo true; else echo false; fi
  fi
}
devices_scan() {  # slow (xcrun devicectl can hang) — run in the background only
  local a=0 i=0
  command -v adb >/dev/null 2>&1 && a="$(adb devices 2>/dev/null | awk 'NR>1 && $2=="device"' | wc -l | tr -d ' ')"
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
skills_hash() { cat "$B/skills.hash" 2>/dev/null || echo none; }
agents() {
  local out="" a
  for a in claude codex gemini; do command -v "$a" >/dev/null 2>&1 && out="$out\"$a\","; done
  [ -n "${AGENT_CMD:-}" ] && out="$out\"custom\","
  echo "[${out%,}]"
}

verb="${1:-}"; shift || true
case "$verb" in
  version) echo "$VERSION" ;;
  metrics)
    printf '{"os":"%s","arch":"%s","host":"%s","mem_free_pct":%s,"mem_total_gb":%s,"disk_free_gb":%s,"work_free_gb":%s,"load":%s,"cpus":%s,"running":%s,"queued":%s,"runner":%s,"agents":%s,"devices":%s,"skills_hash":"%s","version":"%s"}\n' \
      "$(j "$OS")" "$(uname -m)" "$(j "$(hostname)")" "$(mem_free_pct)" "$(mem_total_gb)" "$(disk_free_gb "$HOME")" \
      "$(disk_free_gb "${WORK_ROOT:-$HOME}")" "$(load1)" "$(cpus)" "$(running_jobs)" "$(queued_jobs)" "$(runner_ok)" "$(agents)" "$(devices)" "$(skills_hash)" "$VERSION" ;;

  submit)  # submit <jid> <agent> <timeout_min> <workdir>   (prompt on stdin)
    jid="$1"; d="$B/jobs/$jid"; mkdir -p "$d" "$B/queue"
    cat > "$d/prompt.md"
    printf 'AGENT=%s\nTIMEOUT_MIN=%s\nWORKDIR=%s\n' "$2" "$3" "$4" > "$d/meta"
    touch "$B/queue/$jid"
    [ "$OS" = Darwin ] || "$B/bin/kick" >/dev/null 2>&1 &   # Linux without systemd path units: start the runner now
    echo queued ;;

  state)  # state <jid>  -> {"state":..., "rc":..., "heartbeat_age":...}
    d="$B/jobs/$1"
    if [ ! -d "$d" ]; then echo '{"state":"missing"}'; exit 0; fi
    now="$(date +%s)"
    hb="$(stat -f %m "$d/heartbeat" 2>/dev/null || stat -c %Y "$d/heartbeat" 2>/dev/null || echo "$now")"
    if [ -f "$d/rc" ]; then printf '{"state":"done","rc":%s,"mem_mb":%s}\n' "$(tr -dc '0-9' < "$d/rc")" "$(cat "$d/mem_mb" 2>/dev/null || echo 0)"
    elif [ -f "$d/started" ]; then printf '{"state":"running","heartbeat_age":%s}\n' "$((now - hb))"
    elif [ -f "$B/queue/$1" ]; then echo '{"state":"queued"}'
    else echo '{"state":"lost"}'; fi ;;

  result) cat "$B/jobs/$1/out" 2>/dev/null ;;
  err) tail -n "${2:-40}" "$B/jobs/$1/err.log" 2>/dev/null ;;
  cancel)
    d="$B/jobs/$1"; rm -f "$B/queue/$1"; touch "$d/cancelled" 2>/dev/null
    [ -f "$d/pid" ] && kill -TERM "$(cat "$d/pid")" 2>/dev/null
    [ -f "$d/rc" ] || [ -f "$d/started" ] || echo 130 > "$d/rc"
    echo cancelled ;;
  rm) rm -rf "$B/jobs/$1" "$B/queue/$1" "$B/work/$1"; echo removed ;;

  gc)  # gc <days>  — drop finished job folders older than N days
    days="${1:-3}"; n=0
    for d in "$B"/jobs/*/; do
      [ -f "$d/rc" ] || continue
      if [ -n "$(find "$d/rc" -mtime +"$days" 2>/dev/null)" ]; then rm -rf "$d"; n=$((n + 1)); fi
    done
    find "$B/work" -mindepth 1 -maxdepth 1 -mtime +"$days" -exec rm -rf {} + 2>/dev/null
    echo "removed $n" ;;

  cleanup)  # cleanup [aggressive]  — free disk safely; prints GB before -> after
    before="$(disk_free_gb)"
    "$0" gc 1 >/dev/null
    rm -rf "$B"/jobs/*/out.prev 2>/dev/null
    for p in "$HOME/Library/Caches/pip" "$HOME/.cache/pip" "$HOME/Library/Caches/Homebrew" "$HOME/.cache/yarn" \
             "$HOME/Library/Developer/Xcode/DerivedData" "$HOME/.npm/_cacache" "$HOME/.cache/go-build"; do
      [ -d "$p" ] && rm -rf "$p" 2>/dev/null
    done
    if [ "${1:-}" = aggressive ] || [ "${1:-}" = deep ]; then   # caches under $HOME only — rebuilt on demand
      for p in "$HOME/.gradle/caches" "$HOME/.cache" "$HOME/Library/Caches"; do [ -d "$p" ] && rm -rf "$p"/* 2>/dev/null; done
    fi
    if [ "${1:-}" = deep ]; then   # only when a human asks (scaleout cleanup --deep): machine-wide
      command -v xcrun >/dev/null && xcrun simctl delete unavailable 2>/dev/null
      command -v docker >/dev/null && docker system prune -af >/dev/null 2>&1
    fi
    echo "disk_free_gb $before -> $(disk_free_gb)" ;;

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
    a="$(ms)"
    perl -e 'open(F, "<", $ARGV[0]) or exit; local $/; my $r = <F>; my $x = $r x 78; sleep 1 while -e $ARGV[1];' "$t/r" "$t/hold" &
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

  conf)  # conf KEY VALUE  — set one node.conf value
    f="$B/node.conf"; touch "$f"; grep -v "^$1=" "$f" > "$f.tmp"; echo "$1=$2" >> "$f.tmp"; mv "$f.tmp" "$f"; echo ok ;;

  recv)  # recv <dir-relative-to-home-or-abs>  — tar on stdin
    t="${1/#\~/$HOME}"; mkdir -p "$t" && tar -xf - -C "$t" && echo ok ;;
  send)  # send <dir>  — tar to stdout (excludes heavy build dirs)
    t="${1/#\~/$HOME}"; tar -cf - -C "$t" --exclude node_modules --exclude .gradle --exclude build --exclude .dart_tool --exclude DerivedData . ;;
  skills-hash) echo "$1" > "$B/skills.hash"; echo ok ;;

  repair)  # re-register the runner service
    bash "$B/bin/setup.sh" --repair && echo repaired ;;
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
  runner-remove) bash "$B/bin/gh-runner.sh" remove "$@" ;;
  *) echo "unknown verb: $verb" >&2; exit 4 ;;
esac
