# agent-scaleout: token saving (`scaleout tokens on`)

Every tool call re-reads this session's whole context from the cache, so tokens ≈ calls × context size. A session that
has grown large pays for that size on every step after it. Here auto-compaction runs at about {window_k}k tokens
(Claude Code's own default is near the model's full window, up to ~967k).

## Keep what matters in files, not only in the conversation
A compaction summary loses detail. Two files survive it word for word and are put back into the session right after it:
- `{notes}` — **your notes**. Keep them current: the goal, decisions and why, what is done, what is next, file paths and
  commands that matter, and every correction or preference the user gave in this session (in their words). Update them
  when a step finishes, before long operations, and at once when told the context is getting large.
- `{ledger}` — written automatically just before each compaction from the transcript: the user's latest messages, files
  changed, commands run, the task list and job ids.
Trust these two over the summary when they differ. Keep the notes short (under ~150 lines): replace, do not append forever.

## Work so the context stays small
- **One task per session.** When a task (a PR, a fix) is done, update the notes and continue the next task in a new
  session (or `/clear`) instead of carrying everything in one long session. The notes carry over.
- **No fork from a large context.** A forked subagent inherits this whole context and re-reads it on every step. Start a
  fresh subagent (general-purpose, Explore, scaleout) with a standalone brief. Above {fork_k}k a fork is refused.
- **Keep long output out of the context.** Build, test and E2E logs, large files and listings: write them to a file and
  read only what you need (`tail`, `grep`, line ranges). Do not cat whole logs or read big files end to end.
- **Batch calls.** Independent tool calls go in one message. One command that does a few related steps beats many small ones.
- **Verbose work elsewhere.** Builds, full test runs and anything with long output go to the pool (`scaleout submit`/`run`)
  or a fresh subagent; this session receives the summary.
