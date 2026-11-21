#!/bin/bash
# agent-scaleout node provisioning (macOS first; Linux: Flutter/Node/Firebase parts, from their own releases when there is no Homebrew).
# Installs what the orchestrator uses, at the same versions, without sudo:
#   FLUTTER="3.35.6 3.41.9"  POD=1.16.2  FIREBASE=15.5.1  NODE_MAJOR=25  ANDROID="android-35 android-36"  XCODE=26.2
# Prints one "STATUS item detail" line per item: OK (already there), DONE (installed now), TODO (needs a person), FAIL.
# DRY_RUN=1 only reports.
set -u
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$HOME/fvm/default/bin:$PATH"
DRY="${DRY_RUN:-0}"
say() { local st="$1"; [ "$DRY" = 1 ] && [ "$st" = DONE ] && st=PLAN; printf '%s %s %s\n' "$st" "$2" "$3"; }
run() { [ "$DRY" = 1 ] && return 0; "$@" >/tmp/scaleout-provision.log 2>&1; }
OS="$(uname -s)"

# Homebrew (installing it needs an admin password, so a person does it once). Linux nodes do without it.
if command -v brew >/dev/null 2>&1; then say OK homebrew "$(brew --version | head -1)"
elif [ "$OS" = Darwin ]; then say TODO homebrew 'install once (needs your password): /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'; fi
have_brew() { command -v brew >/dev/null 2>&1; }
linux_arch() { case "$(uname -m)" in x86_64|amd64) echo x64 ;; aarch64|arm64) echo arm64 ;; *) return 1 ;; esac; }
mkdir -p "$HOME/.local/bin"

fvm_release() {   # Linux without Homebrew: fvm's own release build in ~/.local/fvm, linked into ~/.local/bin
  local a tag tmp
  a="$(linux_arch)" || return 1
  tag="$(curl -fsSL https://api.github.com/repos/leoafarias/fvm/releases/latest | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -1)"
  [ -n "$tag" ] || return 1
  tmp="$(mktemp -d)"
  curl -fsSL "https://github.com/leoafarias/fvm/releases/download/$tag/fvm-$tag-linux-$a.tar.gz" | tar -xz -C "$tmp" || { rm -rf "$tmp"; return 1; }
  rm -rf "$HOME/.local/fvm"; mv "$tmp/fvm" "$HOME/.local/fvm"; rm -rf "$tmp"
  ln -sf "$HOME/.local/fvm/fvm" "$HOME/.local/bin/fvm"
}

java_release() {  # Linux: Eclipse Temurin JDK <major> in ~/.local/jdk-<major> (no sudo)
  local a tmp
  case "$(uname -m)" in x86_64|amd64) a=x64 ;; aarch64|arm64) a=aarch64 ;; *) return 1 ;; esac
  tmp="$(mktemp -d)"
  curl -fsSL "https://api.adoptium.net/v3/binary/latest/$1/ga/linux/$a/jdk/hotspot/normal/eclipse" | tar -xz -C "$tmp" || { rm -rf "$tmp"; return 1; }
  rm -rf "$HOME/.local/jdk-$1"; mv "$tmp"/jdk* "$HOME/.local/jdk-$1"; rm -rf "$tmp"
}

node_release() {  # Linux without Homebrew: the official Node.js build of that major in ~/.local/node; npm installs globals into ~/.local
  local a f
  a="$(linux_arch)" || return 1
  f="$(curl -fsSL "https://nodejs.org/dist/latest-v$1.x/SHASUMS256.txt" | awk '{print $2}' | grep -E "^node-v[0-9.]+-linux-$a\.tar\.xz$" | head -1)"
  [ -n "$f" ] || return 1
  rm -rf "$HOME/.local/node.new"; mkdir -p "$HOME/.local/node.new"
  curl -fsSL "https://nodejs.org/dist/latest-v$1.x/$f" | tar -xJ -C "$HOME/.local/node.new" --strip-components 1 || return 1
  rm -rf "$HOME/.local/node"; mv "$HOME/.local/node.new" "$HOME/.local/node"
  for b in node npm npx corepack; do ln -sf "$HOME/.local/node/bin/$b" "$HOME/.local/bin/$b"; done
  "$HOME/.local/bin/npm" config set prefix "$HOME/.local" >/dev/null
}

# fvm + Flutter versions
if [ -n "${FLUTTER:-}" ]; then
  if ! command -v fvm >/dev/null 2>&1; then
    if have_brew && run brew tap leoafarias/fvm && run brew install fvm; then say DONE fvm "$(fvm --version 2>/dev/null)"
    elif [ "$OS" = Linux ] && ! have_brew && run fvm_release; then say DONE fvm "$(fvm --version 2>/dev/null) (release build)"
    else say FAIL fvm "brew install fvm"; fi
  else say OK fvm "$(fvm --version 2>/dev/null)"; fi
  for v in $FLUTTER; do
    if [ -x "$HOME/fvm/versions/$v/bin/flutter" ]; then say OK "flutter-$v" "$HOME/fvm/versions/$v"
    elif command -v fvm >/dev/null 2>&1 && run fvm install "$v" --setup; then say DONE "flutter-$v" "$HOME/fvm/versions/$v"
    else say FAIL "flutter-$v" "fvm install $v (see /tmp/scaleout-provision.log)"; fi
  done
fi

