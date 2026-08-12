"""Arms are pure file operations - default suite, no docker - EXCEPT
`test_grep_notes_survive_the_real_scoring_path` (docker, same as the rest of
tests/test_runner.py) and the one `live` test that settles D66 empirically.

The whole experiment rests on arms differing ONLY in what memory is present in
session B's workspace. A defect that makes two arms behave the same still
produces a clean-looking number, so the tests that matter here are the ones
that would catch an arm silently degrading into FloorArm (D69) and a session A
that was never actually interrupted (D66).
"""

import json
import re
import time
from pathlib import Path

import pytest

from membench.arms.base import NOTES_MOUNT
from membench.arms.ceiling import CeilingArm
from membench.arms.floor import FloorArm
from membench.arms.grep import GrepArm
from membench.arms.vault import VaultArm
from membench.corpus.extract import BenchTask
from membench.models import ToolCall, Transcript
from membench.session_a import (
    MIN_SESSION_A_NOTES_CHARS,
    MIN_SESSION_A_TOOL_CALLS,
    SESSION_A_PROMPT,
    SessionAError,
    _detail,
    _result_event,
    generate_session_a,
)

# Imported for the D69 docker test - reuses test_runner's local, fast gold repo
# instead of standing up a second one (`synthetic_repo` is referenced by name as
# a pytest fixture, hence the noqa-shaped unused import).
from test_runner import _synthetic_workdir, synthetic_repo  # noqa: F401


@pytest.fixture()
def notes(tmp_path: Path) -> Path:
    d = tmp_path / "notes"
    d.mkdir()
    (d / "a.md").write_text("session A tried X, it failed")
    return d


@pytest.fixture()
def wd(tmp_path: Path) -> Path:
    d = tmp_path / "ws"
    d.mkdir()
    return d


def _task(**over) -> BenchTask:
    kw = dict(
        task_id="liquid-209", repo="jg-rp/liquid", issue_number=209,
        issue_title="cycle tag repeats", issue_body="The cycle tag repeats items.",
        base_sha="0" * 40, fix_sha="1" * 40, changed_files=[],
    )
    kw.update(over)
    return BenchTask(**kw)


# --- arms -------------------------------------------------------------------

def test_floor_installs_nothing(wd: Path, notes: Path):
    preamble = FloorArm().install(_task(), wd, notes)
    assert preamble == ""
    assert list(wd.iterdir()) == []


def _preamble_command(preamble: str) -> str:
    """The single backticked shell command an on-disk-notes arm tells the agent
    to run. Extracted rather than hardcoded so the docker check below tests the
    string the ARM actually emits - a hardcoded copy would keep passing after
    someone edits the preamble, which is exactly how `rg` (not in the image)
    survived."""
    cmds = re.findall(r"`([^`]+)`", preamble)
    assert len(cmds) == 1, f"expected exactly one backticked command, got {cmds}"
    return cmds[0]


def test_grep_exposes_notes_dir_but_not_content(wd: Path, notes: Path):
    preamble = GrepArm().install(_task(), wd, notes)
    cmd = _preamble_command(preamble)
    # Explicit NOTES_MOUNT path in the command, not a bare recursive search:
    # NOTES_MOUNT is a dotfile dir and a bare search can skip it entirely.
    assert NOTES_MOUNT in cmd, cmd
    assert (wd / NOTES_MOUNT / "a.md").read_text() == "session A tried X, it failed"
    assert "tried X" not in preamble


