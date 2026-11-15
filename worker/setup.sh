#!/bin/bash
# mac-scaleout 워커 설치 — 보통은 메인의 `scaleout add` 가 SSH 로 이 파일을 올려 대신 돌린다.
# 워커에서 직접 돌려도 된다(메인과 같은 망이 아닐 때): 끝에 찍히는 SCALEOUT_PUBKEY 줄을 메인에서 `scaleout accept` 로 넣는다.
#
#   bash setup.sh --name macbook [--dir ~/workspace] [--max-jobs 2]
#                 [--main-host macmini-ssh.example.com --main-user me --port 2201]   # 역방향 터널 (워커가 어느 망에 있어도 붙게)
#   bash setup.sh --uninstall
#
# 워커에 미리 있어야 하는 것: 원격 로그인(시스템 설정 → 일반 → 공유) · Claude Code 로그인 · (터널을 쓰면) Homebrew
set -u
B="$HOME/.scaleout"
PKG="$(cd "$(dirname "$0")" && pwd)"
UIDN="$(id -u)"
LA="$HOME/Library/LaunchAgents"
RUNNER_LABEL="dev.scaleout.runner"
TUNNEL_LABEL="dev.scaleout.tunnel"

NAME=""; DIR="~"; MAX_JOBS=2; MAIN_HOST=""; MAIN_USER=""; PORT=""; UNINSTALL=0
while [ $# -gt 0 ]; do
  case "$1" in
    --name) NAME="$2"; shift 2 ;;
    --dir) DIR="$2"; shift 2 ;;
    --max-jobs) MAX_JOBS="$2"; shift 2 ;;
    --main-host) MAIN_HOST="$2"; shift 2 ;;
    --main-user) MAIN_USER="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --uninstall) UNINSTALL=1; shift ;;
    *) echo "모르는 옵션: $1" >&2; exit 4 ;;
  esac
done

block_del() {  # ~/.ssh/config 의 scaleout-main 블록 지우기
  local f="$HOME/.ssh/config" tmp
  [ -f "$f" ] || return 0
  tmp="$(mktemp)"
  awk '$0=="# >>> scaleout-main"{skip=1;next} $0=="# <<< scaleout-main"{skip=0;next} !skip' "$f" > "$tmp" && cat "$tmp" > "$f"
  rm -f "$tmp"
}

if [ "$UNINSTALL" = 1 ]; then
  for l in "$RUNNER_LABEL" "$TUNNEL_LABEL"; do
    launchctl bootout "gui/$UIDN/$l" 2>/dev/null
    rm -f "$LA/$l.plist"
  done
  block_del
  rm -f "$HOME/.ssh/scaleout_to_main" "$HOME/.ssh/scaleout_to_main.pub"
  rm -rf "$B/pkg" "$B/queue"
  echo "SCALEOUT_UNINSTALLED (작업 기록 ~/.scaleout/jobs 는 남김)"
  exit 0
fi

[ -n "$NAME" ] || { echo "--name 이 필요해요" >&2; exit 4; }
mkdir -p "$B/queue" "$B/jobs" "$LA"
chmod 700 "$B"

# 실행기 · 허용 목록 (허용 목록은 이미 있으면 그대로 둔다 — 사용자가 고쳤을 수 있다)
cp "$PKG/runner.sh" "$B/runner.sh" && chmod 755 "$B/runner.sh"
[ -f "$B/settings.json" ] || cp "$PKG/settings.json" "$B/settings.json"
cat > "$B/worker.conf" <<EOF
NAME=$NAME
DIR=$DIR
MAX_JOBS=$MAX_JOBS
JOB_MINUTES=180
EOF

plist() {  # plist <파일> <라벨> <추가 키 XML> <실행 인자...>
  local f="$1" label="$2" extra="$3"; shift 3
  {
    printf '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
    printf '<plist version="1.0"><dict>\n<key>Label</key><string>%s</string>\n<key>ProgramArguments</key><array>' "$label"
    for a in "$@"; do printf '<string>%s</string>' "$a"; done
    printf '</array>\n%s\n</dict></plist>\n' "$extra"
  } > "$f"
  plutil -lint -s "$f" >/dev/null || { echo "plist 오류: $f" >&2; exit 1; }
}

load() {  # load <라벨> — 다시 올리기
  launchctl bootout "gui/$UIDN/$1" 2>/dev/null
  launchctl bootstrap "gui/$UIDN" "$LA/$1.plist" 2>/dev/null || launchctl kickstart -k "gui/$UIDN/$1" 2>/dev/null
}