# Node.js + Firebase CLI
if [ -n "${NODE_MAJOR:-}" ]; then
  cur="$(node --version 2>/dev/null | sed 's/^v\([0-9]*\).*/\1/')"
  if [ "$cur" = "$NODE_MAJOR" ]; then say OK node "$(node --version)"
  elif have_brew && { run brew install node || run brew upgrade node; }; then say DONE node "$(node --version 2>/dev/null) (wanted $NODE_MAJOR)"
  elif [ "$OS" = Linux ] && ! have_brew && run node_release "$NODE_MAJOR"; then say DONE node "$(node --version 2>/dev/null) (release build)"
  else say FAIL node "wanted v$NODE_MAJOR, have ${cur:-none}"; fi
fi
if [ -n "${FIREBASE:-}" ]; then
  cur="$(firebase --version 2>/dev/null)"
  if [ "$cur" = "$FIREBASE" ]; then say OK firebase "$cur"
  elif command -v npm >/dev/null 2>&1 && run npm i -g "firebase-tools@$FIREBASE"; then say DONE firebase "$FIREBASE"
  else say FAIL firebase "npm i -g firebase-tools@$FIREBASE"; fi
fi

if [ "$OS" = Darwin ]; then
  # Xcode cannot be installed without an Apple ID: report only
  if [ -n "${XCODE:-}" ]; then
    cur="$(xcodebuild -version 2>/dev/null | sed -n 's/^Xcode //p')"
    if [ "$cur" = "$XCODE" ]; then say OK xcode "$cur"
    elif [ -n "$cur" ]; then say OK xcode "$cur (orchestrator has $XCODE — usually fine; update from the App Store to match)"
    else say TODO xcode "install Xcode $XCODE from the App Store (or: xcodes install $XCODE, signs in with an Apple ID), then open it once"; fi
  fi
  # CocoaPods
  if [ -n "${POD:-}" ]; then
    cur="$(pod --version 2>/dev/null)"
    if [ -n "$cur" ]; then say OK cocoapods "$cur"
    elif have_brew && run brew install cocoapods; then say DONE cocoapods "$(pod --version 2>/dev/null) (wanted $POD)"
    else say FAIL cocoapods "brew install cocoapods"; fi
  fi
  # Java for Android builds: Android Studio's JBR, else Homebrew OpenJDK 21
  JBR="/Applications/Android Studio.app/Contents/jbr/Contents/Home"
  if [ -x "$JBR/bin/java" ]; then JH="$JBR"; say OK java "Android Studio JBR"
  elif [ -x /opt/homebrew/opt/openjdk@21/bin/java ]; then JH=/opt/homebrew/opt/openjdk@21; say OK java "openjdk@21"
  elif have_brew && run brew install openjdk@21; then JH=/opt/homebrew/opt/openjdk@21; say DONE java "openjdk@21"
  else JH=""; say FAIL java "no Android Studio and brew install openjdk@21 failed"; fi
fi

# Java for Android builds on Linux: the orchestrator's major version (Gradle refuses a Java newer than it knows, and Android
# Studio's own runtime can be one), set as Flutter's JDK for every version (flutter config is per user)
if [ "$OS" = Linux ] && [ -n "${JAVA_MAJOR:-}" ]; then
  JH=""
  for d in "$HOME/.local/jdk-$JAVA_MAJOR" /usr/lib/jvm/java-"$JAVA_MAJOR"-openjdk-*; do [ -x "$d/bin/java" ] && { JH="$d"; break; }; done
  if [ -n "$JH" ]; then say OK java "$JAVA_MAJOR ($JH)"
  elif run java_release "$JAVA_MAJOR"; then JH="$HOME/.local/jdk-$JAVA_MAJOR"; say DONE java "Temurin $JAVA_MAJOR"
  else say FAIL java "JDK $JAVA_MAJOR (see /tmp/scaleout-provision.log)"; fi
  if [ -n "$JH" ]; then
    for v in ${FLUTTER:-}; do
      f="$HOME/fvm/versions/$v/bin/flutter"
      if [ -x "$f" ]; then
        if grep -q "\"jdk-dir\": *\"$JH\"" "$HOME/.config/flutter/settings" 2>/dev/null; then say OK flutter-jdk "$JH"
        elif run "$f" config --jdk-dir "$JH"; then say DONE flutter-jdk "$JH"
        else say FAIL flutter-jdk "flutter config --jdk-dir $JH"; fi
        break
      fi
    done
  fi
fi

# Android SDK platforms (only into an SDK whose licenses were accepted on this machine)
if [ -n "${ANDROID:-}" ]; then
  SDK="${ANDROID_HOME:-$HOME/Library/Android/sdk}"; [ "$OS" = Darwin ] || SDK="${ANDROID_HOME:-$HOME/Android/Sdk}"
  SM="$SDK/cmdline-tools/latest/bin/sdkmanager"
  if [ ! -d "$SDK" ]; then say TODO android-sdk "install Android Studio (or the command-line tools) and accept the SDK licenses once"
  elif [ ! -f "$SDK/licenses/android-sdk-license" ]; then say TODO android-licenses "accept once: $SM --licenses"
  else
    for p in $ANDROID; do
      if [ -d "$SDK/platforms/$p" ]; then say OK "$p" "$SDK/platforms/$p"
      elif [ -x "$SM" ] && JAVA_HOME="${JH:-}" run "$SM" --install "platforms;$p"; then say DONE "$p" "installed"
      else say FAIL "$p" "sdkmanager --install platforms;$p"; fi
    done
  fi
fi