@pytest.mark.parametrize("arm", [GrepArm(), VaultArm()], ids=lambda a: a.name)
def test_preamble_command_exists_and_works_in_the_agent_image(arm, wd: Path, notes: Path):
    """The instruction is worthless if the binary it names is not in the image.
    It named `rg` for a while; ripgrep has never been installed
    (docker/Dockerfile:7-9), so this arm was telling the agent to run a missing
    command and would have silently degraded into FloorArm-with-a-directory.

    BOTH on-disk arms, not just grep: VaultArm pointed at ./.membench-notes/
    with no command at all, which is the same defect one step further along -
    a dotfile directory the agent may never think to search.

    Mechanically checked, not comment-checked: runs the arm's own command line
    inside membench-agent:latest against a real dotfile notes dir and requires
    it to find the needle. Docker, like the rest of tests/test_runner.py
    (~1.4s), deliberately NOT marked live - it costs no tokens."""
    from membench.driver import _docker_run, _ensure_image

    needle = "needle-marker-4a7f"
    (notes / "a.md").write_text(f"session A tried {needle}")
    cmd = _preamble_command(arm.install(_task(), wd, notes)).replace("<pattern>", needle)
    assert '"' not in cmd and "'" not in cmd and "$" not in cmd, cmd  # safe to inline in sh -c

    script = (
        f"command -v {cmd.split()[0]} >/dev/null || exit 11\n"
        f"mkdir -p /tmp/w/{NOTES_MOUNT} && printf '%s\\n' 'session A tried {needle}' "
        f"> /tmp/w/{NOTES_MOUNT}/a.md\n"
        f"cd /tmp/w && {cmd}\n"
    )
    proc = _docker_run(
        ["--entrypoint", "sh", _ensure_image(), "-c", script], timeout_s=120
    )
    assert proc.returncode != 11, f"{cmd.split()[0]!r} is not installed in the agent image"
    assert proc.returncode == 0, f"{cmd!r} failed in the image: {proc.returncode} {proc.stderr}"
    assert needle in proc.stdout, f"{cmd!r} did not find the notes: {proc.stdout!r}"


def test_ceiling_inlines_all_notes_and_writes_nothing(wd: Path, notes: Path):
    preamble = CeilingArm().install(_task(), wd, notes)
    assert "tried X" in preamble
    # The ceiling arm's distinguishing property: content in the PROMPT, no
    # retrieval surface in the workspace. If it also planted the notes dir it
    # would be a strictly-better grep arm, not a ceiling.
    assert list(wd.iterdir()) == []


def test_vault_uses_current_md_as_header_when_present(wd: Path, notes: Path):
    (notes / "current.md").write_text("STATE: halfway through the cycle tag fix")
    preamble = VaultArm().install(_task(), wd, notes)
    # Front-loading current.md is the ONLY thing distinguishing this arm from
    # GrepArm now that both name a search command; it must survive.
    assert "STATE: halfway through the cycle tag fix" in preamble
    assert NOTES_MOUNT in _preamble_command(preamble)
    assert (wd / NOTES_MOUNT / "a.md").exists()
    # a.md is reachable but not inlined - that's the vault arm's whole shape.
    assert "tried X" not in preamble


def test_vault_without_current_md_still_points_at_the_notes(wd: Path, notes: Path):
    preamble = VaultArm().install(_task(), wd, notes)
    # A directory path alone is not a retrieval instruction: NOTES_MOUNT is a
    # dotfile dir the agent may never think to search (same defect as naming a
    # binary the image does not have). It must name a runnable command.
    assert NOTES_MOUNT in _preamble_command(preamble)
    assert (wd / NOTES_MOUNT / "a.md").exists()


def test_arms_differ_from_each_other(wd: Path, notes: Path, tmp_path: Path):
    """The one property the experiment cannot survive losing."""
    (notes / "current.md").write_text("STATE: halfway")
    seen = {}
    for arm in (FloorArm(), GrepArm(), CeilingArm(), VaultArm()):
        w = tmp_path / f"ws-{arm.name}"
        w.mkdir()
        seen[arm.name] = (arm.install(_task(), w, notes), sorted(p.name for p in w.rglob("*")))
    assert len(set(map(str, seen.values()))) == 4, seen


