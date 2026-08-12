"""Session A: the interrupted attempt every arm's memory is derived from.

If session A did not actually get interrupted, it is a SOLUTION, not an
attempt - every arm downstream then resumes finished work and the benchmark
reports a clean number that measures nothing. That failure is invisible in the
output, so it is checked here and raised, never returned (D66).
"""

import json
from pathlib import Path

from membench.corpus.extract import BenchTask
from membench.driver import run_agent
from membench.models import ToolCall, Transcript

SESSION_A_PROMPT = """You are working on this issue:

{title}

{body}

Investigate and begin the fix. You will be interrupted before finishing.
Keep working until you are stopped. Do not commit."""


class SessionAError(RuntimeError):
    """Same shape and reasoning as `RunTestsError` (membench/runner.py:63) and
    `GateError` (membench/gate/checks.py:37): a session A that is malformed or
    was never interrupted is NOT a result, and returning something a caller
    cannot distinguish from a real one is how a meaningless benchmark number
    ships looking clean."""


# Measured live against claude 2.1.227 (see
# tests/test_arms.py::test_the_cli_really_reports_an_interruption, which
# asserts the real stream against THESE sets rather than against copies of
# them). The result event carries BOTH of these on a turn-limit hit:
#     subtype='error_max_turns'  terminal_reason='max_turns'
# and on a run that FINISHED inside the budget, measured at max_turns=12:
#     subtype='success'  terminal_reason='completed'
# `Transcript.stop_reason` does NOT carry it: it is the MODEL's last-message
# stop reason and read 'tool_use' on that same run - a value a completed
# session could also produce. Checking stop_reason alone would have been
# unfalsifiable, which is why this parses the result event out of
# `Transcript.raw` instead (`tools_from_init` in membench/driver.py:189 is the
# same raw-rescan precedent). Positive match on the interrupt signal, so
# anything unrecognised - including a CLI that renames these - fails closed as
# "not interrupted" rather than passing through as a valid session A.
_INTERRUPTED_SUBTYPES = {"error_max_turns"}
_INTERRUPTED_TERMINAL_REASONS = {"max_turns"}

# CALIBRATION KNOB, not a derived truth - tune it against the first real corpus
# run and expect to move it. There is no principled value here: it is the floor
# below which session A did not investigate enough for its notes to carry
# anything FloorArm does not already have from the issue title alone.
# Measured, not assumed: a run that emits a single tool_use block and no text
# writes a 0-byte session-a-transcript.md and a pure-template current.md, and
# the four arms then produce textually distinct preambles that are
# INFORMATIONALLY identical - four arms, one experiment, all green.
# tool_calls is the INVESTIGATION signal: a chatty model that made no tool
# calls learned nothing. It is necessary and NOT sufficient - see the notes
# knob below. Too high fails loudly and the operator raises max_turns; absent ships
# a benchmark that measures nothing.
MIN_SESSION_A_TOOL_CALLS = 3

# CALIBRATION KNOB, same status as the one above. This is the RECORD signal,
# and it is measured on the NOTES THIS MODULE ACTUALLY GENERATES - specifically
# on the part of them that is not derivable from the issue text: the tool-call
# log plus whatever prose session A produced. Everything else in the notes is
# template (task id, issue title, turn count), which FloorArm already has.
#
# It used to be measured on `transcript.text`, at 60 chars, and that was
# unreachable-adjacent for the wrong reason. Six live interrupted runs produced
# 0, 0, 78, 88, 120 and 756 chars of text - two of them EXACTLY zero, same
# configuration as the 120-char one - because an interrupted session's `.text`
# is whatever opening sentence it happened to emit before the turn limit, and
# the result event's summary `result` field is absent on a max_turns hit
# (membench/driver.py:277). No threshold on `.text` can separate "did nothing"
# from "did a lot, silently".
#
# Value 80, from arithmetic over the measured shapes:
#   - three name-only tool calls with no path and no command render as ~40
#     chars of log ("1. TodoWrite\n2. ...") and must NOT pass;
#   - three calls carrying real file paths render at ~100 chars and must pass -
#     "the previous attempt read these three files" is exactly the memory the
#     arms differ on;
#   - live run at max_turns=5: 5 tool calls -> ~370 chars of log, plus 756 of
#     text = ~1126 of evidence, 14x this floor.
# ponytail: KNOWN CEILING - it defends against an EMPTY or degenerate record,
# not a shallow one. Nothing here can tell a useful investigation from a
# thorough-looking useless one; that is Task 7's scoring job, not a guard's.
MIN_SESSION_A_NOTES_CHARS = 80

