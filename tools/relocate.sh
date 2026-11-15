#!/bin/bash
# relocate.sh — 무거운 폴더를 다른 디스크(외장 SSD 등)로 옮기고 원래 자리에 링크를 남긴다. 경로를 쓰는 설정 · 서비스는 그대로 돈다.
#
#   tools/relocate.sh copy <대상 뿌리> <폴더...>   1단계: 복사만 (원본은 그대로, 쓰는 중이어도 됨)
#   tools/relocate.sh swap <대상 뿌리> <폴더...>   2단계: 바뀐 것만 다시 복사 → 원본을 비켜 두고 링크 → 원본 지움
#   tools/relocate.sh back <폴더...>               되돌리기: 링크 자리로 다시 복사해 옮겨 온다
#
# 예) tools/relocate.sh copy /Volumes/SSD ~/workspace ~/.gradle  &&  tools/relocate.sh swap /Volumes/SSD ~/workspace ~/.gradle
# 대상 자리: <대상 뿌리>/home/<홈 기준 경로>  (예: ~/workspace → /Volumes/SSD/home/workspace)
# KEEP_OLD=1 이면 옛 폴더를 지우지 않고 비켜 둔다(켜져 있는 프로그램의 현재 폴더일 때).
# Claude Code 는 프로젝트 기록을 실제 경로로 찾으므로, 옮긴 폴더 안의 프로젝트 기록(~/.claude/projects/<키>)에 새 경로 키 링크를 만든다.
set -u
mode="${1:-}"; shift || true

key_of() { printf '%s' "$1" | sed 's/[^A-Za-z0-9]/-/g'; }   # Claude Code 프로젝트 키 꼴

claude_keys() {  # claude_keys <원래 경로> <새 실제 경로>
  local old="$1" new="$2" p="$HOME/.claude/projects" ok nk k
  [ -d "$p" ] || return 0
  ok="$(key_of "$old")"; nk="$(key_of "$new")"
  for k in "$p/$ok" "$p/$ok"-*; do
    [ -e "$k" ] || continue
    k="$(basename "$k")"
    local t="$p/$nk${k#"$ok"}"
    [ -e "$t" ] || { ln -s "$p/$k" "$t" && echo "  Claude 기록 키 링크: ${t##*/} → $k"; }
  done
}

dest_of() {  # dest_of <대상 뿌리> <폴더>
  local src="$1" root="$2"
  case "$src" in "$HOME"/*) echo "$root/home/${src#"$HOME"/}";; /*) echo "$root/abs$src";; *) echo "$root/home/$src";; esac
}

copy_one() {  # copy_one <원본> <대상>
  mkdir -p "$2"
  rsync -a --delete "$1/" "$2/"
}

case "$mode" in
  copy|swap)
    root="${1:-}"; shift || true
    [ -d "$root" ] || { echo "대상 뿌리가 없어요: $root" >&2; exit 4; }
    for src in "$@"; do
      src="${src%/}"
      if [ -L "$src" ]; then echo "건너뜀(이미 링크): $src → $(readlink "$src")"; continue; fi
      [ -d "$src" ] || { echo "건너뜀(폴더 아님): $src"; continue; }
      dst="$(dest_of "$src" "$root")"
      echo "▶ $src → $dst"
      copy_one "$src" "$dst" || { echo "  복사 실패 — 원본 그대로 둠" >&2; continue; }
      [ "$mode" = copy ] && { echo "  복사 끝 ($(du -sh "$dst" | cut -f1))"; continue; }
      # 바꿔치기 직전 한 번 더 — 그 사이 바뀐 것만
      copy_one "$src" "$dst" || { echo "  다시 복사 실패 — 원본 그대로 둠" >&2; continue; }
      a="$(find "$src" -type f 2>/dev/null | wc -l | tr -d ' ')"; b="$(find "$dst" -type f 2>/dev/null | wc -l | tr -d ' ')"
      if [ "$a" != "$b" ]; then echo "  파일 수가 달라요(원본 $a · 대상 $b) — 원본 그대로 둠" >&2; continue; fi
      old="$src.relocating-$$"
      mv "$src" "$old" && ln -s "$dst" "$src" || { echo "  링크 실패 — 되돌림" >&2; [ -e "$old" ] && [ ! -e "$src" ] && mv "$old" "$src"; continue; }
      claude_keys "$src" "$dst"
      if [ "${KEEP_OLD:-0}" = 1 ]; then
        # 그 폴더를 작업 폴더로 쓰는 프로그램(예: 켜져 있는 Claude Code)이 있으면, 옛 폴더를 바로 지우면 그 프로그램의 현재 폴더가 사라져 깨진다
        echo "  옮김 (파일 ${b}개) — 옛 폴더는 $old 에 남김. 그 폴더를 쓰던 프로그램을 다시 켠 뒤 지우세요: rm -rf \"$old\""
      else
        rm -rf "$old"
        echo "  옮김 (파일 ${b}개, $(du -sh "$dst" | cut -f1)) — $src 는 이제 링크"
      fi
    done ;;
  back)
    for src in "$@"; do
      src="${src%/}"
      [ -L "$src" ] || { echo "건너뜀(링크 아님): $src"; continue; }
      dst="$(readlink "$src")"
      tmp="$src.returning-$$"
      copy_one "$dst" "$tmp" && rm "$src" && mv "$tmp" "$src" && echo "되돌림: $src (원본 $dst 는 남겨 둠)"
    done ;;
  *) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 4 ;;
esac
