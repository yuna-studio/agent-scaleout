#!/bin/bash
# agent-scaleout PostToolUse / PostToolUseFailure hook for Claude Code's Bash tool: closes the record of a heavy command
# that offload-hook.sh let run on this machine (scaleout top shows it in the queue). Every other command costs one grep.
input="$(cat)"
id="$(printf '%s' "$input" | grep -o '"tool_use_id" *: *"[A-Za-z0-9_-]*"' | head -1 | sed 's/.*"\([A-Za-z0-9_-]*\)"$/\1/')"
[ -n "$id" ] && [ -f "${SCALEOUT_STATE:-$HOME/.local/state/scaleout}/direct/$id.json" ] || exit 0
py="$(command -v python3 || echo /usr/bin/python3)"
printf '%s' "$input" | "$py" "$(dirname "$0")/direct-run.py" end >/dev/null 2>&1
exit 0