@pytest.mark.parametrize("arm", [GrepArm(), VaultArm()], ids=lambda a: a.name)
def test_copied_notes_never_carry_a_symlink_into_the_workspace(arm, wd: Path, notes: Path):
    """D69: shutil.copytree defaults to symlinks=False, i.e. it DEREFERENCES -
    a link in notes_dir lands as a plain file with real content. That is the
    point: the notes exist to be READ by session B, and a copied-through link
    is content the arm only maybe delivers - dangling if its target sat outside
    notes_dir, or pointing at a host path that does not exist inside the
    container, either way degrading the arm toward FloorArm.

    NOT because it would void the run: measured, `_contained_path` is reached
    only for gold-restore paths and test-surface entries
    (membench/runner.py:300-333) and `.membench-notes` is neither, so a symlink
    in there raises no RunTestsError at all. Pinned by a test because
    symlinks=False is a DEFAULT someone could 'improve', and both arms make the
    identical call - a comment in grep.py does not fail when vault.py is the
    one that changed."""
    (notes / "link.md").symlink_to(notes / "a.md")
    arm.install(_task(), wd, notes)
    dest = wd / NOTES_MOUNT / "link.md"
    assert not dest.is_symlink()
    assert dest.read_text() == "session A tried X, it failed"


# --- session A --------------------------------------------------------------

def _raw(**over) -> str:
    """A result event with the field names verified live against claude
    2.1.227 (see the live test at the bottom of this file)."""
    evt = {
        "type": "result", "subtype": "error_max_turns", "terminal_reason": "max_turns",
        "stop_reason": "tool_use", "num_turns": 2, "total_cost_usd": 0.0158,
        "permission_denials": [], "errors": ["Reached maximum number of turns (1)"],
    }
    evt.update(over)
    return json.dumps({"type": "system", "subtype": "init"}) + "\n" + json.dumps(evt) + "\n"


# Derived from the threshold, not a literal: every OTHER test here is about
# interruption, not about how much work happened, so their fake session must
# clear BOTH minimum-work floors or they pass for the wrong reason - and a
# hardcoded string would silently stop clearing it when the knob is retuned.
_ENOUGH_TEXT = "I started on it. ".ljust(MIN_SESSION_A_NOTES_CHARS + 1, ".")


def _fake_run_agent(
    monkeypatch, raw: str, spy: list | None = None,
    *, tool_calls: int | None = None, text: str | None = None,
    calls: list[ToolCall] | None = None,
):
    n = MIN_SESSION_A_TOOL_CALLS if tool_calls is None else tool_calls
    body = _ENOUGH_TEXT if text is None else text
    made = calls if calls is not None else [
        ToolCall(name="Read", file_path=f"f{i}.py") for i in range(n)
    ]
    def fake(prompt, workdir, *, max_turns, model, timeout_s=900):
        if spy is not None:
            spy.append(prompt)
        return Transcript(text=body, raw=raw, num_turns=2, cost_usd=0.0158, tool_calls=made)
    monkeypatch.setattr("membench.session_a.run_agent", fake)


def test_generate_session_a_rejects_an_empty_issue_before_spending_a_run(monkeypatch, tmp_path: Path):
    """D65: sample_task hardcodes issue_title/issue_body to "" - a malformed
    corpus entry, not a session. Must raise BEFORE run_agent costs money."""
    def boom(*a, **kw):
        raise AssertionError("run_agent must not be called for a malformed task")
    monkeypatch.setattr("membench.session_a.run_agent", boom)
    with pytest.raises(SessionAError, match="issue_body"):
        generate_session_a(
            _task(issue_body="   "), tmp_path / "ws", tmp_path / "notes",
            max_turns=2, model="claude-sonnet-5",
        )
    with pytest.raises(SessionAError, match="issue_title"):
        generate_session_a(
            _task(issue_title=""), tmp_path / "ws", tmp_path / "notes",
            max_turns=2, model="claude-sonnet-5",
        )
    assert not (tmp_path / "notes").exists()