plist "$LA/$RUNNER_LABEL.plist" "$RUNNER_LABEL" \
  "<key>QueueDirectories</key><array><string>$B/queue</string></array>
<key>StandardOutPath</key><string>$B/runner.log</string><key>StandardErrorPath</key><string>$B/runner.log</string>" \
  /bin/bash "$B/runner.sh"
load "$RUNNER_LABEL"
echo "SCALEOUT_RUNNER ok"

# 역방향 터널 — 워커가 메인의 Cloudflare SSH 로 붙으며 메인 localhost:PORT → 워커 22 를 연다
if [ -n "$MAIN_HOST" ] && [ -n "$PORT" ]; then
  export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
  CF="$(command -v cloudflared || true)"
  if [ -z "$CF" ] && command -v brew >/dev/null 2>&1; then
    brew install cloudflared >/dev/null 2>&1
    CF="$(command -v cloudflared || true)"
  fi
  if [ -z "$CF" ]; then
    echo "SCALEOUT_TUNNEL 건너뜀 — cloudflared 가 없고 Homebrew 로도 못 깔았어요" >&2
  else
    mkdir -p "$HOME/.ssh"; chmod 700 "$HOME/.ssh"
    [ -f "$HOME/.ssh/scaleout_to_main" ] || ssh-keygen -q -t ed25519 -N "" -f "$HOME/.ssh/scaleout_to_main" -C "scaleout-$NAME"
    block_del
    touch "$HOME/.ssh/config"; chmod 600 "$HOME/.ssh/config"
    cat >> "$HOME/.ssh/config" <<EOF

# >>> scaleout-main
Host scaleout-main
  HostName $MAIN_HOST
  User $MAIN_USER
  IdentityFile ~/.ssh/scaleout_to_main
  IdentitiesOnly yes
  ProxyCommand $CF access ssh --hostname %h
  ServerAliveInterval 30
  ServerAliveCountMax 3
  ExitOnForwardFailure yes
  StrictHostKeyChecking accept-new
  RemoteForward $PORT localhost:22
# <<< scaleout-main
EOF
    plist "$LA/$TUNNEL_LABEL.plist" "$TUNNEL_LABEL" \
      "<key>RunAtLoad</key><true/><key>KeepAlive</key><true/><key>ThrottleInterval</key><integer>30</integer>
<key>StandardErrorPath</key><string>$B/tunnel.log</string>" \
      /usr/bin/ssh -N scaleout-main
    # 설정이 그대로이고 이미 돌고 있으면 다시 올리지 않는다 — 메인이 이 터널로 들어와 설치 중일 수 있다
    SIG="$( { cat "$LA/$TUNNEL_LABEL.plist"; awk '$0=="# >>> scaleout-main"{p=1} p; $0=="# <<< scaleout-main"{p=0}' "$HOME/.ssh/config"; } | shasum | cut -d' ' -f1)"
    if [ "$(cat "$B/tunnel.sig" 2>/dev/null)" = "$SIG" ] && launchctl print "gui/$UIDN/$TUNNEL_LABEL" >/dev/null 2>&1; then
      echo "SCALEOUT_TUNNEL 그대로"
    else
      echo "$SIG" > "$B/tunnel.sig"
      # 몇 초 뒤 따로 다시 올린다 — 지금 이 설치가 그 터널을 타고 있어도 끝까지 출력하고 끝나게
      nohup /bin/bash -c "sleep 3; launchctl bootout gui/$UIDN/$TUNNEL_LABEL 2>/dev/null; launchctl bootstrap gui/$UIDN $LA/$TUNNEL_LABEL.plist" >/dev/null 2>&1 &
      echo "SCALEOUT_TUNNEL 다시 올림"
    fi
    echo "SCALEOUT_PUBKEY $(cat "$HOME/.ssh/scaleout_to_main.pub")"
  fi
fi

# 메인이 known_hosts 에 넣을 이 워커의 호스트 키 · 상태
echo "SCALEOUT_HOSTKEY $(cut -d' ' -f1,2 /etc/ssh/ssh_host_ed25519_key.pub 2>/dev/null)"
C="$(PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH" command -v claude || echo none)"
echo "SCALEOUT_CLAUDE $C"
echo "SCALEOUT_USER $(whoami)"
