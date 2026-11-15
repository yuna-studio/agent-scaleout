#!/bin/bash
# mac-scaleout 설치 (메인 맥에서 한 번)
#   gh repo clone yuna-studio/mac-scaleout && ./mac-scaleout/install.sh
#   ./install.sh --main-host macmini-ssh.example.com   # Cloudflare SSH 터널 호스트를 직접 줄 때
# 그다음 워커마다: scaleout add <이름> <사용자@주소>   (워커는 원격 로그인 · Claude Code 로그인만 미리)
set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.local/share/scaleout"
mkdir -p "$DEST" "$HOME/.local/bin"
# 패키지를 고정 자리로 복사 — 받은 폴더를 지워도 계속 돈다
for d in bin lib worker claude; do rm -rf "$DEST/$d"; cp -R "$SRC/$d" "$DEST/$d"; done
cp "$SRC/README.md" "$DEST/README.md" 2>/dev/null || true
chmod +x "$DEST/bin/scaleout" "$DEST/worker/"*.sh
ln -sf "$DEST/bin/scaleout" "$HOME/.local/bin/scaleout"
"$HOME/.local/bin/scaleout" init "$@"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo; echo "※ ~/.local/bin 이 PATH 에 없어요. ~/.zshrc 에 넣어 주세요: export PATH=\"\$HOME/.local/bin:\$PATH\"";;
esac
