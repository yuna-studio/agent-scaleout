---
description: Machines in the scaleout pool (name, role, OS, state, memory, load, jobs) and the queue (scaleout status)
allowed-tools: Bash(scaleout status:*), Bash(scaleout jobs:*)
---

Run `scaleout status` and `scaleout jobs -n 10` with the Bash tool. Tell the user in a few short lines, in their language:
which machines are free or busy, what is running or waiting (and why it waits), and anything that looks wrong
(a machine offline, a job failing again).
