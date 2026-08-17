"""``POST /search`` (T46) -- the HTTP front door onto ``cli.py``'s existing
search pipeline.

Deliberately calls ``cli.build_search_request``,
``cli.run_single_destination_pipeline`` and
``cli.run_all_destinations_pipeline`` -- the SAME functions ``cli.py``'s
own ``run`` command calls -- rather than a second, independently
maintained copy of the normalize/validate/dedup/score/rank logic (T46's
own explicit instruction). Everything CLI-specific (``typer.echo``, exit
codes, writing the D15 FIXED-path artifacts) stays in ``cli.py``; this
route only ever writes the per-run artifact copy (T45), keyed by a fresh
``run_id`` returned to the caller.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from flightagent.api.routes_runs import get_settings, run_meta_path
from flightagent.api.schemas import SearchRequestBody, SearchResponse
from flightagent.cli import (
    _build_provider,
    _to_stop_mode,
    build_search_request,
    run_all_destinations_pipeline,
    run_single_destination_pipeline,
)
from flightagent.config.models import FlightAgentSettings
from flightagent.domain.enums import RunStatus
from flightagent.domain.ids import generate_run_id
from flightagent.providers.errors import ProviderConfigError
from flightagent.reporting.run_artifacts import write_run_artifacts
from flightagent.reporting.writer import atomic_write_json

router = APIRouter()


def _write_run_meta(*, run_id: str, runs_dir: Path, meta: dict[str, Any]) -> None:
    """Persist this run's status/metadata -- reuses ``reporting.writer``'s
    own atomic-write primitive (T15) rather than a second, less careful
    serialization path, exactly the same guarantee T45's own report/results
    copy already gets.
    """
    atomic_write_json(run_meta_path(run_id, runs_dir=runs_dir), meta)


@router.post("/search", response_model=SearchResponse)
async def search(
    body: SearchRequestBody, settings: FlightAgentSettings = Depends(get_settings)
) -> SearchResponse:
    """Trigger the same pipeline ``flightagent run`` uses, keyed to a fresh
    ``run_id``.

    Mirrors ``cli.run``'s own ``--dest``/``--all-destinations``
    mutual-exclusion check (HTTP 422, not a silent guess either way) and
    ``_build_provider``'s D6 provider-name check (also 422 -- a
    ``ProviderConfigError`` is a client-supplied-bad-input problem here,
    not a server error).

    Always writes a ``run_meta.json`` for the run (see
    ``routes_runs.RUN_META_FILENAME``), whether or not any valid
    itinerary was found -- that is what makes ``GET /runs/{run_id}``
    answer for a ``no_results``/``failed`` run instead of only ever
    knowing about runs that produced a report.
    """
    if body.all_destinations and body.dest is not None:
        raise HTTPException(
            status_code=422,
            detail="dest and all_destinations are mutually exclusive -- give exactly one.",
        )
    if not body.all_destinations and body.dest is None:
        raise HTTPException(
            status_code=422, detail="dest is required unless all_destinations is given."
        )

    try:
        provider_instance = _build_provider(body.provider)
    except ProviderConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    stop_mode = _to_stop_mode(body.max_stops)
    runs_dir = Path(settings.output.runs_dir)

    if body.all_destinations:
        result = await run_all_destinations_pipeline(
            origin=body.origin,
            departure_date=body.date,
            provider_instance=provider_instance,
            settings=settings,
        )
        run_id = result.envelope.run_meta.run_id
        status_value = result.envelope.status.value
        report_written = result.markdown is not None and result.json_document is not None
        if result.markdown is not None and result.json_document is not None:
            write_run_artifacts(
                run_id=run_id,
                markdown=result.markdown,
                json_data=result.json_document,
                runs_dir=runs_dir,
            )
        message = (
            f"{status_value} -- {result.accepted_count} valid itinerary(ies) across "
            f"{result.destination_count} destination(s) from {body.origin.upper()} on "
            f"{body.date.isoformat()}"
        )
        _write_run_meta(
            run_id=run_id,
            runs_dir=runs_dir,
            meta={
                "run_id": run_id,
                "status": status_value,
                "accepted_count": result.accepted_count,
                "origin": body.origin.upper(),
                "destination": None,
                "all_destinations": True,
                "generated_at": (
                    result.generated_at.isoformat() if result.generated_at is not None else None
                ),
                "message": message,
                "report_available": report_written,
            },
        )
        return SearchResponse(
            run_id=run_id,
            status=status_value,
            accepted_count=result.accepted_count,
            total_offers=None,
            message=message,
        )

    assert body.dest is not None  # guarded by the mutual-exclusion check above
    request_obj = build_search_request(
        origin=body.origin,
        destination=body.dest,
        departure_date=body.date,
        max_stops=stop_mode,
        settings=settings,
    )
    single_result = await run_single_destination_pipeline(
        request_obj, provider_instance=provider_instance, settings=settings
    )
    run_id = generate_run_id()
    report_written = single_result.markdown is not None and single_result.json_document is not None
    if single_result.markdown is not None and single_result.json_document is not None:
        write_run_artifacts(
            run_id=run_id,
            markdown=single_result.markdown,
            json_data=single_result.json_document,
            runs_dir=runs_dir,
        )
        status_value = RunStatus.COMPLETE.value
    else:
        status_value = RunStatus.NO_RESULTS.value

    message = (
        f"{single_result.accepted_count} valid itinerary(ies) out of "
        f"{single_result.total_offers} offer(s) for "
        f"{request_obj.origin}->{request_obj.destination} on {request_obj.departure_date}"
    )
    _write_run_meta(
        run_id=run_id,
        runs_dir=runs_dir,
        meta={
            "run_id": run_id,
            "status": status_value,
            "accepted_count": single_result.accepted_count,
            "origin": request_obj.origin,
            "destination": request_obj.destination,
            "all_destinations": False,
            "generated_at": single_result.generated_at.isoformat(),
            "message": message,
            "report_available": report_written,
        },
    )
    return SearchResponse(
        run_id=run_id,
        status=status_value,
        accepted_count=single_result.accepted_count,
        total_offers=single_result.total_offers,
        message=message,
    )
