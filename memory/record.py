"""What a session learned, in a form a later session can check.

Every field here earns its place against a specific failure found by reading
eight competing memory systems at source. The rule that produced this shape:

    a field ships only if something branches on it.

Ten fields across those eight are written and never read - a tag list that
cannot be searched, a confidence written as a constant on two incompatible
numeric scales, thresholds named in docstrings that do not exist in the
repository. Every one of them was the field that would have answered the
question its system could not answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class Kind(str, Enum):
    """What sort of claim this is. Decides which other fields apply."""

    FACT = "fact"              # something true, about the code or the world
    DECISION = "decision"      # we chose X over Y
    DEAD_END = "dead_end"      # tried X, stopped
    TASK = "task"              # a unit of work with a state
    CONVENTION = "convention"  # how this codebase does things


class Status(str, Enum):
    """Work state. Only meaningful on `Kind.TASK`.

    Distinct from every `status` field in the surveyed systems, all of which
    describe the RECORD's lifecycle (active/archived/decayed) rather than the
    WORK's state. OpenViking's `outcome` is the sole prior art, and it ships
    `merge_op: immutable` - a trajectory can never move from `unfinished` to
    `success`. This one is mutable, which is the point.
    """

    OPEN = "open"
    DOING = "doing"
    DONE = "done"
    ABANDONED = "abandoned"


class FailureMode(str, Enum):
    """Why a dead end failed. Only meaningful on `Kind.DEAD_END`.

    Unrepresentable in all eight systems surveyed: "the approach was wrong"
    and "the approach is fine, our attempt was broken" produce a byte-identical
    record in every one of them. MemOS classifies tool-call root causes; that
    is the closest prior art and it does not reach design decisions.
    """

    WRONG_DIRECTION = "wrong_direction"
    BROKEN_IMPLEMENTATION = "broken_implementation"


class LinkType(str, Enum):
    """Only types something branches on.

    OpenViking declares `contradicts`, `caused_by`, `belongs_to` and
    `evolved_from`; the only values any of its code reads are `derived_from`
    and `related_to`. The rest colour edges in an HTML viewer. So: a type goes
    in here when a reader changes behaviour on it, and not before.
    """

    SUPERSEDES = "supersedes"      # this replaces target; target stops surfacing
    CONTRADICTS = "contradicts"    # both surface, flagged, unresolved
    CAUSED_BY = "caused_by"        # target explains why this exists
    BLOCKS = "blocks"              # this cannot proceed until target does


# Kinds that describe THIS codebase, and therefore expire when it changes.
ANCHORED = frozenset({Kind.DEAD_END, Kind.DECISION, Kind.CONVENTION, Kind.TASK})

_SHA = re.compile(r"^[0-9a-f]{7,40}$")


@dataclass(frozen=True)
class Anchor:
    """What this memory depends on, and when it was last known true.

    The differentiator. Zero of eight surveyed systems anchor a memory to code
    state - every freshness signal in all of them is wall-clock, which cannot
    answer "has the thing I described changed". The content hashes that exist
    hash the memory, never the code.

    `paths` does double duty: it is the staleness input for
    `git diff <commit>..HEAD -- <paths>`, and the key for surfacing a memory
    when a session touches those files - retrieval that needs no query text.
    """

    commit: str
    paths: tuple[str, ...]


@dataclass(frozen=True)
class Scope:
    """Where this memory applies.

    `branch` is the field the whole cohort is missing. Every scope vocabulary
    surveyed bottoms out at a directory. OpenViking is the one system that
    ever had the branch name in hand - its Claude Code ingest reads `gitBranch`
    off every transcript record - and nothing ever reads it back.
    """

    repo: str
    branch: str | None = None


@dataclass(frozen=True)
class Source:
    """Who wrote this.

    `agent` separates the main thread from a subagent. dsh-memento's `agentKey`
    is the proven design BECAUSE it is enforced on read, not merely stored.
    OpenViking smuggles the same idea into a session-id string prefix; its
    default ingest path discards subagent turns entirely as "low-value".
    """

    session_id: str
    agent: str = "main"


@dataclass(frozen=True)
class Link:
    type: LinkType
    target_id: str


class InvalidMemory(ValueError):
    """A memory that cannot be checked later must not be stored.

    Raised at write time, deliberately. `fresh.sh` in this setup reports
    `uncheckable` at READ time instead - and 37 of 52 notes in one vault had
    never been checked once, because "uncheckable" reads like a documentation
    problem and gets ignored. Rejecting at write is the same rule, enforced
    where it still costs nothing to fix.
    """


@dataclass(frozen=True)
class Memory:
    id: str
    kind: Kind
    text: str
    scope: Scope
    source: Source
    created_at: datetime
    updated_at: datetime

    # Required on ANCHORED kinds, meaningless on Kind.FACT about the world.
    anchor: Anchor | None = None

    # External provenance, for facts about the world rather than this repo.
    # Research findings do not expire when code changes; they expire when
    # someone re-reads the source and supersedes them.
    sources: tuple[str, ...] = ()

    status: Status | None = None            # Kind.TASK only
    failure_mode: FailureMode | None = None  # Kind.DEAD_END only
    retry_when: str | None = None            # Kind.DEAD_END only, prose on purpose
    links: tuple[Link, ...] = ()

    def __post_init__(self) -> None:
        validate(self)


def validate(m: Memory) -> None:
    """Raise `InvalidMemory` on anything unverifiable or self-contradictory."""
    if not m.id:
        raise InvalidMemory("id is required")
    if not m.text.strip():
        raise InvalidMemory("text is required")
    if not m.scope.repo:
        raise InvalidMemory("scope.repo is required")
    if not m.source.session_id:
        raise InvalidMemory("source.session_id is required")

    for name, value in (("created_at", m.created_at), ("updated_at", m.updated_at)):
        # Naive datetimes are how MemOS stamps every record
        # (`datetime.now().isoformat()`, no zone). Two machines in different
        # zones then disagree about which memory is newer.
        if value.tzinfo is None or value.utcoffset() is None:
            raise InvalidMemory(f"{name} must be timezone-aware")

    anchored = m.kind in ANCHORED
    if anchored:
        if m.anchor is None:
            raise InvalidMemory(
                f"kind={m.kind.value} requires an anchor: a memory about this "
                "codebase that names no commit can never be checked again"
            )
        if not _SHA.match(m.anchor.commit):
            raise InvalidMemory(f"anchor.commit is not a commit sha: {m.anchor.commit!r}")
        if not m.anchor.paths:
            raise InvalidMemory("anchor.paths must name at least one path")
        if any(p.startswith("/") or ".." in p for p in m.anchor.paths):
            raise InvalidMemory("anchor.paths must be repo-relative and must not escape")
    elif m.anchor is not None:
        raise InvalidMemory(f"kind={m.kind.value} takes no anchor; use sources instead")

    if m.status is not None and m.kind is not Kind.TASK:
        raise InvalidMemory(f"status is meaningless on kind={m.kind.value}")
    if m.kind is Kind.TASK and m.status is None:
        raise InvalidMemory("kind=task requires a status")

    for name, value in (("failure_mode", m.failure_mode), ("retry_when", m.retry_when)):
        if value is not None and m.kind is not Kind.DEAD_END:
            raise InvalidMemory(f"{name} is meaningless on kind={m.kind.value}")
    if m.kind is Kind.DEAD_END and m.failure_mode is None:
        raise InvalidMemory(
            "kind=dead_end requires a failure_mode: recording THAT something "
            "failed without WHY is what all eight surveyed systems already do"
        )

    seen = set()
    for link in m.links:
        if link.target_id == m.id:
            raise InvalidMemory("a memory cannot link to itself")
        key = (link.type, link.target_id)
        if key in seen:
            raise InvalidMemory(f"duplicate link {link.type.value} -> {link.target_id}")
        seen.add(key)


def now() -> datetime:
    return datetime.now(timezone.utc)