def test_generate_session_a_returns_the_transcript_and_writes_notes(monkeypatch, tmp_path: Path):
    """D67: Task 7's score_trace consumes session_a.tool_calls, so the
    Transcript is the return value; notes_dir is an input the caller holds."""
    spy: list = []
    _fake_run_agent(monkeypatch, _raw(), spy)
    notes_dir = tmp_path / "notes"
    t = generate_session_a(
        _task(), tmp_path / "ws", notes_dir, max_turns=2, model="claude-sonnet-5"
    )
    assert isinstance(t, Transcript)
    assert t.text == _ENOUGH_TEXT
    assert "cycle tag repeats" in spy[0]
    # `in`, not `==`: the notes are no longer `transcript.text` verbatim - they
    # are a structured record whose prose section is that text (G4). The
    # structure itself is asserted by
    # test_notes_are_built_from_what_session_a_actually_did above; what this
    # line still pins is that session A's own words are not dropped.
    assert _ENOUGH_TEXT in (notes_dir / "session-a-transcript.md").read_text()
    assert "liquid-209" in (notes_dir / "current.md").read_text()


_REAL_WORK = [
    ToolCall(name="Read", file_path="/workspace/liquid/builtin/tags/cycle.py"),
    ToolCall(name="Bash", command="grep -rn cycle_hash /workspace/liquid"),
    # A SECOND file, and it is not the last call: the files-touched summary has
    # to be a list, not "the file the last tool call named". Without this,
    # dropping the summary from current.md entirely still passed - the
    # last-action line happened to mention the only path in the fixture.
    ToolCall(name="Read", file_path="/workspace/liquid/loaders/base.py"),
    ToolCall(name="Edit", file_path="/workspace/liquid/builtin/tags/cycle.py"),
]


def test_notes_are_built_from_what_session_a_actually_did(monkeypatch, tmp_path: Path):
    """G4. The notes used to be `transcript.text` verbatim, and an interrupted
    session's `.text` is one opening sentence or nothing at all - measured at
    0, 78, 88, 120 and 756 chars across six live runs, with two runs at exactly
    zero. Notes built from that carry nothing FloorArm does not already have
    from the issue title, so all four arms measure the same thing.

    `transcript.tool_calls` is the signal that actually survives an interrupt:
    which files session A read and edited, which commands it ran. This asserts
    the notes carry it, IN ORDER, with text absent entirely - the case that
    previously produced a 0-byte file."""
    _fake_run_agent(monkeypatch, _raw(), calls=_REAL_WORK, text="")
    notes_dir = tmp_path / "notes"
    generate_session_a(_task(), tmp_path / "ws", notes_dir, max_turns=2, model="claude-sonnet-5")

    notes = (notes_dir / "session-a-transcript.md").read_text()
    assert "/workspace/liquid/builtin/tags/cycle.py" in notes
    assert "grep -rn cycle_hash /workspace/liquid" in notes
    # Chronological, not a bag: "read it, then edited it" is the resumable fact.
    # Scoped to the log section - the files-touched summary above it lists the
    # same tool names in a different (per-file) order on purpose.
    log = notes.split("in order", 1)[1].split("## What session A said", 1)[0]
    assert log.index("Read") < log.index("Bash") < log.index("Edit"), log
    assert "INTERRUPTED" in notes and "2 turns" in notes

    current = (notes_dir / "current.md").read_text()
    assert "liquid-209" in current and "cycle tag repeats" in current
    # current.md is what VaultArm front-loads into the prompt; a resuming agent
    # that reads only this one file must still learn which files were touched
    # and where the rest is.
    touched = current.split("Files session A touched:", 1)[1].split("Last thing", 1)[0]
    assert "/workspace/liquid/builtin/tags/cycle.py" in touched, current
    assert "/workspace/liquid/loaders/base.py" in touched, current
    assert "session-a-transcript.md" in current


def test_notes_say_so_when_session_a_wrote_no_prose(monkeypatch, tmp_path: Path):
    """Two of six live runs produced exactly 0 chars of `.text`. The notes must
    say that explicitly rather than leaving an empty section a resuming agent
    reads as "nothing was found"."""
    _fake_run_agent(monkeypatch, _raw(), calls=_REAL_WORK, text="")
    notes_dir = tmp_path / "notes"
    generate_session_a(_task(), tmp_path / "ws", notes_dir, max_turns=2, model="claude-sonnet-5")
    body = (notes_dir / "session-a-transcript.md").read_text()
    said = body.split("## What session A said", 1)[1]
    assert said.strip().startswith("("), said


