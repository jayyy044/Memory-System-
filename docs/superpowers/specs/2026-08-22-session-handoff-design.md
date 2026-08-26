# Session Handoff — Design

**Date:** 2026-08-22
**Status:** design, not approved for implementation
**Scope:** Sub-project A of the agent-memory work. Milestone M1 (mechanical extraction) is the first build; M2 (`remember` tool) follows it. Milestone letters are avoided deliberately — sub-projects already use A/B/C/D.

---

## Problem

A session does part of a task and stops — cleanly, or by crashing, blowing its context, or being closed. The next session starts cold and re-derives what the first one already worked out: the same files read, the same commands run, the same failed approaches attempted a second time.

No existing system addresses this. Six were surveyed at source level (`research/agent-memory-systems-survey.md`): all store distilled facts *about a person*, retrieved by similarity. None store work state — what was tried and rejected, why a decision went a certain way, what is half-finished.

No benchmark measures it either (`research/agentic-benchmark-integrity.md`). The closest 2026 entrant, LongMemEval-V2, states plainly that it "measures question-answering over agent trajectories, not task completion."

## Non-goals

- **Project state tracking** (done / remaining / backlogged across a large build) — sub-project B.
- **Claim verification** (a fresh agent re-runs a claimed test and gets the same result) — sub-project C.
- **Procedural memory / skill routing** — sub-project D. The auto-loaded header reserves a slot for it; nothing fills that slot in this design.
- Any change to membench's arms or scoring semantics beyond feeding them a producer they already expect.

---

## Decisions

Any memory system is defined by five answers. These are this one's.

| Axis | Decision |
|---|---|
| **Write** | Hybrid. Mechanical extraction from the transcript (free, unfabricatable, crash-proof) plus agent-authored rationale at decision points. |
| **Read** | Push what the reader cannot know to ask for (dead ends, task state) under a hard token cap. Everything else stays on disk behind search. |
| **Expire** | Code-anchored (`covers:` + `verified_at:`, validated by git) **plus** explicit supersede by a later session. |
| **Scope** | Per repo + branch, from the transcript's own `cwd` and `gitBranch` fields. |
| **Trigger** | SessionStart, for previous sessions only. |

### Why rationale must be an explicit action

Measured, `research/what-a-claude-cli-transcript-actually-contains.md`:

- `thinking` block content is **stripped server-side** — every block is `{"type":"thinking","thinking":"","signature":"..."}`. No parser change recovers it.
- An interrupted session's visible text is nearly empty. Across six runs, **two produced exactly zero characters**; harder tasks produced *less* text, not more.

Therefore rationale cannot be inferred from what the agent said. It must be recorded as an observable action (a tool call), during the session. This is what forces M2 to exist at all, and it is why the hybrid write path is the only one that satisfies the requirement.

---

## Canonical input: the session JSONL

**Decision: the extractor reads `$CLAUDE_CONFIG_DIR/projects/<slug>/<uuid>.jsonl`, not membench's stream-json.**

Verified 2026-08-22 on `claude 2.1.239`, by running `claude -p` with `HOME` and `CLAUDE_CONFIG_DIR` set to fresh temp directories:

- The transcript **is** written under a relocated `CLAUDE_CONFIG_DIR` (`config/projects/<slug>/<uuid>.jsonl`, 15,023 bytes for one trivial turn).
- Its schema is unchanged by the relocation: `cwd`, `gitBranch`, `sessionId`, `uuid`, `parentUuid`, `isSidechain`, `version`, `timestamp` present on every event.
- Tool results carry `is_error` on every `tool_result` block, and `toolUseResult` carries `stdout` / `stderr` / `interrupted`.

The stream-json path (`membench/driver.py:247`) extracts only `name`, `command`, `file_path`. It has no outcome, no call→result link, no identity, and no repo/branch. It is a strict subset.

One extractor therefore serves both membench runs and real day-to-day sessions.

**Version coupling.** The session JSONL is an internal format; stream-json is the documented one. The observed CLI version has already drifted (research pinned at 2.1.227, host now 2.1.239). The extractor records the `version` field it saw and **fails loud on an unrecognized event shape** rather than silently extracting less. Precedent: `fresh.sh`'s `uncheckable` status was benign-sounding enough to be ignored, and 37 of 52 notes in one vault were never checked once as a result.

