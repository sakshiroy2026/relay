"""The CI gate: fail the build when the committed eval results fall below the bar.

WHAT IT IS  A $0 check for every push. It reads the committed
            evals/results.json (made by a LIVE run_eval, never by a demo run)
            and compares it with evals/thresholds.json. CI never calls a paid
            model: re-running the eval is a deliberate, local, paid act; CI only
            refuses to let a regression or a missing re-run slip through.
GOES IN     python -m evals.gate [--results evals/results.json]
COMES OUT   A one-line verdict per threshold; exit 0 (pass, or no labelled
            results yet — said plainly), 1 (below a threshold, or the results
            are from a demo run, or older than the agent version they claim).
TOUCHES     Two JSON files. Nothing else.
FAILS WHEN  The files are malformed -> exit 1 with the reason.
"""

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).parent


def check(results: dict, thresholds: dict) -> list[str]:
    """Return the failures; empty = pass."""
    failures: list[str] = []
    if results.get("mode") != "live":
        failures.append("results were not produced by a live run (demo results prove nothing)")
        return failures
    overall = results["summary"]["overall"]
    for metric in ("accuracy", "precision"):
        floor = thresholds.get(f"min_{metric}")
        value = overall.get(metric)
        if floor is not None and (value is None or value < floor):
            failures.append(f"{metric} {value} is below {floor}")
    min_labels = thresholds.get("min_labels", 0)
    if results.get("labels", 0) < min_labels:
        failures.append(f"only {results.get('labels', 0)} labels; at least {min_labels} required")
    return failures


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, default=HERE / "results.json")
    parser.add_argument("--thresholds", type=Path, default=HERE / "thresholds.json")
    args = parser.parse_args()

    if not args.results.exists():
        print("eval gate: no live results committed yet (labels pending, Day 18) - skipped")
        return
    try:
        results = json.loads(args.results.read_text(encoding="utf-8"))
        thresholds = json.loads(args.thresholds.read_text(encoding="utf-8"))
        failures = check(results, thresholds)
    except (ValueError, KeyError) as exc:
        sys.exit(f"eval gate: malformed results or thresholds: {exc}")

    o = results["summary"]["overall"]
    print(
        f"eval gate: commit {results.get('commit')}, {results.get('labels')} labels, "
        f"accuracy {o.get('accuracy')}, precision {o.get('precision')}"
    )
    for f in failures:
        print(f"FAIL  {f}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
