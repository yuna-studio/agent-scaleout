# agent-scaleout: offload heavy work to nodes

This machine is an agent-scaleout orchestrator. Heavy work goes through the scaleout queue: the orchestrator runs it
itself while it has room (when `scaleout local on`), hands it to a node when it is busy, and keeps it queued when nobody
has room. `scaleout status` / `scaleout top` show the machines (name, role, OS); `scaleout usage` shows the Claude tokens of every machine, day by day.

## Keep here
- Conversation, planning, reading and editing code, git.
- Quick checks: `dart analyze` / `tsc` on a few files, a single test file, small scripts.

## Send to the pool
- Full builds: `flutter build`, `xcodebuild` (archive, build-for-testing), `./gradlew assemble…`, web production builds.
- Full test suites, coverage runs, E2E (Maestro, Playwright, iOS Simulator, Android emulator).
- Long research, large refactors, or anything that will run for many minutes.
- Send these even when this machine has room: the pool has several machines, and work done here runs one at a time.

## Fire, keep working, collect (do not wait in line)
Do not run heavy items one after another, waiting on each. Hand them off and carry on:
1. Split the work into items that do not need each other's results (e.g. Android build · iOS build · unit tests ·
   E2E per platform · each product). Each item's prompt stands alone.
2. Queue them all at once, without waiting: `id=$(echo "<item>" | scaleout submit --repo <repo> [--tags …])` per item
   (submit prints the job id and returns at once). Or start several `scaleout` subagents in one message.
3. While they run, do the work that does not depend on them: read and edit code, write the next items, update docs.
4. Collect in the background: for each job start `scaleout wait <id> -m 0` with the Bash tool's run_in_background.
   Each wait shows in Claude Code's list of background tasks and you are notified when it ends, so no result is missed
   even after your turn ends. Act on each result as it comes; a failed item is fixed and sent again on its own.
Wait for one result first only when the next step really depends on it (e.g. a build that the E2E needs).

An item does not have to stay in this session. Hand off whatever can be written down as a standalone task: heavy or long
items to the pool (any machine, a separate agent per job), light parallel items (research, reading, drafting) to background
subagents. Keep in this session only what depends on this conversation: decisions being made now, the user's latest
corrections, small steps. Handing off costs a little (the receiving agent loads its own context, about 1% of a job's tokens;
`scaleout usage` shows it as "wasted by scaleout"); running independent items one after another here costs far more time.

## How
- Use the `scaleout` subagent, or run it yourself:
  `echo "<task>" | scaleout run --repo <git repo here> [--subdir path] [--tags …] [--apply]`
  - `--repo`: the node works on a copy of this repo at the current working tree, uncommitted changes included. Git sends only what changed.
  - The result comes back as branch `scaleout/<job>`. `--apply` also applies it to this working tree; conflicts are reported, never forced.
- Tags:
  - `ios-device` / `android-device`: a phone is plugged into the node.
  - `emulator`: a node with room for an Android emulator.
  - `mac` / `linux` / `windows`: the node's OS. `scaleout status` and `scaleout top` show each machine's OS (`macOS arm64`, `Linux x64`, `Windows x64`).
  - `xcode`: a Mac with Xcode.
- Pick the OS by the work, and say it with a tag so the job waits for the right machine:
  - iOS and macOS work (`xcodebuild`, iOS Simulator, `pod install`, anything that writes `Podfile.lock` or the Xcode project) → `--tags mac` (or `xcode`).
  - Android, web, backend, tests and research run on any OS; leave the OS tag out.
  - Flutter app changes: check them on the Android emulator (`--tags emulator`, any OS) unless the change is iOS-only
    (iOS layout bug, iOS plugin, signing). `mac` is for work only a Mac can do; it keeps the job off every other machine.
  - A job that needs bash scripts or `--repo` does not go to a Windows node.
  - Results that differ by OS (golden images, native binaries) → tag the OS they must match.
- Memory decides how many jobs a node runs at once (no fixed limit on simulators or emulators). Pass `--mem <GB>` when a job needs more than usual, e.g. several simulators.
- A job that only runs something and reports (E2E, tests, builds, screenshots) says so in its first lines ("do not change code,
  report only") or is sent with `--check`: where `check_model` is set it runs on that cheaper model. Work that needs changes or
  judgement (fixing, reviews, play tests) is left as it is; `--model default` forces the node's own model.
  Its result ends with `[check]`: look at the evidence of each failure, then record `scaleout check <job> real=<n> false=<n>`.
- Write the task so it stands alone: what to do, which folder, how to check it. The node's agent has not seen this conversation.
- A job waits in the queue while no machine has room (memory, CPU load, a free phone). `scaleout run`/`wait` print why, and
  `scaleout jobs` shows it as `waits: …`. Waiting is normal: do not cancel and resubmit, it starts as soon as a machine has room.
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