**membench change required.** `_CONTAINER_CONFIG` is currently container-local and never bind-mounted, and the container runs `--rm`, so the transcript is created and destroyed inside it. Fix: bind-mount that path to a fresh empty host temp dir per run. The seal holds — the directory starts empty, so no host `CLAUDE.md`, hooks, plugins, skills, or credentials reach the container, which was the original reason for the decision.

---

## What a dead end is

**Two classes. One is asserted; the other is flagged.**

**Hard dead end** — an operation that failed and was never subsequently made to succeed in the same session. Mechanically certain from `is_error` plus the command/file identity.

**Suspected dead end** — a target that absorbed disproportionate effort and was then abandoned while the session continued elsewhere. Mechanically visible; semantically ambiguous. "Tuned the learning rate for six hours and gave up" and "tuned it, got it right, moved on" are identical in a transcript.

The second class exists because the first misses the most expensive case. An agent editing one parameter and re-running training forty times produces forty successful commands and zero failures. Reporting zero dead ends there would be worse than useless.

A suspected dead end is reported **with its outcome marked unknown**. Even hedged it is valuable: *"session A spent 26 tool calls on `train.py` learning-rate config, then moved to `data_loader.py`"* tells B not to start where A already spent six hours. Only M2's `remember` tool can promote it to a verdict.

**Rejected:** parsing command output for a metric that stops improving. It would catch the case precisely, needs per-domain output parsing, breaks on any format it was not written for, and guesses.

---

## Data model

One file per session, keyed by `sessionId` so re-extraction overwrites rather than duplicating. No shared mutable file anywhere in the design.

```yaml
---
session: 93f7f959-6cf8-498f-9b3e-fd5d5dea4ac0
repo: /Users/x/Projects/personal/claude-setup
branch: feat/membench-phase1
started: 2026-08-22T10:14:03Z
ended:   2026-08-22T11:02:55Z
cli_version: 2.1.239
verified_at: c75c01f
covers: membench/driver.py membench/scoring/trace.py
---

## Dead ends

- kind: hard
  target: "pytest -k parser"
  evidence: "failed 2x (is_error), never subsequently succeeded"
  covers: tests/test_parser.py
  verified_at: c75c01f

- kind: suspected
  target: "train.py — learning-rate config"
  evidence: "12 edits + 14 runs, last touched turn 41, session continued in data_loader.py"
  outcome: unknown
  covers: train.py
  verified_at: c75c01f

## In progress
- cycle_tag nested case — 8 calls, still failing at session end

## Decisions
(empty until M2)
```

**Every record must carry `covers:` and `verified_at:`.** A record that cannot be validated later is refused at write time rather than stored and reported `uncheckable` forever.

---

## Read path

The auto-loaded header is **derived at read time and never stored**. Consequences:

- No `current.md` equivalent to contend on, so concurrent sessions cannot clobber each other's state. The failure mode is designed out rather than locked around.
- The header always reflects current staleness, because the git check runs when it is built.

**Budget: a hard cap, enforced, not a convention.** For reference, the existing vault's `current.md` is ~650 tokens and all 21 notes together are ~38k. The cap applies to the header only; the store on disk grows without bound and stays searchable.

**Eviction — filter, rank, declare:**

1. Drop stale records first (their `covers:` paths moved since `verified_at`). The least trustworthy thing goes before anything true gets cut.
2. Rank survivors: current branch → effort spent (tool calls burned) → recency.
3. Declare what was cut, always.

```
DON'T RETRY (3 of 17 shown, 14 in ./memory — 6 stale, hidden):
  - lr tuning in train.py — 26 calls, abandoned turn 41 [outcome unknown]
  - pytest -k parser — failed 2x, never resolved
  - regex approach in cycle.py — failed, superseded by visitor
```

Effort-ranked rather than recency-ranked because six hours on the wrong parameter must outrank a two-call typo from ten minutes ago. Effort is the proxy for how much B stands to waste.

