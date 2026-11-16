# agent-scaleout: offload heavy work to nodes

This machine is an agent-scaleout orchestrator. Heavy work goes through the scaleout queue: the orchestrator runs it
itself while it has room (when `scaleout local on`), hands it to a node when it is busy, and keeps it queued when nobody
has room. `scaleout status` / `scaleout top` show the machines.

## Keep here
- Conversation, planning, reading and editing code, git.
- Quick checks: `dart analyze` / `tsc` on a few files, a single test file, small scripts.

## Send to a node
- Full builds: `flutter build`, `xcodebuild` (archive, build-for-testing), `./gradlew assemble…`, web production builds.
- Full test suites, coverage runs, E2E (Maestro, Playwright, iOS Simulator, Android emulator).
- Long research, large refactors, or anything that will run for many minutes.

## How
- Use the `scaleout` subagent, or run it yourself:
  `echo "<task>" | scaleout run --repo <git repo here> [--subdir path] [--tags …] [--apply]`
  - `--repo`: the node works on a copy of this repo at the current working tree, uncommitted changes included. Git sends only what changed.
  - The result comes back as branch `scaleout/<job>`. `--apply` also applies it to this working tree; conflicts are reported, never forced.
- Tags:
  - `ios-device` / `android-device`: a phone is plugged into the node.
  - `emulator`: a node with room for an Android emulator.
  - `xcode`, `mac`, `linux`: what the node is.
- Memory decides how many jobs a node runs at once (no fixed limit on simulators or emulators). Pass `--mem <GB>` when a job needs more than usual, e.g. several simulators.
- Write the task so it stands alone: what to do, which folder, how to check it. The node's agent has not seen this conversation.
- Screenshots, recordings, logs and reports the node saves come back automatically. The result ends with an `[artifacts]` list of local paths. Show or send those files.
- Node agents never stop for permission prompts. Commands outside their allow list are refused. Deploys, force pushes, pushes to protected branches and `sudo` are always refused.

## Simulators and emulators
- When you tested on this machine, shut down the simulators and emulators you started once you are done:
  `xcrun simctl shutdown <udid>` and `adb -s emulator-<port> emu kill`.
- Nodes stop what their jobs started when they go idle.
- The daemon shuts down any simulator or emulator, here and on nodes, after 20 minutes with no test running.

## When a command is blocked here
If a hook says this machine is short on memory or overloaded, do not retry locally. Send the command to a node as above.
Prefix `SCALEOUT_LOCAL=1` only when the work must use this machine, for example a device attached here.
