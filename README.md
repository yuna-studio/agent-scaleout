# agent-scaleout

**Turn the spare computers on your network into a pool for coding agents.**
One machine (the *orchestrator*) keeps a queue of jobs for Claude Code, Codex, Gemini CLI or any command,
watches every *node*'s memory, disk and load, hands each job to the least-busy healthy node, collects the result,
notices when something hangs, retries elsewhere, and cleans nodes up before their disks fill.

```
            ┌──────────────── orchestrator ─────────────────┐
 you ──►    │ queue · scheduler · health · retries · cleanup │
 (CLI or    └───────┬──────────────┬──────────────┬──────────┘
  a Claude          │ ssh          │ ssh          │ ssh (or reverse tunnel)
  subagent)   ┌─────▼────┐   ┌─────▼────┐   ┌─────▼────┐
              │  node A  │   │  node B  │   │  node C  │   old laptops, mini PCs,
              │  macOS   │   │  Linux   │   │ Windows  │   a desktop under the desk…
              └──────────┘   └──────────┘   └──────────┘
```

- **No server, no cloud, no daemon on nodes beyond a tiny runner.** Plain SSH. The orchestrator is one Python 3.9+ file (stdlib only); nodes run small shell / PowerShell scripts.
- **Bring any agent and any account.** Each node uses whatever agent CLI is installed and signed in there — different Claude accounts, Codex, Gemini, or your own command.
- **Built for cheap, mismatched hardware.** Nodes below the free-disk / free-memory thresholds get no work and are cleaned automatically; one or two jobs per node.
- **Set up once, then hands-off.** Attaching a node is one command on the orchestrator, or one pasted line on the node. After that the orchestrator administers it: updates, cleanup, skills, runners, shell commands.

> Status: **v0.2, early.** A macOS orchestrator with macOS nodes is used daily. **Linux and Windows node support is written but has not been tested on real machines yet** — reports and fixes are very welcome.

