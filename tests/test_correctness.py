from membench.scoring.correctness import score_correctness
from membench.corpus.extract import BenchTask


def _task() -> BenchTask:
    return BenchTask(
        task_id="liquid-202", repo="jg-rp/liquid", issue_number=202,
        issue_title="t", issue_body="b", base_sha="a" * 40, fix_sha="b" * 40,
        changed_files=["liquid/builtin/tags/cycle_tag.py"],
        fail_to_pass=["tests/test_cycle.py::test_empty_group"],
        pass_to_pass=["tests/test_cycle.py::test_basic"],
    )


def test_solved_requires_all_f2p_and_no_p2p_regression():
    s = score_correctness(
        {"tests/test_cycle.py::test_empty_group": True, "tests/test_cycle.py::test_basic": True},
        _task(),
    )
    assert s.solved is True
    assert s.p2p_broken == []


def test_regression_blocks_solved():
    s = score_correctness(
        {"tests/test_cycle.py::test_empty_group": True, "tests/test_cycle.py::test_basic": False},
        _task(),
    )
    assert s.solved is False
    assert s.p2p_broken == ["tests/test_cycle.py::test_basic"]


def test_missing_node_id_blocks_solved_and_is_not_silently_absent():
    # D31: an F2P id absent from `results` entirely (uncollectible) must not
    # look identical to "ran and failed" - it must be named on the score.
    s = score_correctness({"tests/test_cycle.py::test_basic": True}, _task())
    assert s.solved is False
    assert s.missing == ["tests/test_cycle.py::test_empty_group"]
    assert s.p2p_broken == []  # present and True - not a regression
