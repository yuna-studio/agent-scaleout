#!/bin/sh
# agent-scaleout — orchestrator installer (macOS / Linux). Run once on the machine that hands out work.
#
#   curl -fsSL https://raw.githubusercontent.com/yuna-studio/agent-scaleout/main/install.sh | sh
#   # or from a clone:  ./install.sh [scaleout init options, e.g. --tunnel-host ssh.example.com --tunnel-proxy "cloudflared access ssh --hostname %h"]
#
# Installs into ~/.local/share/agent-scaleout and links ~/.local/bin/scaleout. Needs python3 (3.9+), ssh, tar.
# Nodes need nothing from you here: attach them with `scaleout add` or `scaleout invite`.
set -e
REPO="${SCALEOUT_REPO:-https://github.com/yuna-studio/agent-scaleout}"
DEST="$HOME/.local/share/agent-scaleout"
command -v python3 >/dev/null || { echo "python3 is required (macOS: xcode-select --install)"; exit 1; }
command -v ssh >/dev/null || { echo "ssh is required"; exit 1; }

SRC="$(cd "$(dirname "$0")" 2>/dev/null && pwd || true)"
if [ -z "$SRC" ] || [ ! -f "$SRC/orchestrator/scaleout.py" ]; then   # piped from curl: fetch the repo
  TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
  if command -v git >/dev/null; then git clone -q --depth 1 "$REPO" "$TMP/src"
  else curl -fsSL "$REPO/archive/refs/heads/main.tar.gz" | tar -xz -C "$TMP" && mv "$TMP"/agent-scaleout-* "$TMP/src"; fi
  SRC="$TMP/src"
fi

mkdir -p "$DEST" "$HOME/.local/bin"
for d in orchestrator node agents tools plugin .claude-plugin; do rm -rf "$DEST/$d"; [ -d "$SRC/$d" ] && cp -R "$SRC/$d" "$DEST/$d"; done
cp "$SRC/README.md" "$SRC/LICENSE" "$DEST/" 2>/dev/null || true
chmod +x "$DEST/orchestrator/scaleout.py" "$DEST/tools/"*.sh "$DEST/node/unix/"*.sh
ln -sf "$DEST/orchestrator/scaleout.py" "$HOME/.local/bin/scaleout"
"$HOME/.local/bin/scaleout" init "$@"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo; echo "Add ~/.local/bin to PATH:  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc  (or ~/.bashrc)";; esac
echo "Optional: keep the scheduler running in the background:  scaleout install-daemon"
