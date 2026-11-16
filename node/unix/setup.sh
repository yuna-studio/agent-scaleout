#!/bin/bash
# agent-scaleout node setup (macOS / Linux). Normally the orchestrator uploads the package and runs this for you
# (`scaleout add` or the `scaleout invite` one-liner). You can also run it by hand from the unpacked package:
#
#   bash setup.sh --name build-01 [--dir ~/scaleout-work] [--max-jobs 2] [--agent claude|codex|gemini|"<command>"]
#                 [--tunnel-host HOST --tunnel-user USER --tunnel-port 2201 [--tunnel-ssh-port 22] [--tunnel-proxy "cloudflared access ssh --hostname %h"]]
#   bash setup.sh --repair       re-register services
#   bash setup.sh --uninstall    remove services (job history in ~/.scaleout/jobs is kept)
#
# Prints SCALEOUT_* lines that the orchestrator reads.
set -u
B="$HOME/.scaleout"; PKG="$(cd "$(dirname "$0")" && pwd)"; OS="$(uname -s)"
NAME=""; DIR="~/scaleout-work"; MAX_JOBS=2; AGENT=claude; AGENT_CMD=""
TH=""; TU=""; TP=""; TSP=22; TPROXY=""; MODE=install
while [ $# -gt 0 ]; do
  case "$1" in
    --name) NAME="$2"; shift 2 ;;
    --dir) DIR="$2"; shift 2 ;;
    --max-jobs) MAX_JOBS="$2"; shift 2 ;;
    --agent) case "$2" in claude|codex|gemini) AGENT="$2";; *) AGENT=custom; AGENT_CMD="$2";; esac; shift 2 ;;
    --tunnel-host) TH="$2"; shift 2 ;;
    --tunnel-user) TU="$2"; shift 2 ;;
    --tunnel-port) TP="$2"; shift 2 ;;
    --tunnel-ssh-port) TSP="$2"; shift 2 ;;
    --tunnel-proxy) TPROXY="$2"; shift 2 ;;
    --repair) MODE=repair; shift ;;
    --uninstall) MODE=uninstall; shift ;;
    *) echo "unknown option: $1" >&2; exit 4 ;;
  esac
done

LA="$HOME/Library/LaunchAgents"; SU="$HOME/.config/systemd/user"
has_systemd() { [ "$OS" != Darwin ] && command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; }

ssh_block_del() {
  local f="$HOME/.ssh/config" t; [ -f "$f" ] || return 0
  t="$(mktemp)"; awk '$0=="# >>> scaleout-orchestrator"{s=1;next} $0=="# <<< scaleout-orchestrator"{s=0;next} !s' "$f" > "$t" && cat "$t" > "$f"; rm -f "$t"
}

mac_plist() {  # mac_plist <label> <extra xml> <args...>
  local label="$1" extra="$2"; shift 2; local f="$LA/$label.plist"
  mkdir -p "$LA"
  { printf '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
    printf '<plist version="1.0"><dict>\n<key>Label</key><string>%s</string>\n<key>ProgramArguments</key><array>' "$label"
    for a in "$@"; do printf '<string>%s</string>' "$a"; done
    printf '</array>\n%s\n</dict></plist>\n' "$extra"; } > "$f"
  plutil -lint -s "$f" >/dev/null || { echo "bad plist $f" >&2; exit 1; }
}
mac_load() {  # mac_load <label> [later]   — "later" reloads a few seconds from now (the orchestrator may be connected through it)
  local u; u="$(id -u)"
  if [ "${2:-}" = later ]; then
    nohup /bin/bash -c "sleep 3; launchctl bootout gui/$u/$1 2>/dev/null; launchctl bootstrap gui/$u $LA/$1.plist" >/dev/null 2>&1 &
  else
    launchctl bootout "gui/$u/$1" 2>/dev/null; launchctl bootstrap "gui/$u" "$LA/$1.plist" 2>/dev/null || launchctl kickstart -k "gui/$u/$1" 2>/dev/null
  fi
}

uninstall() {
  if [ "$OS" = Darwin ]; then
    for l in dev.scaleout.runner dev.scaleout.tunnel; do launchctl bootout "gui/$(id -u)/$l" 2>/dev/null; rm -f "$LA/$l.plist"; done
  elif has_systemd; then
    systemctl --user disable --now scaleout-runner.path scaleout-tunnel.service 2>/dev/null
    rm -f "$SU"/scaleout-runner.* "$SU"/scaleout-tunnel.service; systemctl --user daemon-reload 2>/dev/null
  fi
  ( crontab -l 2>/dev/null | grep -v scaleout ) | crontab - 2>/dev/null
  ssh_block_del; rm -f "$HOME/.ssh/scaleout_orchestrator" "$HOME/.ssh/scaleout_orchestrator.pub"
  rm -rf "$B/bin" "$B/queue"
  echo "SCALEOUT_UNINSTALLED"
}
[ "$MODE" = uninstall ] && { uninstall; exit 0; }

