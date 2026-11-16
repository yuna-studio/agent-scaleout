#!/bin/bash
# agent-scaleout node provisioning (macOS first; Linux: Flutter/Node/Firebase parts only).
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

# Homebrew (installing it needs an admin password, so a person does it once)
if command -v brew >/dev/null 2>&1; then say OK homebrew "$(brew --version | head -1)"
else say TODO homebrew 'install once (needs your password): /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'; fi
have_brew() { command -v brew >/dev/null 2>&1; }

# fvm + Flutter versions
if [ -n "${FLUTTER:-}" ]; then
  if ! command -v fvm >/dev/null 2>&1; then
    if have_brew && run brew tap leoafarias/fvm && run brew install fvm; then say DONE fvm "$(fvm --version 2>/dev/null)"; else say FAIL fvm "brew install fvm"; fi
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