def test_generate_session_a_refuses_a_run_that_was_not_interrupted(monkeypatch, tmp_path: Path):
    """D66, the highest-value check in this task. A session A that FINISHED is
    a solution, not an interrupted attempt: every arm downstream would resume
    completed work and the benchmark would report a clean number measuring
    nothing."""
    # terminal_reason='completed', not a guess: MEASURED live at max_turns=12,
    # where the agent finished the fix in 9 turns and the result event read
    # subtype='success' terminal_reason='completed' stop_reason='end_turn'.
    # Every other value in _raw() came from a max_turns hit; this is the only
    # place the completed-run vocabulary is recorded, and it was 'end_turn'
    # here - invented, not observed - until the live run at LIVE_MAX_TURNS=12
    # contradicted it.
    _fake_run_agent(monkeypatch, _raw(subtype="success", terminal_reason="completed",
                                     stop_reason="end_turn", errors=[]))
    notes_dir = tmp_path / "notes"
    with pytest.raises(SessionAError, match="not interrupted"):
        generate_session_a(_task(), tmp_path / "ws", notes_dir, max_turns=2, model="claude-sonnet-5")
    assert not (notes_dir / "current.md").exists(), "a completed run must leave no session-A material behind"


@pytest.mark.parametrize(
    "over",
    [
        {"subtype": "success"},              # half-signal: success + max_turns
        {"terminal_reason": "end_turn"},     # half-signal: error_max_turns + end_turn
    ],
    ids=["success-but-max_turns", "error_max_turns-but-end_turn"],
)
def test_generate_session_a_requires_both_interrupt_signals(monkeypatch, tmp_path: Path, over):
    """`or`, not `and` (membench/session_a.py). The CLI emits subtype and
    terminal_reason together on a turn-limit hit; a stream carrying only one of
    them is one this code does not understand, and "not understood" must fail
    closed. With `and` here, either of these half-signals is ACCEPTED as an
    interrupted session A."""
    _fake_run_agent(monkeypatch, _raw(**over))
    notes_dir = tmp_path / "notes"
    with pytest.raises(SessionAError, match="not interrupted"):
        generate_session_a(_task(), tmp_path / "ws", notes_dir, max_turns=2, model="claude-sonnet-5")
    assert not notes_dir.exists()


def test_generate_session_a_uses_the_last_result_event(monkeypatch, tmp_path: Path):
    """`_result_event` returns the LAST result event. With two in the stream -
    an early error_max_turns, then a success - taking the FIRST would accept a
    session A that actually finished, which is the exact failure D66 exists to
    stop."""
    raw = _raw() + _raw(subtype="success", terminal_reason="completed", stop_reason="end_turn")
    _fake_run_agent(monkeypatch, raw)
    with pytest.raises(SessionAError, match="not interrupted"):
        generate_session_a(_task(), tmp_path / "ws", tmp_path / "notes",
                           max_turns=2, model="claude-sonnet-5")


def test_generate_session_a_refuses_a_session_that_did_no_work(monkeypatch, tmp_path: Path):
    """An interrupted session that investigated nothing is not memory. Measured:
    a max_turns=1 run emitting one tool_use block and no text writes a 0-byte
    transcript and a pure-template current.md; the four arms then differ
    textually (so test_arms_differ_from_each_other stays green) while carrying
    the same information FloorArm already has from the issue title. Four arms,
    one experiment, all green - so it has to raise here."""
    _fake_run_agent(monkeypatch, _raw(), tool_calls=MIN_SESSION_A_TOOL_CALLS - 1)
    notes_dir = tmp_path / "notes"
    with pytest.raises(SessionAError, match="too little work") as e:
        generate_session_a(_task(), tmp_path / "ws", notes_dir, max_turns=2, model="claude-sonnet-5")
    # Names the signal that actually failed, and only that one.
    # Only the positive half is assertable. This previously also checked that
    # the notes-floor message was ABSENT, which is unfalsifiable: the tool-call
    # floor returns before the notes floor is evaluated, so the two messages can
    # never co-occur. Verified by setting MIN_SESSION_A_NOTES_CHARS to 100000 --
    # this test still passed, because the notes floor never ran.
    assert "tool calls <" in str(e.value), e.value
    assert not notes_dir.exists(), "a no-work run must leave no session-A material behind"