# A single Bash heredoc can be thousands of characters and would swamp the
# notes it is supposed to be one line of. Truncated, not dropped: the command's
# opening is what identifies it to a resuming agent.
_MAX_DETAIL_CHARS = 300

# Session A's REASONING is not recoverable, measured not assumed. The obvious
# source would be the `thinking` blocks `_parse_stream` discards
# (membench/driver.py:262) - but they are empty on the wire. Instrumented live
# run (max_turns=5, 6 turns, 5 tool calls), counting every string field of
# every assistant content block in `Transcript.raw`:
#     block counts     = {'thinking': 4, 'text': 2, 'tool_use': 5}
#     thinking.thinking = 0 chars      thinking.signature = 7584 chars
# i.e. each thinking block is `{"type":"thinking","thinking":"","signature":
# "Ep..."}` - signature only, content stripped server-side. Re-scanning `raw`
# for them (the `tools_from_init` precedent, driver.py:189) recovers nothing,
# so the notes are built from tool_calls + text and this is recorded here so
# the next reader does not pay for the same run.
_NO_PROSE = (
    "(nothing - session A was interrupted before it wrote any prose. Its reasoning is "
    "not recoverable: the CLI emits thinking blocks with an empty body.)"
)


def _result_event(raw: str) -> dict | None:
    """The LAST result event, not the first: a stream can carry more than one
    and the session's outcome is the final one. Returning the first would let
    an early error_max_turns mask a later successful completion, i.e. accept a
    session A that actually finished. Pinned by
    tests/test_arms.py::test_generate_session_a_uses_the_last_result_event."""
    last = None
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if evt.get("type") == "result":
            last = evt
    return last


def _detail(call: ToolCall) -> str:
    """The one thing worth knowing about a call: what it ran, or what it
    touched. `command or file_path` - ToolCall carries nothing else
    (membench/models.py:4-8)."""
    what = (call.command or call.file_path or "").replace("\n", " ").strip()
    return what[:_MAX_DETAIL_CHARS] + " ...[truncated]" if len(what) > _MAX_DETAIL_CHARS else what


def _build_notes(task: BenchTask, transcript: Transcript, max_turns: int) -> tuple[str, str, str]:
    """`(session-a-transcript.md, current.md, evidence)`.

    `evidence` is the third return rather than a re-derivation because the
    minimum-work guard must measure the notes THAT GET WRITTEN, and it must run
    before they are written. It is the material an arm could not have got from
    the issue text: the tool-call log plus session A's prose.

    Ordered and labelled for a resuming agent, not concatenated: status first
    (this attempt is UNFINISHED), then the files, then the chronology, then the
    prose - which is the least reliable of the three (see MIN_SESSION_A_NOTES_
    CHARS) and so is last."""
    log = "\n".join(
        f"{i}. {c.name}" + (f" - {d}" if (d := _detail(c)) else "")
        for i, c in enumerate(transcript.tool_calls, 1)
    )
    touched: dict[str, list[str]] = {}
    for c in transcript.tool_calls:
        if c.file_path and c.name not in touched.setdefault(c.file_path, []):
            touched[c.file_path].append(c.name)
    files = "\n".join(f"- {p} ({', '.join(n)})" for p, n in touched.items())
    prose = transcript.text.strip()
    evidence = f"{log}\n{prose}".strip()

    status = (
        f"Session A was INTERRUPTED after {transcript.num_turns} turns "
        f"(max_turns={max_turns}). The fix is NOT finished - this is an attempt to resume, "
        f"not a solution to verify."
    )
    transcript_md = (
        f"# Session A on {task.task_id} (interrupted)\n\n"
        f"{status}\n\n"
        f"## Issue\n\n{task.issue_title}\n\n{task.issue_body}\n\n"
        f"## Files session A touched\n\n{files or '(none - it ran no file-path tool)'}\n\n"
        f"## What session A did, in order ({len(transcript.tool_calls)} tool calls)\n\n"
        f"{log or '(no tool calls)'}\n\n"
        f"## What session A said\n\n{prose or _NO_PROSE}\n"
    )
    last = f"{transcript.tool_calls[-1].name} {_detail(transcript.tool_calls[-1])}".strip() \
        if transcript.tool_calls else "(none)"
    # Short on purpose: VaultArm inlines this whole file into session B's
    # prompt (membench/arms/vault.py), so it is the standing state, not the
    # record - the record is session-a-transcript.md, which it points at.
    current_md = (
        f"# Session A on {task.task_id} - INTERRUPTED\n\n"
        f"Issue: {task.issue_title}\n"
        f"Status: stopped after {transcript.num_turns} turns (max_turns={max_turns}). "
        f"The fix is NOT complete; resume it.\n\n"
        f"Files session A touched:\n{files or '- (none)'}\n\n"
        f"Last thing it did: {last}\n"
        f"Everything it did, in order: session-a-transcript.md\n"
    )
    return transcript_md, current_md, evidence


