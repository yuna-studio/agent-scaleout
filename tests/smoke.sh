#!/bin/bash
# mac-scaleout 시험 — 네트워크 · 워커 없이 돈다: bash tests/smoke.sh
# 문법(bash 3.2) · 공용 함수 · 워커 실행기(가짜 claude 로 대기열 → 결과)까지 본다.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PASS=0; FAIL=0
ok()   { PASS=$((PASS + 1)); printf '  ✔ %s\n' "$1"; }
bad()  { FAIL=$((FAIL + 1)); printf '  ✘ %s\n' "$1"; }
check() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (기대 「$3」, 실제 「$2」)"; fi; }

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT

echo "문법 (/bin/bash $(/bin/bash -c 'echo $BASH_VERSION'))"
for f in bin/scaleout lib/common.sh worker/runner.sh worker/setup.sh install.sh; do
  /bin/bash -n "$ROOT/$f" && ok "$f" || bad "$f"
done
{ if command -v jq >/dev/null 2>&1; then jq empty "$ROOT/worker/settings.json"; else python3 -m json.tool "$ROOT/worker/settings.json"; fi; } >/dev/null 2>&1 && ok "settings.json 이 올바른 JSON" || bad "settings.json"

echo "공용 함수"
. "$ROOT/lib/common.sh"
C="$T/a.conf"
conf_set "$C" PORT 2201; conf_set "$C" USER kong; conf_set "$C" PORT 2202
check "conf_set 은 같은 키를 덮어쓴다" "$(conf_get "$C" PORT)" "2202"
check "conf_get" "$(conf_get "$C" USER)" "kong"
check "없는 키는 빈 값" "$(conf_get "$C" NOPE)" ""
S="$T/ssh_config"; printf 'Host keep\n  HostName a\n' > "$S"
ssh_block_put "$S" "scaleout x" "Host scaleout-x
  Port 2201"
ssh_block_put "$S" "scaleout x" "Host scaleout-x
  Port 2203"
check "ssh 블록은 한 번만 들어간다" "$(grep -c '^Host scaleout-x$' "$S")" "1"
check "ssh 블록은 새 값으로 바뀐다" "$(grep -c 'Port 2203' "$S")" "1"
check "다른 Host 는 그대로" "$(grep -c '^Host keep$' "$S")" "1"
ssh_block_del "$S" "scaleout x"
check "ssh 블록 지우기" "$(grep -c 'scaleout-x' "$S")" "0"
mkdir -p "$T/home/.cloudflared"
printf 'tunnel: x\ningress:\n  - hostname: web.example.com\n    service: http://localhost:3000\n  - hostname: ssh.example.com\n    service: ssh://localhost:22\n  - service: http_status:404\n' > "$T/home/.cloudflared/config.yml"
check "Cloudflare 설정에서 SSH 호스트 찾기" "$(HOME="$T/home" main_tunnel_host)" "ssh.example.com"
printf '{"result":"안녕 결과","is_error":false}' > "$T/out.json"
check "claude json 결과 읽기" "$(json_result "$T/out.json")" "안녕 결과"

echo "CLI"
check "help 가 돈다" "$(SCALEOUT_CONF="$T/conf" "$ROOT/bin/scaleout" help | head -1 | cut -c1-8)" "scaleout"
case "$(SCALEOUT_CONF="$T/conf" "$ROOT/bin/scaleout" ls)" in "워커 없음"*) ok "워커 없을 때 ls";; *) bad "워커 없을 때 ls";; esac
echo "일" > "$T/task.md"; SCALEOUT_CONF="$T/conf" "$ROOT/bin/scaleout" run -w nobody -f "$T/task.md" >/dev/null 2>&1
check "없는 워커로 run 은 종료 코드 5" "$?" "5"
SCALEOUT_CONF="$T/conf" "$ROOT/bin/scaleout" wait bad-id >/dev/null 2>&1
check "작업 번호 꼴이 아니면 종료 코드 4" "$?" "4"

echo "워커 실행기 (가짜 claude)"
W="$T/worker"; mkdir -p "$W/.scaleout/queue" "$W/.scaleout/jobs" "$W/bin" "$W/proj"
cat > "$W/bin/claude" <<'EOF'
#!/bin/bash
p="$(cat)"; printf '{"result":"받음: %s · 폴더 %s","is_error":false}\n' "$p" "$(pwd)"
EOF
chmod +x "$W/bin/claude"
for i in 1 2 3; do
  mkdir -p "$W/.scaleout/jobs/j$i"; printf '일%s' "$i" > "$W/.scaleout/jobs/j$i/prompt.md"
  printf '%s' "~/proj" > "$W/.scaleout/jobs/j$i/dir"; touch "$W/.scaleout/queue/j$i"
done
printf 'MAX_JOBS=2\nJOB_MINUTES=1\n' > "$W/.scaleout/worker.conf"
HOME="$W" PATH="$W/bin:/usr/bin:/bin" /bin/bash "$ROOT/worker/runner.sh"
check "대기열이 비워진다" "$(ls "$W/.scaleout/queue" | wc -l | tr -d ' ')" "0"
check "세 일 모두 rc=0" "$(cat "$W"/.scaleout/jobs/j*/rc | tr -d '\n')" "000"
check "지시를 받아 결과를 남긴다" "$(json_result "$W/.scaleout/jobs/j2/out.json" | cut -d' ' -f1-2)" "받음: 일2"
case "$(json_result "$W/.scaleout/jobs/j1/out.json")" in *"$W/proj"*) ok "~ 폴더를 워커 홈으로 펼쳐 그 폴더에서 돈다";; *) bad "작업 폴더";; esac
mkdir -p "$W/.scaleout/jobs/j9"; printf 'x' > "$W/.scaleout/jobs/j9/prompt.md"; printf '/no/such' > "$W/.scaleout/jobs/j9/dir"; touch "$W/.scaleout/queue/j9"
HOME="$W" PATH="$W/bin:/usr/bin:/bin" /bin/bash "$ROOT/worker/runner.sh"
check "없는 폴더면 rc=9" "$(cat "$W/.scaleout/jobs/j9/rc")" "9"

echo
echo "통과 $PASS · 실패 $FAIL"
[ "$FAIL" -eq 0 ]
