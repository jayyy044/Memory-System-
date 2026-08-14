from dataclasses import FrozenInstanceError, asdict, replace

import pytest

from membench.models import ToolCall, Transcript
from membench.scoring.trace import TraceScore, TraceScoreError, score_trace


def _t(cmds=(), files=()) -> Transcript:
    return Transcript(
        text="",
        tool_calls=[ToolCall("Bash", command=c) for c in cmds]
        + [ToolCall("Read", file_path=f) for f in files],
    )


def test_detects_redone_command():
    s = score_trace(_t(["python -m pytest tests/test_cycle.py"]),
                    _t(["python -m pytest tests/test_cycle.py"]))
    assert s.redone_commands == ["python -m pytest tests/test_cycle.py"]


def test_exact_match_only_no_substring_false_positive():
    # plan:1036 - `npm test` must not match `npm test:watch`.
    assert score_trace(_t(["npm test"]), _t(["npm test:watch"])).redone_commands == []


def test_exact_match_in_the_other_direction_too():
    # The mirror of the case above: session A's command being a prefix of
    # session B's is the likelier substring bug, and is equally wrong.
    assert score_trace(_t(["npm test:watch"]), _t(["npm test"])).redone_commands == []


def test_whitespace_normalised_before_matching():
    s = score_trace(_t(["npm  test"]), _t(["npm\ttest"]))
    assert s.redone_commands == ["npm test"]


def test_matching_is_case_sensitive():
    # Shell commands and POSIX paths are case sensitive; folding case would
    # invent matches (`Makefile` vs `makefile` are different files).
    s = score_trace(_t(["LS"], files=["SRC/A.py"]), _t(["ls"], files=["src/a.py"]))
    assert s.redone_commands == []
    assert s.redone_files == []


def test_normalisation_does_not_canonicalise_paths():
    # `./src/a.py` and `src/a.py` are the same file, but nothing here resolves
    # paths - normalisation is whitespace only. Pinned so a future "helpful"
    # strip of `./` is a deliberate change, not an accident.
    s = score_trace(_t(files=["./src/a.py"]), _t(files=["src/a.py"]))
    assert s.files_measured is True
    assert s.redone_files == []


def test_redone_commands_deduped_and_in_session_b_order():
    a = _t(["ls", "pwd"])
    b = _t(["pwd", "ls", "pwd"])
    assert score_trace(a, b).redone_commands == ["pwd", "ls"]


def test_redone_files_deduped_and_in_session_b_order():
    a = _t(files=["src/a.py", "src/b.py"])
    b = _t(files=["src/b.py", "src/a.py", "src/b.py"])
    assert score_trace(a, b).redone_files == ["src/b.py", "src/a.py"]


def test_detects_redone_files():
    # D70: re-reading/re-editing the same file is the bulk of a real
    # investigation and is exactly what memory should prevent. Bash-only
    # scoring (plan:1075) is dark for most transcripts.
    a = _t(files=["src/a.py", "src/b.py"])
    b = _t(files=["src/b.py", "src/b.py", "src/c.py"])
    s = score_trace(a, b)
    assert s.redone_files == ["src/b.py"]
    # Session A ran no commands, so the command side was never measured - it is
    # not a clean zero. See test_measuredness_is_per_category below.
    assert s.commands_measured is False


def test_commands_and_files_stay_in_separate_lists():
    a = Transcript(text="", tool_calls=[ToolCall("Bash", command="ls"),
                                        ToolCall("Edit", file_path="ls")])
    b = Transcript(text="", tool_calls=[ToolCall("Bash", command="ls"),
                                        ToolCall("Edit", file_path="ls")])
    s = score_trace(a, b)
    assert s.redone_commands == ["ls"]
    assert s.redone_files == ["ls"]