@pytest.mark.parametrize(
    "text", ["", "   \n  ", "looking into it"],
    ids=["empty", "whitespace", "one-liner"],
)
def test_generate_session_a_refuses_a_session_that_wrote_nothing_down(monkeypatch, tmp_path: Path, text):
    """The tool-call floor alone does NOT defend what it claims to: three calls
    on `f0.py`/`f1.py`/`f2.py` with no prose is a 47-char record, and before
    any of these floors existed that transcript was ACCEPTED - four textually
    distinct, INFORMATIONALLY IDENTICAL preambles (task_id, issue_title, turn
    count, all of which FloorArm already has from the issue text), with
    test_arms_differ_from_each_other still green.

    NARROWED, deliberately, when the notes stopped being `transcript.text`
    (G5): the floor is now measured on the generated notes, so this no longer
    proves "empty text is rejected" - empty text with a REAL tool-call log is
    now accepted on purpose, and
    test_notes_are_built_from_what_session_a_actually_did covers that. What it
    still proves is that a degenerate record is rejected and that the error
    names which of the two signals failed."""
    _fake_run_agent(monkeypatch, _raw(), text=text)
    notes_dir = tmp_path / "notes"
    with pytest.raises(SessionAError, match="chars of notes <") as e:
        generate_session_a(_task(), tmp_path / "ws", notes_dir, max_turns=2, model="claude-sonnet-5")
    assert "tool calls <" not in str(e.value), e.value
    assert not notes_dir.exists(), "a no-notes run must leave no session-A material behind"


def test_generate_session_a_refuses_when_no_result_event_was_emitted(monkeypatch, tmp_path: Path):
    """Fail closed: a crashed/auth-failed run has no result event at all, and
    "unknown" must never read as "interrupted"."""
    _fake_run_agent(monkeypatch, json.dumps({"type": "system", "subtype": "init"}) + "\n")
    with pytest.raises(SessionAError, match="no result event"):
        generate_session_a(_task(), tmp_path / "ws", tmp_path / "notes",
                           max_turns=2, model="claude-sonnet-5")


def test_session_a_prompt_interpolates_both_issue_fields():
    p = SESSION_A_PROMPT.format(title="T-marker", body="B-marker")
    assert "T-marker" in p and "B-marker" in p


# --- D69: the installed notes must survive the real scoring path ------------