The declaration line is load-bearing. A header that quietly shows 3 of 17 teaches the reader that 3 is all there is.

---

## Staleness

A record declares the files it depends on and the commit it was true at. Invalidation is the union of two mechanisms:

- **Code-anchored:** `git diff --name-only <verified_at>..HEAD -- <covers>` is non-empty → stale.
- **Explicit supersede:** a later session marks a record superseded, with a reason.

Both are needed. Code-anchored alone misses records wrong for non-code reasons (a bad assumption, a misread doc). Supersede alone misses silent drift, which is exactly how Graphiti fails — a missed contradiction never expires.

Inherited from `fresh.sh`, with its known bugs fixed rather than reproduced:

- **A `covers:` path that no longer exists must not read as fresh.** `git diff` returns nothing for a deleted path, which is the maximally-wrong case reported as the clean one. Existence is checked before silence is trusted.
- **Branch-awareness.** `fresh.sh` measures against `HEAD` and therefore lies on the wrong branch (`backlog/fresh-sh-is-branch-blind`). Records carry `branch:` and are validated against it.
- **Frontmatter parsing must not read past the fence.** The current parser eats body bullets as paths.

---

## Milestones

**M1.1 — extend the captured tool record.** This touches DEBT-1. `D73` reads: *"Do NOT modify driver.py. DEBT-1 now blocks two tasks and should be fixed ONCE, deliberately, where both consumers exist (Task 9)."* It designates **Task 9** as the fix site, not a general condition. Fixing it here is a deliberate deviation from that decision and must be recorded as one — or Task 9 absorbs this change instead. Either is fine; doing it twice is not.

```python
@dataclass
class ToolCall:
    name: str
    command: str | None = None
    file_path: str | None = None
    pattern: str | None = None      # DEBT-1: Grep/Glob were invisible
    ok: bool | None = None          # from tool_result.is_error
    result_excerpt: str = ""        # for the error signature
```

**M1.2 — the extractor.** Session JSONL → per-session memory file. Hard and suspected dead ends, files touched, in-progress state.

**M1.3 — SessionStart read path.** Scan for un-extracted transcripts (skipping the live `session_id`), extract, derive the capped header, inject.

**M1.4 — membench integration.** Bind-mount the config dir, harvest the transcript, feed `score_trace()` the catalogue it has always expected as a caller-supplied argument.

**M2 — the `remember` tool.** Agent-authored rationale at decision points, validated at write time: a record without `covers:` is refused, not stored.

M1 is independently useful and independently measurable. Running membench with M1-only against M1+M2 is the experiment that says what rationale is actually worth.

---

## Testing

Each unit leaves one runnable check that fails if the logic breaks:

- A transcript fixture with a known failed-then-abandoned command yields exactly one hard dead end; the same command later succeeding yields zero.
- A fixture with 12 edits to one file then a switch yields one suspected dead end marked `outcome: unknown`.
- A record whose `covers:` path is deleted reports **stale**, not fresh.
- A header built from more records than the cap allows emits the declaration line with correct counts.
- An unrecognized CLI event shape raises rather than returning a partial extraction.
- Two simultaneous extractions of different sessions produce two files and no interleaving.

---

## Open questions

- **`isSidechain` semantics.** Subagent work is flagged separately in the transcript. If a subagent burned 40 calls down a dead end, the tokens were spent and B should not repeat it — so it likely counts. But this setup delegates heavily, and the choice affects every number. Not settled.
- **Container chown.** `seal-egress.sh` creates and chowns `CLAUDE_CONFIG_DIR` before dropping to uid 1000. Its behaviour against a bind mount is **unverified** — no container run has been made.
- **Effect size.** Nothing yet estimates how large the vault-vs-floor difference is, and it determines whether membench is affordable to run. Measured cost is ~$0.07–0.15 per run at `max_turns=5`; a 2,400-run budget is roughly $240. The estimate itself is still missing.
- **Suspected-dead-end thresholds.** "Disproportionate effort" needs concrete numbers (edit count, run count, abandonment gap). These must be calibrated against real transcripts, not guessed — `MIN_SESSION_A_NOTES_CHARS` in membench is already flagged as calibrated on a single sample (DEBT-4).
