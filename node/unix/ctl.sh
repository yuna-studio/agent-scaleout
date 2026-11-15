#!/bin/bash
# agent-scaleout node control (macOS / Linux). The orchestrator calls this over SSH:
#   ~/.scaleout/bin/ctl <verb> [args]
# Every verb prints plain text or one JSON line, so the orchestrator never needs to know the node's OS.
# Works with bash 3.2 (stock macOS): no `wait -n`, no associative arrays, and ${var} before non-ASCII text.
set -u
B="$HOME/.scaleout"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
[ -f "$B/node.conf" ] && . "$B/node.conf"
OS="$(uname -s)"
VERSION="__VERSION__"

j() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }   # JSON-escape a short string

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
    printf '{"os":"%s","arch":"%s","host":"%s","mem_free_pct":%s,"mem_total_gb":%s,"disk_free_gb":%s,"work_free_gb":%s,"load":%s,"cpus":%s,"running":%s,"queued":%s,"runner":%s,"agents":%s,"skills_hash":"%s","version":"%s"}\n' \
      "$(j "$OS")" "$(uname -m)" "$(j "$(hostname)")" "$(mem_free_pct)" "$(mem_total_gb)" "$(disk_free_gb "$HOME")" \
      "$(disk_free_gb "${WORK_ROOT:-$HOME}")" "$(load1)" "$(cpus)" "$(running_jobs)" "$(queued_jobs)" "$(runner_ok)" "$(agents)" "$(skills_hash)" "$VERSION" ;;

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
    if [ -f "$d/rc" ]; then printf '{"state":"done","rc":%s}\n' "$(tr -dc '0-9' < "$d/rc")"
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
