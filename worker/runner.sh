#!/bin/bash
# mac-scaleout 워커 실행기 — 로그인 세션의 LaunchAgent(dev.scaleout.runner, QueueDirectories ~/.scaleout/queue)가 부른다.
# SSH 세션에서는 맥 키체인(Claude 로그인)을 못 읽어서, Claude 일은 꼭 여기(로그인 세션)에서 돌린다.
# 메인이 ~/.scaleout/jobs/<번호>/{prompt.md,dir} 를 쓰고 ~/.scaleout/queue/<번호> 를 만들면 이 스크립트가 집어 간다.
# bash 3.2 (macOS 기본) 에서 돌게 쓴다.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
B="$HOME/.scaleout"; Q="$B/queue"; J="$B/jobs"
MAX_JOBS=2; JOB_MINUTES=180
[ -f "$B/worker.conf" ] && . "$B/worker.conf"
mkdir -p "$Q" "$J"

running() { jobs -rp | wc -l | tr -d ' '; }

run_job() {  # run_job <작업 폴더>
  local d="$1" dir s=()
  dir="$(cat "$d/dir" 2>/dev/null || echo "$HOME")"; dir="${dir/#\~/$HOME}"
  cd "$dir" 2>/dev/null || { echo "폴더 없음: $dir" > "$d/err.log"; echo 9 > "$d/rc"; return; }
  [ -f "$B/settings.json" ] && s=(--settings "$B/settings.json")
  date '+%F %T' > "$d/started"
  claude -p --output-format json "${s[@]}" < "$d/prompt.md" > "$d/out.json" 2> "$d/err.log" &
  local cpid=$!
  echo "$cpid" > "$d/pid"
  # 시간 한도 — 넘으면 끝낸다
  ( sleep $((JOB_MINUTES * 60)); kill -TERM "$cpid" 2>/dev/null; echo "시간 한도(${JOB_MINUTES}분) 넘어 끝냄" >> "$d/err.log" ) &
  local wpid=$!
  wait "$cpid"; local rc=$?
  kill "$wpid" 2>/dev/null; wait "$wpid" 2>/dev/null
  [ -f "$d/cancelled" ] && rc=130
  echo "$rc" > "$d/rc"
  date '+%F %T' > "$d/finished"
}

while :; do
  set -- "$Q"/*
  if [ ! -e "$1" ]; then
    [ "$(running)" -eq 0 ] && break
    sleep 2; continue
  fi
  for f in "$@"; do
    while [ "$(running)" -ge "$MAX_JOBS" ]; do sleep 2; done
    id="$(basename "$f")"; rm -f "$f"
    [ -d "$J/$id" ] || continue
    run_job "$J/$id" &
  done
done
wait
