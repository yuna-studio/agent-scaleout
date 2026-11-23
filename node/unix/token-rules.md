Token saving is on for this machine: every tool call re-reads the whole context, so keep it small.
- Long output (build, test, E2E logs, big files, long listings) goes to a file; read only what you need with tail, grep or line ranges.
- Put independent tool calls in one message, and prefer one command that does a few related steps over many small ones.
- Do not start forked subagents; a fresh subagent with a standalone brief costs far less.
- Finish with a short result: what changed, what passed or failed, where the files are.