def generate_session_a(
    task: BenchTask,
    workdir: Path,
    notes_dir: Path,
    *,
    max_turns: int,
    model: str,
    timeout_s: int = 900,
) -> Transcript:
    """Runs the interrupted first session in `workdir`, writes its notes into
    `notes_dir`, and returns the `Transcript`.

    D67: the Transcript, not `notes_dir` - Task 7's `score_trace` consumes
    `session_a.tool_calls`, which writing `.text` to a file throws away, and
    `notes_dir` is an input the caller already holds.

    D65: an empty `issue_title`/`issue_body` is a malformed corpus entry
    (tests/conftest.py:32-33 hardcodes both to ""). Rejected up front so a real
    agent run is never spent interpolating nothing into the prompt.
    """
    for field in ("issue_title", "issue_body"):
        if not (getattr(task, field) or "").strip():
            raise SessionAError(
                f"task {task.task_id!r} has an empty {field} - a malformed corpus entry, "
                f"not a session; refusing to spend an agent run on it"
            )

    transcript = run_agent(
        SESSION_A_PROMPT.format(title=task.issue_title, body=task.issue_body),
        workdir,
        max_turns=max_turns,
        model=model,
        timeout_s=timeout_s,
    )

    result = _result_event(transcript.raw)
    if result is None:
        raise SessionAError(
            f"task {task.task_id!r}: the agent stream contained no result event "
            f"(exit_code={transcript.exit_code}) - the run's outcome is unknown, which must "
            f"never be treated as an interrupted session"
        )
    subtype, terminal = result.get("subtype"), result.get("terminal_reason")
    # BOTH signals required (`or`, not `and`): the CLI emits them together on a
    # turn-limit hit, so a half-signal - subtype='success' with
    # terminal_reason='max_turns', or error_max_turns with
    # terminal_reason='end_turn' - is a stream this code does not understand,
    # and "not understood" must fail closed rather than be read as interrupted.
    # Pinned by the two half-signal cases in tests/test_arms.py.
    if subtype not in _INTERRUPTED_SUBTYPES or terminal not in _INTERRUPTED_TERMINAL_REASONS:
        raise SessionAError(
            f"task {task.task_id!r}: session A was not interrupted "
            f"(subtype={subtype!r} terminal_reason={terminal!r} stop_reason={transcript.stop_reason!r} "
            f"num_turns={transcript.num_turns} max_turns={max_turns}) - it either finished or failed, "
            f"and a finished session A is a solution, not an attempt. Lower max_turns and re-run."
        )

    # Notes built BEFORE the guard and before any write: the guard's whole job
    # is to measure what would be written, and a rejected session A must leave
    # nothing behind for an arm to pick up.
    transcript_md, current_md, evidence = _build_notes(task, transcript, max_turns)

    # BOTH signals, and neither is sufficient alone. tool_calls is the
    # INVESTIGATION signal - a chatty model that called no tools learned
    # nothing. `evidence` is the RECORD signal, measured on the generated notes
    # rather than on `transcript.text`, because the text alone is 0 chars on
    # two of six measured live runs while the same runs made 5-8 tool calls.
    failed = []
    if len(transcript.tool_calls) < MIN_SESSION_A_TOOL_CALLS:
        failed.append(
            f"{len(transcript.tool_calls)} tool calls < "
            f"MIN_SESSION_A_TOOL_CALLS={MIN_SESSION_A_TOOL_CALLS} (nothing was investigated)"
        )
    if len(evidence) < MIN_SESSION_A_NOTES_CHARS:
        failed.append(
            f"{len(evidence)} chars of notes < "
            f"MIN_SESSION_A_NOTES_CHARS={MIN_SESSION_A_NOTES_CHARS} (the notes would carry "
            f"no tool-call log and no prose - only the issue text FloorArm already has)"
        )
    if failed:
        raise SessionAError(
            f"task {task.task_id!r}: session A did too little work to be memory - "
            + "; ".join(failed)
            + f" (num_turns={transcript.num_turns}, max_turns={max_turns}). Its notes would "
            f"carry nothing FloorArm does not already have, making all four arms one "
            f"experiment. Raise max_turns and re-run."
        )

    notes_dir.mkdir(parents=True, exist_ok=True)
    (notes_dir / "session-a-transcript.md").write_text(transcript_md)
    (notes_dir / "current.md").write_text(current_md)
    return transcript