install_runner_service() {
  if [ "$OS" = Darwin ]; then
    mac_plist dev.scaleout.runner "<key>QueueDirectories</key><array><string>$B/queue</string></array>
<key>StandardOutPath</key><string>$B/runner.log</string><key>StandardErrorPath</key><string>$B/runner.log</string>" /bin/bash "$B/bin/runner.sh"
    mac_load dev.scaleout.runner; echo "SCALEOUT_RUNNER launchd"
  elif has_systemd; then
    mkdir -p "$SU"
    printf '[Unit]\nDescription=agent-scaleout runner\n[Service]\nType=oneshot\nExecStart=/bin/bash %%h/.scaleout/bin/runner.sh\n' > "$SU/scaleout-runner.service"
    printf '[Unit]\nDescription=agent-scaleout queue watch\n[Path]\nPathExistsGlob=%%h/.scaleout/queue/*\nUnit=scaleout-runner.service\n[Install]\nWantedBy=default.target\n' > "$SU/scaleout-runner.path"
    systemctl --user daemon-reload && systemctl --user enable --now scaleout-runner.path >/dev/null 2>&1
    loginctl enable-linger "$(whoami)" >/dev/null 2>&1 || echo "SCALEOUT_NOTE run once with sudo so the runner works while logged out: sudo loginctl enable-linger $(whoami)"
    echo "SCALEOUT_RUNNER systemd"
  else
    ( crontab -l 2>/dev/null | grep -v scaleout; echo "* * * * * [ -n \"\$(ls \$HOME/.scaleout/queue 2>/dev/null)\" ] && /bin/bash \$HOME/.scaleout/bin/runner.sh >> \$HOME/.scaleout/runner.log 2>&1" ) | crontab -
    echo "SCALEOUT_RUNNER cron"
  fi
}

if [ "$MODE" = repair ]; then
  [ -f "$B/node.conf" ] && . "$B/node.conf"
  install_runner_service; exit 0
fi

[ -n "$NAME" ] || { echo "--name is required" >&2; exit 4; }
mkdir -p "$B/bin" "$B/queue" "$B/jobs" "$B/work" "$B/settings"; chmod 700 "$B"
for f in ctl.sh runner.sh setup.sh gh-runner.sh; do [ -f "$PKG/$f" ] && cp "$PKG/$f" "$B/bin/$f"; done
mv -f "$B/bin/ctl.sh" "$B/bin/ctl"; chmod 755 "$B/bin/"*
printf '#!/bin/bash\nnohup /bin/bash "$HOME/.scaleout/bin/runner.sh" >> "$HOME/.scaleout/runner.log" 2>&1 &\n' > "$B/bin/kick"; chmod 755 "$B/bin/kick"
# agent permissions: keep the user's edited copy
if [ ! -f "$B/settings/claude.json" ]; then
  [ -f "$PKG/claude-settings.json" ] && cp "$PKG/claude-settings.json" "$B/settings/claude.json"
elif [ -f "$PKG/claude-settings.json" ] && command -v python3 >/dev/null 2>&1; then
  # keep the node's own edits, add allow/deny rules and folders that new versions introduce
  python3 - "$B/settings/claude.json" "$PKG/claude-settings.json" <<'PY' || true
import json, sys
mine, new = (json.load(open(p)) for p in sys.argv[1:3])
mp, np = mine.setdefault("permissions", {}), new.get("permissions", {})
for k in ("allow", "deny", "additionalDirectories"):
    cur = mp.setdefault(k, [])
    cur += [x for x in np.get(k, []) if x not in cur]
json.dump(mine, open(sys.argv[1], "w"), indent=2, ensure_ascii=False)
PY
fi
cat > "$B/node.conf" <<EOF
NAME=$NAME
WORK_ROOT=$DIR
MAX_JOBS=$MAX_JOBS
AGENT=$AGENT
AGENT_CMD='$AGENT_CMD'
MIN_FREE_GB=2
EOF
install_runner_service

