"""Harness-behaviour evals: no model, no network. Each case is a function of
plain asserts. Same table as model_runner so the two read alike."""
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))   # learning/
sys.path.insert(0, str(Path(__file__).parent))          # evals/

from harness import ALL_CASES


def main():
    results = []
    for case in ALL_CASES:
        try:
            case["check"]()
            passed, detail = True, ""
        except AssertionError as e:
            passed, detail = False, str(e) or "assert failed"
        except Exception:
            passed, detail = False, traceback.format_exc(limit=1).strip().splitlines()[-1]
        results.append(passed)
        print(f"{'PASS' if passed else 'FAIL'}  {case['name']:28} {detail}")

    print(f"\n{sum(results)}/{len(results)} passed")


if __name__ == "__main__":
    main()