## Contents
[Install](#install) · [Attach nodes](#attach-nodes) · [Run jobs](#run-jobs) · [Use from Claude Code](#use-from-claude-code) · [How it works](#how-it-works) · [Resource guard](#resource-guard--self-healing) · [Skills sync](#skills-sync) · [Orchestrator folders](#working-on-orchestrator-folders) · [GitHub runners](#github-actions-runners) · [Security](#security) · [Commands](#command-reference) · [Troubleshooting](#troubleshooting) · [Relocating folders](#bonus-relocate-heavy-folders-to-another-disk)

## Install

On the orchestrator (macOS or Linux; needs `python3`, `ssh`, `tar`):

```bash
curl -fsSL https://raw.githubusercontent.com/yuna-studio/agent-scaleout/main/install.sh | sh
scaleout install-daemon      # recommended: keep scheduling and healing in the background
```

The installer copies everything to `~/.local/share/agent-scaleout`, links `~/.local/bin/scaleout`, runs `scaleout init`
(creates an SSH key, records this machine's LAN address) and, if Claude Code is present, installs a `scaleout` subagent.
The orchestrator's own SSH server must be on for invites (macOS: *System Settings → General → Sharing → Remote Login*).

## Attach nodes

Every node needs an SSH server (the invite turns it on), a signed-in coding agent (e.g. Claude Code), and power settings that keep it awake.

**A. From the orchestrator only** — the node already accepts SSH (macOS / Linux):

```bash
scaleout add build-01 alice@192.168.1.23 --dir ~/scaleout-work --tags linux,docker
```
You type the node's password once (to install the key). The orchestrator uploads the node package, registers the runner service,
pins the host key (no yes/no prompt) and runs a test job.

**B. One line on the node** — a new machine, SSH maybe off:

```bash
scaleout invite build-02                 # macOS / Linux node
scaleout invite build-win --os windows   # Windows 10/11 node
```
It prints one line. Paste it into a terminal (or an Administrator PowerShell) on the node — in person or through any remote desktop.
The line turns the SSH server on (asking for that machine's password once), downloads the node package *from the orchestrator*,
installs it, registers the node and exchanges keys. Invite keys are single-use, expire after 24 h and can do nothing but enroll.

**Nodes outside the LAN.** If the orchestrator has a public SSH endpoint, nodes keep a reverse tunnel to it and work from anywhere:

```bash
scaleout init --tunnel-host ssh.example.com                                                        # plain SSH
scaleout init --tunnel-host ssh.example.com --tunnel-proxy "cloudflared access ssh --hostname %h"  # e.g. Cloudflare Tunnel
```
Tunnels are optional; on one LAN nothing extra is needed. (Reverse tunnels are not implemented for Windows nodes yet.)

## Run jobs

```bash
scaleout status                                   # health, route, free memory/disk, load, jobs and agents per node
echo "Run the test suite in ./api and summarise failures" | scaleout run -d ~/src/app
id=$(scaleout submit -f task.md --tags xcode)     # queue and return immediately
scaleout wait "$id"                               # result (exit 2 = still running, run it again)
scaleout jobs · logs ID · cancel ID · result ID
scaleout exec build-01 -- df -h                   # any shell command on a node
```

| option | meaning |
|---|---|
| `-w NODE` | pin a node (default `auto`: least-loaded healthy node) |
| `-a claude\|codex\|gemini` | pick an agent (default: the node's default) |
| `-d DIR` | working folder on the node (default: the node's `--dir`) |
| `--sync DIR` | work on an orchestrator folder: copied to the node before, copied back after |
| `--tags a,b` | node requirements, e.g. `xcode`, `gpu`, `windows`; `android-device` / `ios-device` = needs a phone plugged into the node |
| `--timeout MIN` | hard limit (default 180) |

Exit codes: `0` ok · `1` agent failed · `2` still running · `3` no reachable node · `4` usage · `5` not found.

## Use from Claude Code

`scaleout init` installs `~/.claude/agents/scaleout.md`. In any session:

> "Use the scaleout subagent to run the full E2E suite on a free machine and report failures. Repo: …, branch: …, push fixes to a new branch."

The subagent is a cheap relay (Haiku): it queues the task, waits, and returns the node agent's report verbatim.
Write tasks so they stand alone — the node's agent has not seen your conversation.

## How it works

- **Queue on the orchestrator** (`~/.local/state/scaleout`). `scaleout daemon` (or any `run`/`wait`) does a *tick*: refresh node metrics → collect finished work → retry lost work → dispatch queued jobs → maintenance.
- **Node control is one script** (`~/.scaleout/bin/ctl`, or `ctl.ps1` on Windows) with verbs such as `metrics`, `submit`, `state`, `result`, `cleanup`. The orchestrator never needs to know a node's OS.
- **Jobs run inside the node's login session**, started by its service manager whenever the node's queue is non-empty: a LaunchAgent with `QueueDirectories` (macOS), a `systemd --user` path unit (Linux, cron fallback), a scheduled task (Windows). On macOS this matters: an SSH session cannot read the login keychain, so agents look "not logged in" when started over plain SSH.
- **Heartbeats.** The node runner touches a heartbeat every 10 s and enforces the timeout. A stale heartbeat (default 5 min), a vanished job, or a node offline for 10 min puts the job back in the queue (2 retries by default); the orphan is cancelled when that node returns.
- **Scheduling score** = free memory % + free disk − 30 × jobs on the node − load per CPU, among nodes that are healthy, have the required tags and agent, and are under their `max_jobs`.

## Resource guard & self-healing

| check | default | what happens |
|---|---|---|
| free disk on a node | `< 5 GB` | no new jobs (`disk-full`) |
| free memory on a node | `< 15 %` | no new jobs (`mem-full`) |
| free disk on a node | `< 8 GB` | node cleanup (old job folders, package caches; below 5 GB also caches under the node's home), at most hourly |
| finished job folders | 3 days | removed — results are fetched and deleted right away anyway |
| runner service missing | — | re-registered (`ctl repair`) |
| free disk when a job starts | `< 2 GB` | job refused (exit 28) instead of filling the disk |
| free memory when a job starts | `< 12 %` | the node holds the job (never starts a second agent into swap); after 10 min the orchestrator moves it to another node |
| phones attached | — | `android-device` / `ios-device` jobs go only to nodes with a free phone of that kind, one job per phone |

Change with `scaleout config KEY VALUE` (e.g. `scaleout config min_free_disk_gb 10`) or per node (`scaleout config --node build-01 max_jobs 1`).
Machine-wide cleanup (Docker prune, unavailable simulators) runs only when you ask: `scaleout cleanup NODE --deep`.

## Real devices

Nodes count USB-attached phones (`adb devices`, `xcrun devicectl`) and report them in `scaleout status` (`A1 I1`).
Tag a job `android-device` or `ios-device` and it waits for a node with a free phone. Pairing a phone (USB debugging,
"Trust this computer", iOS Developer Mode) is a one-time step on the node. Real phones are also the cheapest way to make
small nodes useful: an Android emulator alone takes ~4.5 GB of RAM, a phone takes none.

## Sizing nodes

What one mobile job roughly needs (measured on Apple Silicon Macs running Flutter projects):

| part | memory |
|---|---|
| coding agent (Claude Code CLI) | 0.3–0.5 GB |
| Gradle / Xcode build | 2–3 GB |
| iOS Simulator | ~2 GB |
| Android emulator | ~4.5 GB |
| real phone | ~0 GB |

So an 8 GB node runs one job with a real phone (no emulator), a 16 GB node runs two or three, and the CPU becomes the
limit after that. 256 GB of disk is enough for Xcode, one simulator runtime, the Android SDK and caches (the node cleans
itself below 8 GB free). Start with `--max-jobs 1` on 8 GB nodes; the node's memory guard also holds extra jobs.

## Benchmark, load test, memory tuning

How much did that second-hand machine add? Every node gets a score; **100 = one M1 Mac mini with 8 GB running one mobile
job (agent + build + test) at a time.** `scaleout add` benchmarks new nodes automatically.

```bash
scaleout add m1-b alice@192.168.1.31 --price 200000   # what you paid (any currency)
#   m1-b added 100 points for 200,000 -> 5.0 points per 10k
#   pool score 300  (was 200, +100, 1.50x)
scaleout bench            # re-score all nodes: CPU (1 core / all cores), memory, disk -> job slots and score
scaleout bench --load     # load test: synthetic build jobs through the real scheduler, timed end to end
scaleout bench --history  # how the pool grew
scaleout stats            # real jobs per node: done, failed, run and wait times, memory per job
scaleout tune [--apply]   # max_jobs from the memory real jobs used (the daemon does this daily)
```

- **Score** = job slots × speed. Slots = min(memory slots, CPU slots): one mobile job needs ~3.5 GB on top of ~3 GB for the OS;
  CPU slots grow with all-core speed. Speed counts only the CPU part of a job (~40 %) — the rest is waiting for the model.
- **Load test** jobs hold ~2.5 GB, alternate waits with 4-core build bursts and write 512 MB, so slots, memory guard, dispatch
  latency and CPU contention all show up. Load score 100 = one M1 8 GB working through the same jobs one at a time.
  The synthetic jobs use no agent account.
- **Memory tuning.** The node runner records how much memory each job really added (peak). Once a node has 5 finished jobs,
  `tune` sets `max_jobs = (memory - idle use - 1 GB) / p90(memory per job)`, capped by the CPU slots. Nodes added with an
  explicit `--max-jobs` are never changed.

## Skills sync

`~/.claude/skills`, `~/.claude/agents` and `~/.claude/commands` on the orchestrator are copied to nodes automatically before a
dispatch whenever they changed (content hash), or on demand with `scaleout sync-skills`. Edit the list in
`~/.config/scaleout/config.json` → `skills.paths`, or turn it off with `"auto": false`.

## Working on orchestrator folders

`--sync DIR` streams `DIR` (minus `node_modules`, `build`, `.gradle`, `.dart_tool`) to a fresh folder on the node, runs the job
there, then streams the folder back into `DIR`. It works for every node OS and survives flaky Wi-Fi.
A live network mount is possible too (share the folder over SMB and mount it on the nodes), but builds over SMB are slow and
break when the link drops, so the pool does not depend on it.

## GitHub Actions runners

Put self-hosted runners on nodes (uses your `gh` login; needs admin on the org/repo):

```bash
scaleout runner add build-01 --org my-org --labels scaleout,linux,x64
scaleout runner ls --org my-org
scaleout runner remove build-01 --org my-org
```
Runners live in `~/.scaleout/gh-runners/` on the node and run as the node user (launchd / systemd --user).

## Security

- Orchestrator → node: a dedicated key `~/.ssh/scaleout_ed25519`. Host keys are pinned at enrollment (`HostKeyAlias scaleout-NAME`).
- Node → orchestrator (tunnels only): the node key is added with `restrict,port-forwarding,permitlisten="localhost:PORT",command=…` — it can open its tunnel and nothing else.
- Invite keys: `restrict,command="scaleout _enroll NAME"`, single use, expiring.
- Jobs run with the node user's rights. For Claude Code, `~/.scaleout/settings/claude.json` allows edits, git, package managers and read-only tools, and **denies** force pushes, pushes to `main`/`master`/`dev`/`release*`, `gh pr merge`, workflow runs, releases, `rm -rf`, `sudo` and common deploy commands. Edit it per node; upgrades keep your copy.
- Treat nodes as trusted machines on a trusted network: an agent with shell access can do whatever the node user can.

## Command reference

```
scaleout init [--tunnel-host H] [--tunnel-proxy CMD] [--lan IP] [--user U]
scaleout add NAME user@host | --ssh ALIAS   [--dir D] [--max-jobs N] [--agent A] [--tags a,b] [--no-tunnel] [--no-test]
scaleout invite NAME [--os unix|windows] [--hours 24] [--dir D] [--max-jobs N] [--agent A] [--tags a,b]
scaleout remove NAME · nodes · status [NAME…] · doctor NAME · update [NAME…]
scaleout submit|run [-f FILE] [-w NODE] [-a AGENT] [-d DIR] [--sync DIR] [--tags a,b] [--timeout MIN]
scaleout wait|result|logs|cancel JOB · jobs [-n N] · gc
scaleout exec NAME -- CMD · cleanup [NAME…] [--aggressive|--deep] · sync-skills [NAME…]
scaleout config [KEY [VALUE]] [--node NAME] · tick · daemon · install-daemon
scaleout runner add|remove|ls [NAME] --org ORG|--repo OWNER/REPO [--labels L] [--runner-name N]
```

## Troubleshooting

| symptom | fix |
|---|---|
| node `offline` | asleep or off. Keep nodes plugged in and awake (macOS: *Battery → Prevent automatic sleeping on power adapter*). |
| node `runner-down` | the orchestrator re-registers it within 10 min, or run `scaleout update NAME`. |
| agent says "not logged in" | sign in to the agent **on the node** (run `claude` there once). |
| job fails at once with rc 28 | node disk almost full → `scaleout cleanup NAME --aggressive`. |
| tunnel never comes up | read `~/.scaleout/tunnel.log` on the node; the LAN route still works when the node is home. |
| pasted commands break | chat apps can turn spaces into non-breaking spaces; invites are one base64 line for that reason. |
| same Wi-Fi, but nodes cannot reach each other (no ping) | the router isolates Wi-Fi clients ("AP / client isolation"). Turn it off, plug the machines into the router or a switch with Ethernet, or use a tunnel / Tailscale (`scaleout add NAME user@100.x.y.z`). |
| a macOS service fails with "Operation not permitted" | launchd services cannot read external USB disks. Keep `--dir`, toolchains and caches on the internal disk. |

## Bonus: relocate heavy folders to another disk

`tools/relocate.sh` moves folders (workspaces, build caches) to another disk and leaves a symlink, so every path keeps working.
It copies first, swaps in seconds, and adds Claude Code project-history aliases for the new real path.

```bash
tools/relocate.sh copy /Volumes/BigSSD ~/workspace ~/.gradle    # 1) copy while you keep working
tools/relocate.sh swap /Volumes/BigSSD ~/workspace ~/.gradle    # 2) quick re-sync, then swap to a symlink
tools/relocate.sh back ~/workspace                              # undo
```
Use `KEEP_OLD=1` when the folder is the current directory of a running program (the old copy is kept aside instead of deleted).

**macOS caveat:** background services (launchd: CI runners, scheduled scripts, this tool's node runner) get
"Operation not permitted" on external USB disks. Only move folders that are used interactively (old projects, archives,
backups). `relocate.sh` skips folders it sees in a LaunchAgent and known toolchains/caches (`FORCE=1` overrides).

## Development

```bash
python3 tests/test_orchestrator.py    # end-to-end with two fake nodes: dispatch, results, failures, stalls, offline retry, disk guard, --sync
```
Contributions welcome — especially Linux and Windows node testing. MIT licensed.

한국어 안내: [README.ko.md](README.ko.md)