def test_grep_notes_survive_the_real_scoring_path(synthetic_repo, tmp_path: Path):  # noqa: F811
    """D69, measured not reasoned. `run_tests` -> `_reset_test_surface` deletes
    anything in the test surface and unlinks conftest.py/sitecustomize.py by
    NAME at any depth (membench/runner.py:122,330-333). If that pass ate
    `.membench-notes`, GrepArm and VaultArm would silently degrade into
    FloorArm and every arm would measure the same thing.

    Docker, like the rest of tests/test_runner.py, but on the fast synthetic
    repo rather than liquid."""
    repo, base_sha, fix_sha = synthetic_repo
    task = BenchTask(
        task_id="synthetic", repo="local/synthetic", issue_number=0,
        issue_title="t", issue_body="b", base_sha=base_sha, fix_sha=fix_sha, changed_files=[],
    )
    wdir = _synthetic_workdir(repo, base_sha, tmp_path / "ws")
    notes_dir = tmp_path / "notes"
    (notes_dir / "sub").mkdir(parents=True)
    (notes_dir / "current.md").write_text("STATE: mid-fix")
    (notes_dir / "sub" / "dead-ends.md").write_text("tried Y, it failed")
    # KNOWN CEILING, asserted rather than assumed: `_looks_like_config_name`
    # matches by BARE NAME at any depth, so a note called conftest.py is
    # deleted even inside .membench-notes. Notes are markdown, so this costs
    # nothing today - but it is measured here so a future note-writer that
    # picks one of _DISCARD_NAMES finds out from a test and not from two arms
    # quietly scoring the same.
    (notes_dir / "conftest.py").write_text("# not a note")

    from membench.runner import run_tests

    GrepArm().install(task, wdir, notes_dir)
    # Proves the delete pass actually RAN in this container invocation - without
    # it, a no-op reset would make the survival assertions below vacuous.
    canary = wdir / "tests" / "test_planted_by_agent.py"
    canary.write_text("def test_planted():\n    assert True\n")

    results = run_tests(wdir, task, None, reference_repo=repo)

    assert results.get("tests/test_good.py::test_good") is True, "scoring itself must still work"
    assert not canary.exists(), "delete pass did not run; the survival checks below prove nothing"
    assert (wdir / NOTES_MOUNT / "current.md").read_text() == "STATE: mid-fix"
    assert (wdir / NOTES_MOUNT / "sub" / "dead-ends.md").read_text() == "tried Y, it failed"
    assert not (wdir / NOTES_MOUNT / "conftest.py").exists()


# --- D66, empirically -------------------------------------------------------

# Measured across five live runs, not derived. This is the whole point of the
# test below, so the numbers are recorded rather than summarised:
#   max_turns=5,  1 file  -> INTERRUPTED: subtype='error_max_turns'
#                            terminal_reason='max_turns' stop_reason='tool_use'
#                            num_turns=6, 8 tool calls, 120 chars of text.
#   max_turns=12, 1 file  -> the agent FINISHED in 9 turns: subtype='success'
#                            terminal_reason='completed' stop_reason='end_turn'.
#                            Correctly rejected as "not interrupted" - and
#                            'completed' is a value NO mock here had, because
#                            _raw() was written from the max_turns hit alone.
#   max_turns=8,  4 files -> interrupted, num_turns=9, 88 chars of text.
#   max_turns=8,  4 files -> interrupted, num_turns=9, 8 tool calls, 78 chars,
#                            and the raw stream carried thinking:7 text:1
#                            tool_use:8 - the reasoning is in `thinking`, which
#                            _parse_stream discards (membench/driver.py:262).
#   max_turns=5,  1 file  -> interrupted, num_turns=6, and ZERO chars of text.
#                            Same configuration as the 120-char run.
#   max_turns=5,  1 file  -> interrupted, num_turns=6, 5 tool calls, 756 chars
#                            of text, cost $0.14, 30s. Instrumented over every
#                            string field of every content block:
#                            counts {'thinking': 4, 'text': 2, 'tool_use': 5},
#                            thinking.thinking = 0 chars, signature = 7584.
#                            The thinking blocks are EMPTY on the wire - the
#                            reasoning is not in `raw` either, so re-scanning
#                            for it (the tools_from_init precedent) recovers
#                            nothing. Notes are built from tool_calls + text.
#
# The 0-chars run is why this test used to stop at `run_agent` instead of
# calling generate_session_a: against a floor on `.text` an end-to-end live
# assertion was a coin flip. The floor now measures the GENERATED NOTES, whose
# tool-call log is 5-8 entries on every interrupted run measured (~370 chars at
# 5 calls, against MIN_SESSION_A_NOTES_CHARS=80), so end-to-end is back.
LIVE_MAX_TURNS = 5


