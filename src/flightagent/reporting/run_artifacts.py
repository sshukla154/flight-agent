"""Per-run artifact directory layout (T45).

D15 fixes exactly two output paths for this project's whole run:
``out/flight_report_2027-07-17.md`` and ``out/flight_results_2027-07-17.json``
(see ``reporting.writer``). That is correct and untouched here — it is the
CLI's default, spec-literal behaviour and every existing regression test
depends on it staying exactly as it is.

What D15's fixed paths cannot do is make one SPECIFIC run's own artifacts
individually addressable: every run overwrites the same two files, so
there is no way to ask "show me run X's report" once a second run has
happened. That is needed by the future FastAPI app (``GET
/runs/{id}/report.md``, master plan §3/§7's Phase 7 goal) — a run must be
able to hand back its own report by ``run_id`` regardless of how many other
runs happened before or after it.

This module adds that layout ALONGSIDE the D15 fixed paths, never instead
of them: ``{runs_dir}/{run_id}/report.md`` and
``{runs_dir}/{run_id}/results.json``. ``run_id`` is whatever the caller is
already using for this run — ``domain.ids.generate_run_id()`` /
``domain.run.RunMeta.run_id`` — this module does not generate or validate
one itself, it only places already-rendered content under a directory
named after it.

D15's fixed paths are the PRIMARY, canonical artifacts (the ones the
existing CLI regression tests read, and the ones a spec-literal caller
expects at a fixed location); the per-run copy is a secondary, additional
view over the identical content, keyed for individual addressability. Two
separate CLI invocations of the same deterministic request therefore
produce byte-identical D15 artifacts AND byte-identical per-run
``report.md``/``results.json`` CONTENT, but under two different
``{runs_dir}/{run_id}/`` directories, because ``run_id`` itself is a fresh
identifier per run (``domain.ids.generate_run_id()`` is UUID4) even though
nothing else about the run's output is allowed to vary (D15, finding 0.3).

Reuses ``reporting.writer``'s atomic-write primitives (temp file + fsync +
``os.replace``) directly rather than a second, less careful serialization
path — the per-run copy gets the exact same crash-safety guarantee the
D15 artifacts already have, for free.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from flightagent.reporting.writer import atomic_write_json, atomic_write_text


class InvalidRunIdError(ValueError):
    """``run_id`` is not the shape ``domain.ids.generate_run_id()`` produces.

    SECURITY, not a data-quality nicety: every one of this module's
    three call sites (``run_artifact_paths`` here, plus
    ``api.routes_runs.run_meta_path`` and ``api.routes_approval.approval_path``,
    both of which call this function rather than re-deriving the
    ``{runs_dir}/{run_id}/`` join independently) feeds ``run_id`` straight
    into a filesystem path join. ``run_id`` reaches this function directly
    from a FastAPI PATH PARAMETER on three routes (``GET /runs/{id}``,
    ``GET /runs/{id}/report.md``, ``POST /runs/{id}/approve``) — i.e. from
    the network, unsanitized, before this check existed. An adversarial
    audit confirmed this was genuinely exploitable: Starlette's router
    blocks multi-segment slash-encoded traversal (``..%2f..%2f``) but NOT
    a backslash-encoded single segment (``..%5csecretdir``), which stays
    inside one ``{run_id}`` path segment past the router and is then
    honoured as a directory separator by Windows ``pathlib`` during the
    actual join below — giving arbitrary-directory read (``report.md``/
    ``run_meta.json``) and write (``approval.json``) on any Windows
    deployment. Validating that ``run_id`` is exactly a UUID4 — the only
    shape this codebase ever generates one in — makes every character a
    path separator could hide (``.``, ``/``, ``\\``, ``%``) categorically
    impossible to reach the join, rather than attempting to enumerate and
    block specific encodings, which is exactly the game this bug class
    already won once.
    """


def _validate_run_id(run_id: str) -> None:
    try:
        uuid.UUID(run_id)
    except ValueError as exc:
        raise InvalidRunIdError(
            f"run_id {run_id!r} is not a valid UUID4 — refusing to build a filesystem path "
            f"from it (see InvalidRunIdError's docstring)"
        ) from exc


DEFAULT_RUNS_DIR = Path("data") / "runs"
"""Matches ``config/defaults.toml``'s ``[output] runs_dir`` default —
mirrors ``reporting.writer.DEFAULT_OUT_DIR``'s own role as the fallback a
caller uses when it has no loaded ``FlightAgentSettings`` to hand. Real
callers should pass ``Path(settings.output.runs_dir)`` explicitly, exactly
as they already do for ``report_path``/``results_path``."""

RUN_REPORT_FILENAME = "report.md"
RUN_RESULTS_FILENAME = "results.json"
"""Deliberately NOT the D15 filenames (``flight_report_2027-07-17.md`` /
``flight_results_2027-07-17.json``) — those names are spec-literal and
belong only to the fixed-path artifacts. A per-run directory is already
disambiguated by its own ``run_id`` path segment, so a short, generic
filename inside it is unambiguous and matches the shape the future FastAPI
route (``GET /runs/{id}/report.md``) is expected to serve directly."""


def run_artifact_paths(run_id: str, *, runs_dir: Path = DEFAULT_RUNS_DIR) -> tuple[Path, Path]:
    """The ``(report_path, results_path)`` pair for one run's own artifact
    directory, without writing anything — a future FastAPI route can use
    this to locate a run's files without importing the writer function
    that produces them.
    """
    _validate_run_id(run_id)
    run_dir = runs_dir / run_id
    return run_dir / RUN_REPORT_FILENAME, run_dir / RUN_RESULTS_FILENAME


def write_run_artifacts(
    *,
    run_id: str,
    markdown: str,
    json_data: Mapping[str, Any],
    runs_dir: Path = DEFAULT_RUNS_DIR,
) -> tuple[Path, Path]:
    """Atomically write one run's own report+results copy under
    ``{runs_dir}/{run_id}/``, alongside (never instead of) the D15 fixed-path
    write a caller performs separately via ``reporting.writer.write_report_artifacts``.

    Each file is written independently and atomically via
    ``reporting.writer``'s own primitives — same guarantee as the D15
    artifacts: a reader (or a crash mid-write) never observes a partially
    written ``report.md`` or ``results.json`` here either.

    Returns the same ``(report_path, results_path)`` shape
    ``write_report_artifacts`` returns, so a caller can log or return both
    path pairs uniformly.
    """
    report_path, results_path = run_artifact_paths(run_id, runs_dir=runs_dir)
    atomic_write_text(report_path, markdown)
    atomic_write_json(results_path, json_data)
    return report_path, results_path
