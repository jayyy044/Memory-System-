# Agent Memory Benchmark — Design

**Date:** 2026-08-10
**Status:** design, not approved for implementation
**Scope:** Phase 1 only — a thin vertical slice that validates the harness

---

## Problem

No benchmark measures whether a memory system helps a coding agent do work across
sessions.

Existing memory benchmarks measure conversational recall. LoCoMo is multi-session
chat about a person's life, then questions about it. Plain `grep` over the transcripts
scores ~74% on it — Letta's result — because the task is shallow fact lookup, and that
is what `grep` is for. An independent audit found 6.4% of LoCoMo's answer key is wrong,
and its LLM judge accepts up to 63% of intentionally wrong answers.

Existing coding benchmarks measure single-session bug fixing. SWE-bench Verified was
retired by OpenAI in 2026 after their own re-audit found ≥59.4% of the subset models
most often fail had flawed tests rejecting functionally correct submissions.

Neither measures the thing that matters for agentic coding: an agent stopped
mid-task, and a fresh session has to pick it up.

## What exists (survey, 2026-08-10)

Six systems read — four by cloning and reading source, two by web research.

| System | Contradiction handling | Staleness vs. reality |
|---|---|---|
| mem0 | none — write path is ADD-only, hash dedup | none |
| Graphiti | LLM classifier, single-shot, no confidence | LLM-guessed dates; missed contradictions never expire |
| basic-memory | n/a | sync staleness only (DB vs. file) |
| TencentDB Agent Memory | write-time dedup only | age purge, disabled by default |
| GitHub Copilot Memory | — | **re-validates facts against current branch; 28-day validate-or-expire** |
| Google Memory Bank | consolidation with revision history | **configurable TTL + consolidation** |

Every one stores distilled facts *about a person*, retrieved by similarity — memory for
chat personalization. None store work state: what was tried and rejected, why a decision
went the way it did, what is half-finished, what the code looked like when a claim was
made.

Two systems do handle staleness against ground truth. Both are locked inside platforms
the user does not control.

## Phase 1 scope

One repo. One task family. Six arms. The goal is **to find out whether the harness
measures anything**, not to publish a number.

Two results would change everything downstream and are cheap to get early:

- `grep` beats the memory system → most architecture work is unnecessary
- the full-context ceiling beats retrieval → retrieval is solved by hardware at this scale

Deliberately out of scope for phase 1: the other two task families, additional repos,
any architecture change to the existing vault.

## Corpus

**Repo:** `jg-rp/liquid` — 98★, MIT, Python, ~15.8k source LOC / ~16.8k test LOC.
Layered architecture (lexer → parser → AST → renderer → filters), which produces
genuinely multi-file bugs rather than one-line edits.

Test suite verified green by direct run: `pytest` → 3518 passed, 72 skipped, 1.36s.

Two environment constraints found by running it:

- **`TZ=UTC` must be pinned.** Two date-filter tests fail otherwise. Environment-dependent,
  not flaky.
- **`git submodule update` required** for the `tests/golden-liquid` fixture repo. One-time
  network fetch at setup; test runs stay hermetic.

**Task selection: post-cutoff issues only.** Contamination is avoided by construction
rather than detected by measurement — the same approach SWE-bench-Live takes. A model
cannot have memorized a fix that postdates its training data.

This replaces an earlier proposal to run a contamination probe comparing pre- and
post-cutoff localization accuracy. That probe is confounded: it mixes contamination with
codebase drift (files that existed in 2024 may not exist now), and the split is defined by
one specific model's cutoff, so the result would not transfer.

**Ground truth extraction.** Issue → fix commit → changed files. Three mapping paths, in
order:

1. Merge commits from `fix-<issue>` branches, diffed against first parent
2. Commit bodies matching `(fix|close|resolve)[^0-9]*#<issue>`
3. Pickaxe on the changelog: `git log -S"issues/<n>)" -- CHANGES.md`, then diff that commit

Path 3 is needed because the `fix-<issue>` branch convention is recent. Seven of twelve
sampled issues mapped cleanly.

**Known constraint:** liquid closed 8 issues in 2026, down from 40/year in 2022. Roughly
5 are cleanly mappable. That is enough for the slice and **not** enough for a standing
benchmark. Repo selection for scale is a phase-2 problem.

**Synthetic probes** may supplement the natural corpus for precise edge-case coverage.
They are scored and reported **separately** and never folded into a headline number.

## Task family: resume interrupted work

Session A performs part of a task and stops. Session B starts cold and must finish it.

Scored on:

- did it complete the task correctly
- did it redo work session A already did
- did it retry an approach session A recorded as rejected

This is the family where the user's existing `/distill` output is closest to ground truth
— its session template already has `# Dead ends` and `# Left open` sections.

## Arms

Every task runs against six arms:

| Arm | What it is | Why |
|---|---|---|
| floor | no memory at all | lower reference |
| grep | ripgrep over raw notes | the baseline that embarrassed the field |
| ceiling | entire corpus in a 1M context window | upper reference; may prove retrieval unnecessary |
| vault | the user's markdown vault + freshness contract | system under test |
| mem0 | mem0 ingesting the same corpus | cross-system comparison |
| basic-memory | basic-memory ingesting the same corpus | cross-system comparison |

A score without a floor and ceiling is uninterpretable. The user's full distill corpus is
~157,000 words, which fits in a 1M-token window — so the ceiling arm is runnable, not
hypothetical.

**Scaffold config is pinned and published with every result** — turns, tools, retries,
context. The same model scored 60.4% under Aider's two-attempt cap versus 93.3% in a full
agentic loop. Numbers from different scaffolds are not comparable.

## Scoring

Three layers.

**Deterministic — the primary number.** Test pass/fail, file diffs, exit codes, and
transcript trace checks. Agentic sessions leave a trace, so "did it redo work" is whether
a command appears twice across two transcripts, not a judgment call. Trace checks match
exactly, never by substring: "did it re-run `npm test`" must not fire on `npm test:watch`.

**Validated judge — extends coverage.** An LLM judge with a written rubric for what
deterministic checks cannot reach. Its agreement rate against human labels is measured and
**published alongside every result that uses it.**

**Human spot-check — audits the automation.** A sample reviewed by hand. The same sample
produces the labels that validate the judge, so it pays for itself twice.

## Isolation

**Shallow clone, `--depth 1`, truncated at the base commit. No other refs or branches
present.**

This is the leak that goes straight through this design. The premise is that session A did
work and session B resumes it — so if session A's commits are in the repo history, session B
runs `git log` and never touches memory. Every arm converges and the benchmark measures git.

Precedent: SWE-bench Pro, built specifically to fix its predecessor's leakage, shipped
Docker images with full `.git` history reachable past the base commit. Agents ran
`git log --all` / `git show` and pulled the golden fix directly.

Other channels that must be closed for the floor arm to be a floor:

- `CLAUDE.md` / `AGENTS.md` in the repo checkout
- Claude Code auto-memory at `~/.claude/projects/<project>/memory/` — **confirmed populated
  on three of the user's projects** by directory listing (contents unexamined)
- session A's transcript left in the working directory
- session A's uncommitted changes surviving into session B's checkout
- the task prompt restating what session A learned
- a corpus note stating the fix verbatim rather than the context around it

Every one of these makes results *better*, which is why they go unnoticed.

## The audit gate

**No number is reported until the harness passes all ten checks.** Each corresponds to a
documented real-world failure.

1. **Golden-patch end-to-end.** The known-correct solution runs through the entire harness
   for every task and scores 100%. Anything less means the harness is broken, not the agent.
2. **Broken-baseline.** Unmodified code makes the target test actually fail. A test already
   passing scores every arm as success.
3. **Determinism.** Golden solution 5–10 runs, 100% pass. Unmodified baseline 5–10 runs,
   100% fail. Anything less and the task is cut as flaky.
4. **Regression guard.** Tests passing before the change still pass after (`PASS_TO_PASS`).
5. **Prompt leakage.** Diff the prompt the agent sees against the golden patch; grep for
   leaked filenames and function names.
6. **History leakage.** A cheat-probe arm whose only job is to try `git log --all`,
   `git show`, and network fetches, and confirm it gets nothing useful.
7. **Null arm equals floor arm.** A memory system returning nothing must score identically
   to no memory. Higher means information reaches the agent through an undeclared channel.
8. **Oracle arm near ceiling.** A memory system returning the literal answer must score
   high. If it does not, the harness cannot detect memory working even when memory is perfect.
9. **Environment pinning.** `TZ`, dependency lockfiles, frozen image digest, no live network
   calls during runs.
10. **Exclusion accounting.** Every task dropped from the set is logged with a reason.
    SWE-bench Verified filtered out 68.3% of candidates — legitimate only because it was
    published.

The auditor receives the diff and the original request only, in fresh context, never the
implementer's reasoning. Scope is the bounded regression question — *does this harness
measure what it claims* — not open-ended "what is wrong here," so it terminates.

It should also run the harness against a deliberately broken memory system and confirm the
score drops. A harness that scores everything well is the failure that looks like success.

## Known limitations

- Five tasks is a thin slice. It answers "does the harness work," not "which system is best."
- liquid's issue rate cannot sustain a standing benchmark.
- Contamination is avoided by task selection, not measured. A future model trained past
  June 2026 invalidates this corpus.
- The judge's error rate is measured on a sample, so it carries sampling error.
- Run-to-run variance is irreducible; results are medians with spread, never single runs.

## Open questions

- Does deterministic retrieval (`vault-search.sh`) land before or after the first
  benchmark run? Landing it first gives a more stable baseline; landing it after gives a
  before/after measurement.
- Public release: repo, license, and whether the corpus ships with the harness.
