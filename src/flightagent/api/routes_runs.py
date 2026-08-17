"""``GET /runs/{id}``, ``GET /runs/{id}/report.md``, ``GET /healthz`` (T46).

Reads T45's per-run artifact directory layout (``reporting.run_artifacts``)
directly -- never a second, independently-invented path convention. A run
that produced zero valid itineraries never gets a ``report.md``/
``results.json`` at all (D15/finding 0.3's own "writes nothing" contract,
which the API's ``POST /search`` route deliberately preserves rather than
writing a placeholder report for a run with nothing to show) -- this
module's own ``run_meta.json`` file is what still makes such a run
individually addressable and inspectable via ``GET /runs/{id}``, even
though it has no report to serve.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse

from flightagent.api.schemas import HealthResponse, RunSummary
from flightagent.config.models import FlightAgentSettings
from flightagent.reporting.run_artifacts import run_artifact_paths

router = APIRouter()

RUN_META_FILENAME = "run_meta.json"
"""Sits alongside T45's own ``report.md``/``results.json`` inside
``{runs_dir}/{run_id}/`` -- NOT part of T45's own module
(``reporting.run_artifacts``), which only knows about report/results
content and has no notion of "did this run even produce a report". This
file is this API's own minimal record of "this run_id existed and here is
what happened to it", written once by ``routes_search`` at the end of the
pipeline call that created the run, and read here.
"""


def get_settings(request: Request) -> FlightAgentSettings:
    """FastAPI dependency: the ``FlightAgentSettings`` the app loaded once
    at startup (``app.py``'s lifespan), or the settings a test handed
    ``create_app`` directly. Shared by both route modules so there is
    exactly one way any route reaches the effective config.
    """
    settings: FlightAgentSettings = request.app.state.settings
    return settings


def run_meta_path(run_id: str, *, runs_dir: Path) -> Path:
    """The ``run_meta.json`` path for one run -- derived from T45's own
    ``run_artifact_paths`` (same directory as ``report.md``/
    ``results.json``) rather than re-deriving the ``{runs_dir}/{run_id}/``
    convention a second time.
    """
    report_path, _results_path = run_artifact_paths(run_id, runs_dir=runs_dir)
    return report_path.parent / RUN_META_FILENAME


@router.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    """Trivial liveness check -- no dependency on config, disk, or any
    provider. If this process can answer HTTP at all, this returns 200.
    """
    return HealthResponse(status="ok")


@router.get("/runs/{run_id}", response_model=RunSummary)
async def get_run(
    run_id: str, settings: FlightAgentSettings = Depends(get_settings)
) -> RunSummary:
    """This run's own persisted status/metadata.

    404s (never a 500 or an empty 200) when ``run_id`` names no run this
    process has ever recorded -- ``routes_search`` writes
    ``run_meta.json`` for EVERY run it creates, valid-itinerary or not, so
    a missing file here means the id itself is unknown, not that the run
    merely produced no report.
    """
    meta_path = run_meta_path(run_id, runs_dir=Path(settings.output.runs_dir))
    if not meta_path.is_file():
        raise HTTPException(status_code=404, detail=f"run {run_id!r} not found")
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    return RunSummary.model_validate(data)


@router.get("/runs/{run_id}/report.md")
async def get_run_report(
    run_id: str, settings: FlightAgentSettings = Depends(get_settings)
) -> PlainTextResponse:
    """The Markdown report artifact for one run, with a Markdown
    content-type.

    404s (never a 500 or an empty 200) both when ``run_id`` is entirely
    unknown and when it names a real run that produced zero valid
    itineraries (D15's "writes nothing" contract means there is genuinely
    no report file to serve in that case either) -- either way, "no report
    exists for this id" is the honest answer.
    """
    report_path, _results_path = run_artifact_paths(run_id, runs_dir=Path(settings.output.runs_dir))
    if not report_path.is_file():
        raise HTTPException(status_code=404, detail=f"report for run {run_id!r} not found")
    return PlainTextResponse(
        report_path.read_text(encoding="utf-8"), media_type="text/markdown; charset=utf-8"
    )