@pytest.mark.live
def test_the_cli_really_reports_an_interruption(tmp_path: Path, capsys):
    """The only thing that settles D66: what the CLI ACTUALLY reports when
    --max-turns is hit.

    It is also the only test in this file that is not circular. `_raw()` above
    hardcodes the same strings the production code checks, so every mocked test
    agrees with itself about the CLI's vocabulary; `grep -rn terminal_reason`
    across the repo hits only session_a.py and this file. This run compares the
    REAL stream against the production allowlists themselves - imported, not
    copied - so a CLI that renames a value fails here instead of passing
    everywhere.

    END TO END through `generate_session_a` (G6), not `run_agent` directly:
    the interruption check, the minimum-work floors and the notes writer are
    all on the path a real benchmark run takes, and the mocked tests above can
    only prove they agree with a mock. It was narrowed to `run_agent` while the
    floor was measured on `.text`, which is 0 chars on real interrupted runs;
    the floor now measures the notes, whose tool-call log is 5-8 entries on
    every measured run. Cost measured at $0.14 and 30s for one run at
    max_turns=5 - the $0.03 figure in an earlier revision of this comment was
    from a smaller prompt and does not hold."""
    from membench.session_a import _INTERRUPTED_SUBTYPES, _INTERRUPTED_TERMINAL_REASONS

    wdir = tmp_path / "ws"
    wdir.mkdir()
    (wdir / "cycle.py").write_text("def cycle(items):\n    return items[0]\n")
    notes_dir = tmp_path / "notes"
    task = _task(
        issue_title="cycle() only ever returns the first item",
        issue_body="cycle.py's cycle() should rotate through items on repeated calls. "
                   "It returns items[0] every time. Investigate and start the fix.",
    )

    started = time.monotonic()
    t = generate_session_a(task, wdir, notes_dir, max_turns=LIVE_MAX_TURNS,
                           model="claude-sonnet-5", timeout_s=300)
    wall = time.monotonic() - started

    result = _result_event(t.raw) or {}
    notes = (notes_dir / "session-a-transcript.md").read_text()
    current = (notes_dir / "current.md").read_text()
    with capsys.disabled():
        print(f"\nLIVE D66 (max_turns={LIVE_MAX_TURNS}): "
              f"subtype={result.get('subtype')!r} "
              f"terminal_reason={result.get('terminal_reason')!r} "
              f"stop_reason={t.stop_reason!r} num_turns={t.num_turns} "
              f"tool_calls={len(t.tool_calls)} text_chars={len(t.text.strip())} "
              f"cost_usd={t.cost_usd} exit_code={t.exit_code} wall_s={wall:.1f}\n"
              f"--- session-a-transcript.md ({len(notes)} chars) ---\n{notes}\n"
              f"--- current.md ({len(current)} chars) ---\n{current}")

    assert result, "no result event in the stream - the run's outcome is unknown"
    # Against the production sets, not against string literals: this is the
    # assertion that breaks the circle.
    assert result.get("subtype") in _INTERRUPTED_SUBTYPES, result.get("subtype")
    assert result.get("terminal_reason") in _INTERRUPTED_TERMINAL_REASONS, \
        result.get("terminal_reason")
    assert t.num_turns > LIVE_MAX_TURNS - 1, (t.num_turns, LIVE_MAX_TURNS)
    # Reaching here at all means both floors passed on a REAL transcript -
    # generate_session_a raises otherwise. Recorded anyway so a drop in real
    # tool-call volume is visible in the failure message rather than buried in
    # a SessionAError.
    assert len(t.tool_calls) >= MIN_SESSION_A_TOOL_CALLS, len(t.tool_calls)
    # The notes must carry what session A actually touched - not just the
    # template. Whatever tool the model reached for first, its detail line has
    # to be in the record; that is the difference between memory and a header.
    assert any(_detail(c) and _detail(c) in notes for c in t.tool_calls), notes
    assert "INTERRUPTED" in current and "session-a-transcript.md" in current
    assert len(notes) > len(current) > 0
    # stop_reason is the MODEL's last-message reason, NOT the session's -
    # measured 'tool_use' on a max-turns hit, a value a completed session could
    # also produce. Recorded so a CLI change that repurposes it is visible; it
    # can never be the interruption signal on its own.
    assert t.stop_reason in ("tool_use", "end_turn", None), t.stop_reason
