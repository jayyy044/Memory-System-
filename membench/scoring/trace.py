"""Did session B redo session A's work?

Correctness says whether the bug got fixed. This says whether session B had to
re-derive what session A already found - the benchmark's actual hypothesis.

What the numbers mean, and what they do not:

- `redone_commands` / `redone_files` are OVERLAP, not WASTE (D72). Session B
  re-running the test suite or re-reading the file it is about to edit is
  legitimate verification. Separating verification from wasted re-derivation
  needs per-call outcome data that `Transcript` does not carry, so this module
  does not guess. Compare the number across arms; do not read a single run's
  value as "work thrown away".

- Grep and Glob are invisible here (D73). `membench/driver.py:268-275` reads
  only `command` and `file_path`/`path` from a tool's input, so a Grep's
  `pattern` is dropped at parse time and its ToolCall carries nothing to match
  on. Repeated searching therefore scores as zero, not as a miss. That is
  DEBT-1; it is fixed in driver.py once both consumers exist, not patched here.

- EVERY metric here RAISES unless it was actually measured. A metric is
  measured only when session A supplied a non-empty catalogue to match against
  (D71, generalised). Session A is designed to be interrupted early, and a
  Grep-heavy or short session A leaves an empty catalogue - which would
  otherwise produce `[]`, i.e. a perfect, strongly memory-positive zero, in
  every arm at once. Commands, files and dead ends are tracked separately
  because they go blind separately: a session A with Bash but no file reads has
  a measurable command signal and an unmeasurable file one.

How a report should CONSUME this, because the obvious way is wrong:

    if score.files_measured:
        row["redone_files"] = score.redone_files
    else:
        row["redone_files"] = None      # or "unmeasured" - an explicit token

Branch on the flag. Do NOT wrap the read in `try/except TraceScoreError` and
fall back to `[]` - that reinstates, in the caller, the exact false green this
module raises to prevent, and no test here can catch it. An unmeasured run is
not a data point: it must be excluded from any cross-arm mean, not counted as
a zero, or a benchmark whose session A went blind will report every arm as
perfectly memory-positive.
"""

from dataclasses import dataclass, field

from membench.models import ToolCall, Transcript


class TraceScoreError(RuntimeError):
    pass


def _normalize(s: str | None) -> str:
    """Whitespace only. No case folding and no path canonicalisation: shell
    commands and POSIX paths are case sensitive, and `./a.py` is not resolved
    against `a.py` because nothing here knows the working directory."""
    return " ".join((s or "").split())


def _first_seen(values: list[str], against: set[str]) -> list[str]:
    """`values` that are in `against`, deduped, in first-occurrence order."""
    out: list[str] = []
    for v in values:
        if v in against and v not in out:
            out.append(v)
    return out


def _b_sequence(calls: list[ToolCall]) -> list[tuple[str, str]]:
    """Session B's `(kind, value)` pairs in chronological order, blanks
    dropped. One sequence, not two lists, so dead-end ordering is session B's
    real ordering rather than all-commands-then-all-files."""
    seq: list[tuple[str, str]] = []
    for c in calls:
        for kind, raw in (("command", c.command), ("file", c.file_path)):
            v = _normalize(raw)
            if v:
                seq.append((kind, v))
    return seq


