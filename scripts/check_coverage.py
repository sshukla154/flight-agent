"""Phase 8 coverage gate.

Master plan section 10 ("Coverage"): "overall gate 90% line / 85% branch,
plus a per-module script so a well-covered adapter cannot mask an
under-tested core module. Non-negotiable 100% branch: validator, scorer,
decision_policy, ranker... Orchestrator and retry at 90/85."

Run AFTER a coverage-instrumented pytest run has already produced a
`.coverage` file in the current directory:

    uv run pytest --cov=src/flightagent --cov-branch
    uv run python scripts/check_coverage.py

This script never runs pytest itself and never re-collects coverage data --
it only reads what the pytest run already measured, via `coverage json`
(coverage.py's own documented, stable JSON schema -- not its internal
Python API, which isn't a public contract).

Deliberately computes line% and branch% as two INDEPENDENT percentages
from the raw statement/branch counts, never coverage.py's own blended
`percent_covered` (which folds both into one number once `branch = true`
is set) -- the master plan's 90%/85% are two separate floors, not one
combined number, and conflating them could pass a run that is comfortably
line-covered but branch-thin, or vice versa.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
_COVERAGE_DATA_FILE = _REPO_ROOT / ".coverage"
_COVERAGE_JSON_FILE = _REPO_ROOT / "coverage.json"

_GLOBAL_LINE_FLOOR = 90.0
_GLOBAL_BRANCH_FLOOR = 85.0

_HUNDRED_PERCENT_BRANCH_MODULES = (
    "src/flightagent/validation/engine.py",
    "src/flightagent/scoring/score.py",
    "src/flightagent/policy/direct_vs_stop.py",
    "src/flightagent/scoring/ranking.py",
)
"""The four modules master plan section 10 calls non-negotiable: validator,
scorer, decision_policy, ranker (master-plan names) -- see project_status
memory / this project's own module-mapping research for the confirmed
plan-name -> real-file mapping."""

_EXECUTOR_MODULE = "src/flightagent/orchestration/executor.py"
_EXECUTOR_LINE_FLOOR = 90.0
_EXECUTOR_BRANCH_FLOOR = 85.0
""""Orchestrator and retry" (master plan's names) -- there is no separate
retry.py; retry logic lives inside this same file. Same 90/85 floor as the
global gate, not the four modules' 100% -- master plan section 10's own
carve-out: branch coverage of TaskGroup error paths, semaphore contention,
and cancellation is genuinely hard to reach."""


def _run_coverage_json() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "coverage", "json", "-o", str(_COVERAGE_JSON_FILE)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        raise SystemExit(
            f"`coverage json` failed (exit {result.returncode}) -- "
            "run `pytest --cov=src/flightagent --cov-branch` first."
        )


def _line_pct(summary: dict[str, Any]) -> float:
    num_statements = summary["num_statements"]
    if num_statements == 0:
        return 100.0
    return 100.0 * (num_statements - summary["missing_lines"]) / num_statements


def _branch_pct(summary: dict[str, Any]) -> float:
    num_branches = summary["num_branches"]
    if num_branches == 0:
        return 100.0
    return 100.0 * (num_branches - summary["missing_branches"]) / num_branches


def _find_file(files: dict[str, Any], relative_path: str) -> dict[str, Any] | None:
    if relative_path in files:
        return files[relative_path]
    # Defensive fallback in case relative_files=true is ever reverted and
    # coverage.json's keys come back absolute/backslash-separated.
    normalized_target = relative_path.replace("\\", "/")
    for key, entry in files.items():
        if key.replace("\\", "/").endswith(normalized_target):
            return entry
    return None


def main() -> int:
    if not _COVERAGE_DATA_FILE.is_file():
        print(
            f"FAIL: no {_COVERAGE_DATA_FILE.name} found -- run "
            "`pytest --cov=src/flightagent --cov-branch` first.",
            file=sys.stderr,
        )
        return 1

    _run_coverage_json()
    data = json.loads(_COVERAGE_JSON_FILE.read_text(encoding="utf-8"))
    files: dict[str, Any] = data["files"]

    failures: list[str] = []

    totals = data["totals"]
    global_line_pct = _line_pct(totals)
    global_branch_pct = _branch_pct(totals)
    if global_line_pct >= _GLOBAL_LINE_FLOOR:
        print(f"OK:   global line coverage {global_line_pct:.2f}% >= {_GLOBAL_LINE_FLOOR}%")
    else:
        failures.append(f"global line coverage {global_line_pct:.2f}% < {_GLOBAL_LINE_FLOOR}%")
    if global_branch_pct >= _GLOBAL_BRANCH_FLOOR:
        print(f"OK:   global branch coverage {global_branch_pct:.2f}% >= {_GLOBAL_BRANCH_FLOOR}%")
    else:
        failures.append(
            f"global branch coverage {global_branch_pct:.2f}% < {_GLOBAL_BRANCH_FLOOR}%"
        )

    for module_path in _HUNDRED_PERCENT_BRANCH_MODULES:
        entry = _find_file(files, module_path)
        if entry is None:
            failures.append(f"{module_path}: not found in coverage.json (never imported by tests?)")
            continue
        summary = entry["summary"]
        num_branches = summary["num_branches"]
        missing_branches = summary["missing_branches"]
        if num_branches == 0:
            failures.append(f"{module_path}: 0 branches recorded -- module likely never imported")
            continue
        # Exact integer equality, not a rounded percentage -- see module
        # docstring for why float comparison is the wrong tool here.
        if missing_branches == 0:
            print(f"OK:   {module_path} is 100% branch-covered ({num_branches} branches)")
        else:
            failures.append(
                f"{module_path}: {missing_branches}/{num_branches} branches not covered "
                "(requires exactly 0, non-negotiable per master plan section 10)"
            )

    executor_entry = _find_file(files, _EXECUTOR_MODULE)
    if executor_entry is None:
        failures.append(f"{_EXECUTOR_MODULE}: not found in coverage.json")
    else:
        executor_summary = executor_entry["summary"]
        executor_line_pct = _line_pct(executor_summary)
        executor_branch_pct = _branch_pct(executor_summary)
        if executor_line_pct >= _EXECUTOR_LINE_FLOOR:
            print(f"OK:   {_EXECUTOR_MODULE} line coverage {executor_line_pct:.2f}%")
        else:
            failures.append(
                f"{_EXECUTOR_MODULE}: line coverage {executor_line_pct:.2f}% "
                f"< {_EXECUTOR_LINE_FLOOR}%"
            )
        if executor_branch_pct >= _EXECUTOR_BRANCH_FLOOR:
            print(f"OK:   {_EXECUTOR_MODULE} branch coverage {executor_branch_pct:.2f}%")
        else:
            failures.append(
                f"{_EXECUTOR_MODULE}: branch coverage {executor_branch_pct:.2f}% "
                f"< {_EXECUTOR_BRANCH_FLOOR}%"
            )

    if failures:
        print("\nCoverage gate FAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print("\nCoverage gate PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
