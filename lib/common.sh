# mac-scaleout 공용 함수 — bin/scaleout 이 읽는다. macOS 기본 bash 3.2 에서 돌게 쓴다
# (wait -n · 연관 배열 · mapfile · ${var,,} 를 쓰지 않는다).

SCALEOUT_CONF="${SCALEOUT_CONF:-$HOME/.config/scaleout}"
SCALEOUT_KEY="${SCALEOUT_KEY:-$HOME/.ssh/scaleout_ed25519}"
SCALEOUT_PORT_BASE="${SCALEOUT_PORT_BASE:-2201}"

say()  { printf '%s\n' "$*"; }
warn() { printf 'scaleout: %s\n' "$*" >&2; }
die()  { local code="$1"; shift; warn "$*"; exit "$code"; }

# 종료 코드 (README 와 같게)
E_JOB_FAILED=1; E_RUNNING=2; E_UNREACHABLE=3; E_USAGE=4; E_NOT_FOUND=5

conf_get() {  # conf_get <파일> <키> — KEY=값 한 줄씩인 설정에서 값
  [ -f "$1" ] || return 1
  sed -n "s/^$2=//p" "$1" | tail -1
}

conf_set() {  # conf_set <파일> <키> <값>
  local f="$1" k="$2" v="$3"
  mkdir -p "$(dirname "$f")"
  touch "$f"
  if grep -q "^$k=" "$f"; then
    local tmp; tmp="$(mktemp)"
    awk -v k="$k" -v v="$v" -F= 'BEGIN{OFS="="} $1==k {print k, v; next} {print}' "$f" > "$tmp" && mv "$tmp" "$f"
  else
    printf '%s=%s\n' "$k" "$v" >> "$f"
  fi
}

json_result() {  # json_result <claude -p json 파일> — .result 값 (jq 없으면 plutil)
  if command -v jq >/dev/null 2>&1; then
    jq -r '.result // empty' "$1" 2>/dev/null
  else
    plutil -extract result raw -o - "$1" 2>/dev/null
  fi
}

# ~/.ssh/config 의 「# >>> 이름」 ~ 「# <<< 이름」 블록 바꿔 넣기 / 지우기
ssh_block_put() {  # ssh_block_put <설정파일> <이름> <내용>
  local f="$1" name="$2" body="$3" tmp
  mkdir -p "$(dirname "$f")"; touch "$f"; chmod 600 "$f"
  tmp="$(mktemp)"
  awk -v s="# >>> $name" -v e="# <<< $name" '$0==s{skip=1;next} $0==e{skip=0;next} !skip' "$f" > "$tmp"
  { cat "$tmp"; printf '\n# >>> %s\n%s\n# <<< %s\n' "$name" "$body" "$name"; } > "$f"
  rm -f "$tmp"
}

ssh_block_del() {  # ssh_block_del <설정파일> <이름>
  local f="$1" name="$2" tmp
  [ -f "$f" ] || return 0
  tmp="$(mktemp)"
  awk -v s="# >>> $name" -v e="# <<< $name" '$0==s{skip=1;next} $0==e{skip=0;next} !skip' "$f" > "$tmp" && cat "$tmp" > "$f"
  rm -f "$tmp"
}

main_tunnel_host() {  # 메인의 Cloudflare 터널에서 ssh://localhost:22 로 가는 hostname
  local cfg="$HOME/.cloudflared/config.yml"
  [ -f "$cfg" ] || return 1
  awk '/hostname:/ {h=$NF} /service:[[:space:]]*ssh:\/\/(localhost|127\.0\.0\.1):22/ {print h; exit}' "$cfg"
}

free_mem_pct() {  # 이 기기의 남은 메모리 % (kern.memorystatus_level)
  sysctl -n kern.memorystatus_level 2>/dev/null || echo 0
}