@dataclass(frozen=True)
class TraceScore:
    """Every list is behind a property that raises unless its `*_measured`
    flag is set - see D71 and the module docstring. The stored fields are
    `None`, never `[]`, when unmeasured: `dataclasses.asdict` bypasses the
    properties, so an empty list would put the exact false green this guards
    against straight into any JSON report Task 9 writes.

    `frozen=True` is load-bearing, not style. `__post_init__` runs at
    construction only, so on a mutable dataclass `score.dead_ends_measured =
    True` walks straight past the invariant: the read then returns the stored
    `None`, and `not None` is the clean-pass reading. Freezing is what makes
    the invariant's claim below true rather than aspirational."""

    commands_measured: bool = False
    files_measured: bool = False
    dead_ends_measured: bool = False
    _redone_commands: list[str] | None = None
    _redone_files: list[str] | None = None
    _repeated_dead_ends: list[str] | None = None

    _PAIRS = (
        ("commands_measured", "_redone_commands"),
        ("files_measured", "_redone_files"),
        ("dead_ends_measured", "_repeated_dead_ends"),
    )

    def __post_init__(self) -> None:
        # Without this, `dataclasses.replace(score, dead_ends_measured=True)`
        # gets past the raise and returns None - and `not None` is True, the
        # clean-pass reading. An inconsistent score must not exist at all.
        for flag, payload in self._PAIRS:
            measured = getattr(self, flag)
            value = getattr(self, payload)
            if measured and not (
                isinstance(value, list) and all(isinstance(v, str) for v in value)
            ):
                raise TraceScoreError(
                    f"{flag} is True but {payload} is not a list of str: {value!r}"
                )
            if not measured and value is not None:
                raise TraceScoreError(
                    f"{flag} is False but {payload} is {value!r}; unmeasured must be None"
                )

    def _read(self, flag: str, payload: str) -> list[str]:
        # Raising is the only mechanism a consumer cannot accidentally read the
        # wrong way: an unmeasured metric and a measured-but-empty one are both
        # falsy, so returning `[]` would let `if not score.X` report a clean
        # pass off a measurement that never happened.
        if not getattr(self, flag):
            raise TraceScoreError(
                f"{payload.lstrip('_')} was not measured ({flag} is False): session A's "
                "catalogue for it was empty, so there was nothing to redo. Check the "
                "flag first; an empty list is not a clean pass."
            )
        return getattr(self, payload)

    @property
    def redone_commands(self) -> list[str]:
        return self._read("commands_measured", "_redone_commands")

    @property
    def redone_files(self) -> list[str]:
        return self._read("files_measured", "_redone_files")

    @property
    def repeated_dead_ends(self) -> list[str]:
        return self._read("dead_ends_measured", "_repeated_dead_ends")


def _clean_dead_ends(dead_ends: list[str] | None) -> set[str]:
    """A bare string would iterate into characters and a blank entry would
    contribute nothing, yet both would set `dead_ends_measured=True`. A junk
    catalogue reading as measured is D71 inverted, so reject rather than score."""
    if dead_ends is None:
        return set()
    if isinstance(dead_ends, str) or not isinstance(dead_ends, (list, tuple)):
        raise TraceScoreError(f"dead_ends must be a list of str, got {type(dead_ends).__name__}")
    out = set()
    for d in dead_ends:
        if not isinstance(d, str):
            raise TraceScoreError(f"dead_ends entries must be str, got {d!r}")
        n = _normalize(d)
        if not n:
            raise TraceScoreError(f"dead_ends entries must not be blank, got {d!r}")
        out.add(n)
    return out


def score_trace(
    session_a: Transcript,
    session_b: Transcript,
    dead_ends: list[str] | None = None,
) -> TraceScore:
    """Exact match after whitespace normalisation - `npm test` is not
    `npm test:watch` (plan:1036). Commands and files stay in separate lists
    (D70): a repeated shell command and a repeated file read are different
    behaviours and Task 9 reports them apart.

    A category is measured only if session A actually contributed something to
    match against; otherwise reading it raises rather than returning a zero
    nothing earned. `dead_ends` is a catalogue of things session A already
    established do not work - `None` or `[]` means unmeasured, anything
    malformed is rejected.
    """
    a_cmds = {v for k, v in _b_sequence(session_a.tool_calls) if k == "command"}
    a_files = {v for k, v in _b_sequence(session_a.tool_calls) if k == "file"}
    b_seq = _b_sequence(session_b.tool_calls)
    b_cmds = [v for k, v in b_seq if k == "command"]
    b_files = [v for k, v in b_seq if k == "file"]

    dead = _clean_dead_ends(dead_ends)
    return TraceScore(
        commands_measured=bool(a_cmds),
        files_measured=bool(a_files),
        dead_ends_measured=bool(dead),
        _redone_commands=_first_seen(b_cmds, a_cmds) if a_cmds else None,
        _redone_files=_first_seen(b_files, a_files) if a_files else None,
        # A dead end can be a command or a file, so match against both, in
        # session B's chronological order.
        _repeated_dead_ends=_first_seen([v for _, v in b_seq], dead) if dead else None,
    )
