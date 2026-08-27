"""The rejections are the point. A validator that accepts everything is the
`console.assert` suite found in dsh-memory-connect: green, wired to nothing,
proving nothing."""

from datetime import datetime, timezone

import pytest

from memory.record import (
    Anchor, FailureMode, InvalidMemory, Kind, Link, LinkType,
    Memory, Scope, Source, Status, now,
)


def make(**over):
    base = dict(
        id="m1", kind=Kind.FACT, text="the parser is recursive descent",
        scope=Scope(repo="/repo"), source=Source(session_id="s1"),
        created_at=now(), updated_at=now(),
    )
    return Memory(**{**base, **over})


def anchored(**over):
    base = dict(kind=Kind.CONVENTION,
                anchor=Anchor(commit="c75c01f", paths=("src/a.py",)))
    return make(**{**base, **over})


# --- the differentiator: unverifiable memory cannot be written ------------

@pytest.mark.parametrize("kind", [Kind.DEAD_END, Kind.DECISION, Kind.CONVENTION, Kind.TASK])
def test_anchored_kinds_refuse_to_exist_without_an_anchor(kind):
    """fresh.sh reports `uncheckable` at read time and 37 of 52 notes were
    never checked. Rejecting at write is the same rule, enforced earlier."""
    extra = {}
    if kind is Kind.TASK:
        extra["status"] = Status.OPEN
    if kind is Kind.DEAD_END:
        extra["failure_mode"] = FailureMode.WRONG_DIRECTION
    with pytest.raises(InvalidMemory, match="requires an anchor"):
        make(kind=kind, **extra)


def test_a_fact_about_the_world_takes_no_anchor():
    make(kind=Kind.FACT, sources=("https://arxiv.org/abs/1234",))
    with pytest.raises(InvalidMemory, match="takes no anchor"):
        make(kind=Kind.FACT, anchor=Anchor(commit="c75c01f", paths=("a.py",)))


def test_anchor_commit_must_look_like_a_sha():
    with pytest.raises(InvalidMemory, match="not a commit sha"):
        anchored(anchor=Anchor(commit="HEAD", paths=("src/a.py",)))
    with pytest.raises(InvalidMemory, match="not a commit sha"):
        anchored(anchor=Anchor(commit="", paths=("src/a.py",)))


def test_anchor_paths_must_exist_and_stay_inside_the_repo():
    with pytest.raises(InvalidMemory, match="at least one path"):
        anchored(anchor=Anchor(commit="c75c01f", paths=()))
    for bad in ("/etc/passwd", "../../secrets"):
        with pytest.raises(InvalidMemory, match="must not escape"):
            anchored(anchor=Anchor(commit="c75c01f", paths=(bad,)))


# --- fields that are meaningless on the wrong kind ------------------------

def test_status_only_on_tasks():
    with pytest.raises(InvalidMemory, match="status is meaningless"):
        anchored(status=Status.DONE)
    with pytest.raises(InvalidMemory, match="requires a status"):
        make(kind=Kind.TASK, anchor=Anchor(commit="c75c01f", paths=("a.py",)))


def test_failure_fields_only_on_dead_ends():
    for f in ({"failure_mode": FailureMode.WRONG_DIRECTION}, {"retry_when": "later"}):
        with pytest.raises(InvalidMemory, match="meaningless"):
            anchored(**f)


def test_a_dead_end_must_say_why_it_failed():
    """Recording THAT something failed without WHY is what all eight surveyed
    systems already do."""
    with pytest.raises(InvalidMemory, match="requires a failure_mode"):
        anchored(kind=Kind.DEAD_END)


def test_both_failure_modes_are_expressible():
    """The distinction no surveyed system can represent: identical records
    in all eight."""
    a = anchored(kind=Kind.DEAD_END, failure_mode=FailureMode.WRONG_DIRECTION)
    b = anchored(kind=Kind.DEAD_END, failure_mode=FailureMode.BROKEN_IMPLEMENTATION)
    assert a.failure_mode is not b.failure_mode


# --- time ------------------------------------------------------------------

def test_naive_datetimes_are_refused():
    """MemOS stamps every record with datetime.now().isoformat() - no zone."""
    naive = datetime(2026, 8, 26, 12, 0, 0)
    with pytest.raises(InvalidMemory, match="timezone-aware"):
        make(created_at=naive)
    with pytest.raises(InvalidMemory, match="timezone-aware"):
        make(updated_at=naive)
    make(created_at=datetime(2026, 8, 26, tzinfo=timezone.utc))


# --- links -----------------------------------------------------------------

def test_links_reject_self_reference_and_duplicates():
    with pytest.raises(InvalidMemory, match="cannot link to itself"):
        make(links=(Link(LinkType.SUPERSEDES, "m1"),))
    with pytest.raises(InvalidMemory, match="duplicate link"):
        make(links=(Link(LinkType.CONTRADICTS, "x"), Link(LinkType.CONTRADICTS, "x")))
    make(links=(Link(LinkType.CONTRADICTS, "x"), Link(LinkType.SUPERSEDES, "x")))


# --- the basics ------------------------------------------------------------

@pytest.mark.parametrize("over,msg", [
    ({"id": ""}, "id is required"),
    ({"text": "   "}, "text is required"),
    ({"scope": Scope(repo="")}, "scope.repo is required"),
    ({"source": Source(session_id="")}, "source.session_id is required"),
])
def test_required_fields(over, msg):
    with pytest.raises(InvalidMemory, match=msg):
        make(**over)


def test_frozen():
    """Immutability is convention-only in every surveyed system; nobody
    enforces it and everybody regrets it."""
    m = make()
    with pytest.raises(Exception):
        m.text = "changed"
