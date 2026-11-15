#!/bin/bash
# GitHub Actions self-hosted runner on a node (macOS / Linux). Called by `ctl runner-install|runner-remove`.
#   gh-runner.sh install <url> <registration-token> <name> <labels>
#   gh-runner.sh remove  <name> <removal-token>
# Runners live in ~/.scaleout/gh-runners/<name> and run as the node user (launchd on macOS, systemd --user on Linux).
set -eu
R="$HOME/.scaleout/gh-runners"; OS="$(uname -s)"; ARCH="$(uname -m)"
case "$OS" in Darwin) os=osx;; Linux) os=linux;; *) echo "unsupported OS $OS" >&2; exit 4;; esac
case "$ARCH" in arm64|aarch64) arch=arm64;; x86_64|amd64) arch=x64;; armv7l) arch=arm;; *) echo "unsupported arch $ARCH" >&2; exit 4;; esac

cmd="${1:-}"; shift || true
case "$cmd" in
  install)
    url="$1"; token="$2"; name="$3"; labels="${4:-}"
    d="$R/$name"; mkdir -p "$d"; cd "$d"
    if [ ! -x ./config.sh ]; then
      ver="$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest | sed -n 's/.*"tag_name": *"v\([^"]*\)".*/\1/p' | head -1)"
      [ -n "$ver" ] || { echo "cannot find the latest runner release" >&2; exit 1; }
      curl -fsSL -o runner.tgz "https://github.com/actions/runner/releases/download/v$ver/actions-runner-$os-$arch-$ver.tar.gz"
      tar -xzf runner.tgz && rm -f runner.tgz
    fi
    ./config.sh --unattended --replace --url "$url" --token "$token" --name "$name" --labels "$labels" --work _work
    if [ "$OS" = Darwin ]; then
      ./svc.sh install >/dev/null && ./svc.sh start >/dev/null
    else
      mkdir -p "$HOME/.config/systemd/user"
      printf '[Unit]\nDescription=GitHub runner %s\n[Service]\nWorkingDirectory=%s\nExecStart=%s/run.sh\nRestart=always\n[Install]\nWantedBy=default.target\n' "$name" "$d" "$d" \
        > "$HOME/.config/systemd/user/scaleout-gh-$name.service"
      systemctl --user daemon-reload && systemctl --user enable --now "scaleout-gh-$name.service"
    fi
    echo "runner $name installed" ;;
  remove)
    name="$1"; token="$2"; d="$R/$name"
    [ -d "$d" ] || { echo "no runner $name"; exit 0; }
    cd "$d"
    if [ "$OS" = Darwin ]; then ./svc.sh stop >/dev/null 2>&1 || true; ./svc.sh uninstall >/dev/null 2>&1 || true
    else systemctl --user disable --now "scaleout-gh-$name.service" 2>/dev/null || true; rm -f "$HOME/.config/systemd/user/scaleout-gh-$name.service"; fi
    ./config.sh remove --token "$token" || true
    cd "$R" && rm -rf "$d"
    echo "runner $name removed" ;;
  *) echo "usage: gh-runner.sh install <url> <token> <name> <labels> | remove <name> <token>" >&2; exit 4 ;;
esac
