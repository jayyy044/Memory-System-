# Goal

Build a memory system that tracks what happens inside a coding session and
carries it forward to the next one.

Specifically, it should:

**Understand the work.** Know what the session is working on, not just which
files it touched.

**Tell progress from motion.** As work happens, capture which things worked
and moved closer to the goal, and which failed.

**Classify the failures.** For anything that failed, distinguish:
- *wrong direction* — the approach itself is wrong, do not go back down it
- *wrong implementation* — the approach is right, the attempt was broken, a
  fix gets us further

This distinction is the whole point. A machine reading tool calls cannot
make it: both produce an identical failed command. Only the agent knows
which, and only at the moment it decides.

**Track progress in steps, reliably.** Work happens in stages. At any point
we should know what is finished, what has been abandoned and why, and what
still needs doing — within a single session AND across sessions.

**Organize it so it can be maintained and queried.** Storage format is
undecided: raw markdown files that sessions and agents grep, or retrieval
over embeddings. Pick it on evidence, not preference.

**Compress the project state** — where the last session stopped, what
direction the next one should take — into a hard ceiling of **30k tokens**.
Completed work drops out of the summary as it finishes, so what remains
stays concentrated on what is still live. The ceiling is a starting point
to measure against, not a target to fill.

---

## Constraints

- **Must not fabricate.** A confidently wrong memory is worse than no
  memory, because the next session acts on it.
- **Must survive a session dying.** Crash, context overflow, closed
  terminal — none of them run an exit hook.
- **Must fit the budget as history grows.** Ten sessions and two hundred
  sessions have to cost the same at read time: 30k tokens, whatever the
  history behind it.
- **Must not stall a session start.** The hook has a five-second timeout.

## How we will know it works

The session's own phrasing is the metric: *"waste less time searching and
just get the answer and start working."*

Count the orientation a session does before it produces useful work — tool
calls spent re-reading, re-running, re-deriving. A cold session pays it
every time. A warm one should not.

Measured against `floor` (no memory) using membench.

---

## Measured, so we do not re-derive it

- **`transcript_path` is in the SessionStart payload.** Its `.parent` is the
  transcript folder, its `.stem` is the live session id. No path
  reconstruction is needed.
- **`thinking` block content is stripped server-side.** Reasoning never
  reaches the client. An interrupted session emits near-zero text — two of
  six measured runs produced exactly zero characters.
- **`is_error` conflates at least five things.** Across 8 sessions, 9 of 16
  errors never executed at all: blocked by a pre-flight guard, refused by
  the user, or over a size limit.
- **Exit code non-zero is a return value, not a failure.** `fresh.sh`
  documents `1 = stale`; `grep` and `diff` do the same.
- **Mechanical dead-end detection does not fire.** "Failed twice, never
  succeeded" found **0** matches across theflowershop-v2's 42 sessions and
  6,125 tool calls. Agents vary the command between attempts, so repetition
  never accumulates on an identical string.

**Therefore:** the machine cannot generate the content. It can and should
own everything else — when capture happens, where it is stored, what schema
is enforced, what the budget is, and whether a memory has gone stale.