# optional reverse tunnel: node -> orchestrator, opening orchestrator:localhost:PORT -> this node's sshd
if [ -n "$TH" ] && [ -n "$TP" ]; then
  if [ -n "$TPROXY" ]; then
    pbin="${TPROXY%% *}"
    if ! command -v "$pbin" >/dev/null 2>&1 && command -v brew >/dev/null 2>&1; then brew install "$pbin" >/dev/null 2>&1; fi
    p="$(command -v "$pbin" 2>/dev/null)"; [ -n "$p" ] && TPROXY="$p ${TPROXY#* }"
  fi
  mkdir -p "$HOME/.ssh"; chmod 700 "$HOME/.ssh"
  [ -f "$HOME/.ssh/scaleout_orchestrator" ] || ssh-keygen -q -t ed25519 -N "" -f "$HOME/.ssh/scaleout_orchestrator" -C "scaleout-$NAME"
  ssh_block_del; touch "$HOME/.ssh/config"; chmod 600 "$HOME/.ssh/config"
  {
    printf '\n# >>> scaleout-orchestrator\nHost scaleout-orchestrator\n  HostName %s\n  Port %s\n  User %s\n' "$TH" "$TSP" "$TU"
    printf '  IdentityFile ~/.ssh/scaleout_orchestrator\n  IdentitiesOnly yes\n'
    [ -n "$TPROXY" ] && printf '  ProxyCommand %s\n' "$TPROXY"
    printf '  ServerAliveInterval 30\n  ServerAliveCountMax 3\n  ExitOnForwardFailure yes\n  StrictHostKeyChecking accept-new\n  RemoteForward %s localhost:22\n# <<< scaleout-orchestrator\n' "$TP"
  } >> "$HOME/.ssh/config"
  SIG="$( { awk '$0=="# >>> scaleout-orchestrator"{p=1} p; $0=="# <<< scaleout-orchestrator"{p=0}' "$HOME/.ssh/config"; } | shasum 2>/dev/null | cut -d' ' -f1)"
  if [ "$OS" = Darwin ]; then
    mac_plist dev.scaleout.tunnel "<key>RunAtLoad</key><true/><key>KeepAlive</key><true/><key>ThrottleInterval</key><integer>30</integer>
<key>StandardErrorPath</key><string>$B/tunnel.log</string>" /usr/bin/ssh -N scaleout-orchestrator
    if [ "$(cat "$B/tunnel.sig" 2>/dev/null)" = "$SIG" ] && launchctl print "gui/$(id -u)/dev.scaleout.tunnel" >/dev/null 2>&1; then echo "SCALEOUT_TUNNEL unchanged"
    else echo "$SIG" > "$B/tunnel.sig"; mac_load dev.scaleout.tunnel later; echo "SCALEOUT_TUNNEL reloading"; fi
  elif has_systemd; then
    mkdir -p "$SU"
    printf '[Unit]\nDescription=agent-scaleout reverse tunnel\nAfter=network-online.target\n[Service]\nExecStart=/usr/bin/ssh -N scaleout-orchestrator\nRestart=always\nRestartSec=30\n[Install]\nWantedBy=default.target\n' > "$SU/scaleout-tunnel.service"
    systemctl --user daemon-reload
    if [ "$(cat "$B/tunnel.sig" 2>/dev/null)" = "$SIG" ] && systemctl --user is-active scaleout-tunnel.service >/dev/null 2>&1; then echo "SCALEOUT_TUNNEL unchanged"
    else echo "$SIG" > "$B/tunnel.sig"; nohup /bin/bash -c "sleep 3; systemctl --user enable scaleout-tunnel.service; systemctl --user restart scaleout-tunnel.service" >/dev/null 2>&1 & echo "SCALEOUT_TUNNEL reloading"; fi
  else
    echo "SCALEOUT_NOTE no service manager for the tunnel; run: ssh -fN scaleout-orchestrator"
  fi
  echo "SCALEOUT_PUBKEY $(cat "$HOME/.ssh/scaleout_orchestrator.pub")"
fi

echo "SCALEOUT_OS $OS"
echo "SCALEOUT_USER $(whoami)"
echo "SCALEOUT_HOSTKEY $(cut -d' ' -f1,2 /etc/ssh/ssh_host_ed25519_key.pub 2>/dev/null)"
echo "SCALEOUT_LAN $( (ipconfig getifaddr en0 || ipconfig getifaddr en1) 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}')"
echo "SCALEOUT_AGENTS $("$B/bin/ctl" metrics | sed -n 's/.*"agents":\(\[[^]]*\]\).*/\1/p')"