def test_a_command_matching_a_file_path_is_not_redone_work():
    # Cross-category matching would be nonsense: session A running `ls` has
    # nothing to do with session B reading a file called `ls`. Both categories
    # are populated on both sides so both are measured and both must be empty.
    a = Transcript(text="", tool_calls=[ToolCall("Bash", command="ls"),
                                        ToolCall("Read", file_path="src/a.py")])
    b = Transcript(text="", tool_calls=[ToolCall("Read", file_path="ls"),
                                        ToolCall("Bash", command="src/a.py")])
    s = score_trace(a, b)
    assert (s.commands_measured, s.files_measured) == (True, True)
    assert s.redone_commands == []
    assert s.redone_files == []


def test_grep_and_glob_only_session_a_is_unmeasured_not_a_clean_zero():
    # D73/DEBT-1: driver.py:268-275 drops Grep/Glob `pattern`, so these calls
    # carry neither command nor file_path. Scoring must not crash - but the
    # result must not read as "session B redid nothing" either, because
    # nothing was ever in the catalogue to redo.
    a = Transcript(text="", tool_calls=[ToolCall("Grep"), ToolCall("Glob")])
    s = score_trace(a, _t(["ls"], files=["src/a.py"]))
    assert (s.commands_measured, s.files_measured) == (False, False)
    with pytest.raises(TraceScoreError):
        s.redone_commands
    with pytest.raises(TraceScoreError):
        s.redone_files


def test_empty_session_a_is_unmeasured_not_a_clean_zero():
    # F1: session A is designed to be interrupted early. A short session A
    # yields an empty catalogue, and `[]` from an empty catalogue would be a
    # perfect-looking, memory-positive zero across every arm at once.
    s = score_trace(Transcript(text="", tool_calls=[]),
                    _t(["ls", "pwd"], files=["src/a.py"]))
    assert (s.commands_measured, s.files_measured) == (False, False)
    with pytest.raises(TraceScoreError):
        s.redone_commands
    with pytest.raises(TraceScoreError):
        s.redone_files


def test_measuredness_is_per_category():
    # A session A with Bash but no file reads has a measurable command signal
    # and an unmeasurable file one. Collapsing them into one flag would blind
    # the measured half or falsely certify the unmeasured half.
    s = score_trace(_t(["ls"]), _t(["pwd"], files=["src/a.py"]))
    assert s.commands_measured is True
    assert s.redone_commands == []
    assert s.files_measured is False
    with pytest.raises(TraceScoreError):
        s.redone_files


def test_serialising_unmeasured_redone_fields_cannot_read_as_zero():
    # asdict() bypasses the properties, so the stored value is what lands in
    # any JSON report Task 9 writes. An unmeasured category must serialise as
    # None, never [].
    d = asdict(score_trace(_t(["ls"]), _t(["ls"])))
    assert d["commands_measured"] is True
    assert d["_redone_commands"] == ["ls"], d
    assert d["files_measured"] is False
    assert d["_redone_files"] is None, d
    assert d["dead_ends_measured"] is False
    assert d["_repeated_dead_ends"] is None, d
    # A measured category still serialises a real list.
    m = asdict(score_trace(_t(), _t(["ls"]), dead_ends=["ls"]))
    assert m["dead_ends_measured"] is True
    assert m["_repeated_dead_ends"] == ["ls"], m


def test_whitespace_only_values_are_not_matchable():
    # `if c.command` accepts "   ", which normalises to "" and then matches
    # anything else blank - a false positive, and it would also make an
    # otherwise-empty catalogue look measured.
    a = _t(["   "], files=["\t"])
    s = score_trace(a, a)
    assert (s.commands_measured, s.files_measured) == (False, False)


def test_unmeasured_dead_ends_raise_instead_of_reading_as_empty():
    # D71: the realistic call supplies no dead ends. Under the plan's code that
    # yields [], which reads as "session B repeated no dead ends" when in fact
    # nothing was measured. Reading it must be impossible, not falsy.
    s = score_trace(_t(["ls"]), _t(["ls"]))
    assert s.dead_ends_measured is False
    with pytest.raises(TraceScoreError):
        s.repeated_dead_ends


def test_empty_dead_ends_list_is_also_unmeasured():
    # An empty catalogue means the detector is blind, not that it looked and
    # found nothing. Same false green as passing nothing at all.
    s = score_trace(_t(["ls"]), _t(["ls"]), dead_ends=[])
    assert s.dead_ends_measured is False
    with pytest.raises(TraceScoreError):
        s.repeated_dead_ends


