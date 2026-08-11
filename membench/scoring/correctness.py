from dataclasses import dataclass, field

from membench.corpus.extract import BenchTask


@dataclass
class CorrectnessScore:
    f2p_passed: int
    f2p_total: int
    p2p_broken: list[str] = field(default_factory=list)
    # D31: fail_to_pass/pass_to_pass ids that never appear in `results` at
    # all (as opposed to appearing with outcome False). run_tests raises
    # before returning if it was given these ids directly and one is
    # uncollectible, but `results` may also arrive here already filtered
    # (e.g. from a full-suite run) - this keeps that case detectable too,
    # rather than indistinguishable from a normal, ran-and-failed regression.
    missing: list[str] = field(default_factory=list)

    @property
    def solved(self) -> bool:
        return (
            self.f2p_total > 0
            and self.f2p_passed == self.f2p_total
            and not self.p2p_broken
            and not self.missing
        )


def score_correctness(results: dict[str, bool], task: BenchTask) -> CorrectnessScore:
    all_ids = task.fail_to_pass + task.pass_to_pass
    missing = [n for n in all_ids if n not in results]
    f2p_passed = sum(1 for n in task.fail_to_pass if results.get(n) is True)
    p2p_broken = [n for n in task.pass_to_pass if results.get(n) is False]
    return CorrectnessScore(
        f2p_passed=f2p_passed,
        f2p_total=len(task.fail_to_pass),
        p2p_broken=p2p_broken,
        missing=missing,
    )