def test_detects_repeated_dead_end():
    b = _t(["python -m pytest -k broken_approach"])
    s = score_trace(_t(), b, dead_ends=["python -m pytest -k broken_approach"])
    assert s.dead_ends_measured is True
    assert s.repeated_dead_ends == ["python -m pytest -k broken_approach"]


def test_measured_but_none_repeated_is_readable_and_empty():
    s = score_trace(_t(), _t(["ls"]), dead_ends=["rm -rf /"])
    assert s.dead_ends_measured is True
    assert s.repeated_dead_ends == []


def test_dead_end_can_match_a_file_path_too():
    s = score_trace(_t(), _t(files=["src/red_herring.py"]),
                    dead_ends=["src/red_herring.py"])
    assert s.repeated_dead_ends == ["src/red_herring.py"]


def test_dead_ends_are_whitespace_normalised_like_everything_else():
    s = score_trace(_t(), _t(["npm\ttest"]), dead_ends=["npm  test"])
    assert s.repeated_dead_ends == ["npm test"]


def test_dead_ends_deduped():
    b = _t(["ls", "ls"])
    s = score_trace(_t(), b, dead_ends=["ls", "ls"])
    assert s.repeated_dead_ends == ["ls"]


def test_repeated_dead_ends_follow_session_b_chronology():
    # F5: commands and files are matched against one chronological sequence,
    # not commands-then-files. Session B touched the file first, so the file
    # comes first.
    b = Transcript(text="", tool_calls=[ToolCall("Read", file_path="f"),
                                        ToolCall("Bash", command="c")])
    s = score_trace(_t(), b, dead_ends=["c", "f"])
    assert s.repeated_dead_ends == ["f", "c"]


# A tuple is deliberately accepted - it is a sequence of str and carries none
# of the risks below. The rejected cases are the ones that would fabricate a
# measured catalogue out of nothing.
@pytest.mark.parametrize("bad", ["ls", [""], ["   "], [None], [123], 5])
def test_malformed_dead_ends_are_rejected(bad):
    # A bare string iterates into characters and a blank entry contributes
    # nothing, yet both would set dead_ends_measured=True - a junk catalogue
    # reading as measured is D71 inverted.
    with pytest.raises(TraceScoreError):
        score_trace(_t(), _t(["ls"]), dead_ends=bad)


def test_default_traceScore_is_unmeasured_everywhere():
    # A hand-built TraceScore must default to unmeasured, not to a clean pass.
    s = TraceScore()
    for name in ("redone_commands", "redone_files", "repeated_dead_ends"):
        with pytest.raises(TraceScoreError):
            getattr(s, name)


@pytest.mark.parametrize("kwargs", [
    {"dead_ends_measured": True},
    {"commands_measured": True},
    {"files_measured": True},
    {"_repeated_dead_ends": []},
    {"_redone_commands": []},
    {"_redone_files": ["x"]},
])
def test_flag_and_payload_cannot_disagree(kwargs):
    # F2: without an invariant, `replace(score, dead_ends_measured=True)` gets
    # past the raise and returns None, and `not None` is the clean-pass
    # reading. An inconsistent score must be unconstructable.
    with pytest.raises(TraceScoreError):
        TraceScore(**kwargs)


def test_replace_cannot_forge_a_measured_score():
    s = score_trace(_t(["ls"]), _t(["ls"]))
    with pytest.raises(TraceScoreError):
        replace(s, dead_ends_measured=True)


def test_assignment_cannot_forge_a_measured_score():
    # __post_init__ runs at construction only, so on a mutable dataclass this
    # assignment walks straight past the invariant: the read then returns the
    # stored None and `not None` is the clean-pass reading. frozen=True is what
    # closes that door, and this pins it.
    s = score_trace(_t(["ls"]), _t(["ls"]))
    with pytest.raises(FrozenInstanceError):
        s.dead_ends_measured = True
    assert s.dead_ends_measured is False
    with pytest.raises(TraceScoreError):
        s.repeated_dead_ends
