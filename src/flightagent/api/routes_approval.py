"""``POST /runs/{id}/approve`` (T47) -- an honest, audit-only approval
record.

**Scope, deliberately narrower than "approval-and-booking flow."** Master
plan section 8.4 ("Approval gate (§8)") is explicit: the spec's own
gate -- "the agent asks in chat" -- is weak, because the same agent that
asks also decides whether the answer counts. But master plan 8.3/8.4 is
equally explicit that **no booking tool exists anywhere in this codebase**
(section 7's closed tool registry is exactly ``search_flights``,
``airport_info``, ``save_json`` -- see ``providers.base``/the safety
tests) and "there is nothing to gate yet, and that is correct... building a
stub creates an illusion of safety around a tool that doesn't exist."

So this endpoint does exactly what the spec's literal acceptance criterion
requires -- renders the exact prompt string, records a decision -- and
nothing more. It is structurally INCAPABLE of authorizing any real action:

- The request body (``ApprovalRequestBody``) is one boolean and
  ``extra="forbid"`` -- there is no free-text field for a caller (or an
  injected value from anywhere) to carry booking-shaped content through.
- This module imports nothing booking/payment-shaped -- see
  ``tests/unit/test_approval.py``'s AST-based regression guard, which
  parses this file's own imports and fails the build if that ever
  changes.
- The response (``ApprovalResponse``) is a fixed, ``extra="forbid"``
  shape that never contains a confirmation number, ticket reference, or
  payment reference, because there is no code path here that could ever
  produce one.

Master plan 8.4's fuller design -- HMAC capability tokens the agent cannot
mint, fail-closed missing/expired-token handling, an itinerary-snapshot
hash, authenticated (not self-asserted) identity -- is explicitly OUT OF
SCOPE here. That design "gets its own dedicated security review... never
bundled into a routine feature PR" (master plan's own words), and there is
no booking tool yet for a capability token to gate access to. What is
built here is the minimal HONEST version: a plain audit record of
"someone said yes/no to this price, at this time" -- not a security
control, and the response message says so explicitly.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from flightagent.api.routes_runs import get_settings, run_meta_path
from flightagent.api.schemas import ApprovalRequestBody, ApprovalResponse
from flightagent.config.models import FlightAgentSettings
from flightagent.observability.events import EventName
from flightagent.observability.logging import log_event
from flightagent.reporting.run_artifacts import InvalidRunIdError, run_artifact_paths
from flightagent.reporting.writer import atomic_write_json

router = APIRouter()

APPROVAL_FILENAME = "approval.json"
"""Sits alongside T45/T46's own ``report.md``/``results.json``/
``run_meta.json`` inside ``{runs_dir}/{run_id}/`` -- this endpoint's own
minimal audit record, written once per ``POST .../approve`` call (a
second call for the same run overwrites it, recording the latest
decision; there is no history of prior decisions kept, matching this
task's own "minimal honest version, not the full production design"
brief)."""


def approval_path(run_id: str, *, runs_dir: Path) -> Path:
    """The ``approval.json`` path for one run -- derived from
    ``reporting.run_artifacts.run_artifact_paths`` (same directory as
    ``report.md``/``results.json``/``run_meta.json``) rather than
    re-deriving the ``{runs_dir}/{run_id}/`` convention a fourth time.
    """
    report_path, _results_path = run_artifact_paths(run_id, runs_dir=runs_dir)
    return report_path.parent / APPROVAL_FILENAME


def render_approval_prompt(price_eur: str) -> str:
    """The spec's own literal section 8 wording, with the real price
    substituted in: ``"I found a valid itinerary for €X. Do you approve
    proceeding to booking?"``.

    ``price_eur`` is expected to already be the decimal-shaped string
    ``results.json``'s ``top_itineraries[*].price_eur`` carries (e.g.
    ``"451.23"``) -- this function does not reformat or re-round it, it
    only substitutes it into the fixed prompt template.
    """
    return f"I found a valid itinerary for €{price_eur}. Do you approve proceeding to booking?"


@router.post("/runs/{run_id}/approve", response_model=ApprovalResponse)
async def approve_run(
    run_id: str,
    body: ApprovalRequestBody,
    settings: FlightAgentSettings = Depends(get_settings),
) -> ApprovalResponse:
    """Render this run's approval prompt, record the human's decision, and
    confirm the record was written -- nothing else.

    404s (never a 500 or a fabricated success) in two distinct cases:

    - ``run_id`` names no run this process has ever recorded (mirrors
      ``routes_runs.get_run``'s own check -- ``run_meta.json`` is written
      for EVERY run ``routes_search`` creates, so a missing file means
      the id itself is unknown).
    - ``run_id`` is a real, known run that produced zero valid
      itineraries (D15's "writes nothing" contract means ``results.json``
      genuinely does not exist for that run) -- there is no price to
      quote and nothing to approve, so this is refused rather than
      silently approving an empty run or inventing a price.

    On success, writes ``approval.json`` (see ``APPROVAL_FILENAME``)
    reusing ``reporting.writer.atomic_write_json`` -- the same
    temp-file-plus-``fsync``-plus-``os.replace`` guarantee every other
    artifact in this codebase gets, applied here to an audit record
    instead of a report.
    """
    runs_dir = Path(settings.output.runs_dir)

    try:
        meta_path = run_meta_path(run_id, runs_dir=runs_dir)
    except InvalidRunIdError:
        # A malformed (non-UUID4) run_id gets the SAME "not found" 404 as a
        # well-formed-but-unknown one -- see run_artifacts.InvalidRunIdError's
        # docstring for why an unsanitized run_id here was a real,
        # exploitable path-traversal vector (this route's own
        # atomic_write_json call below is the WRITE half of that finding).
        raise HTTPException(status_code=404, detail=f"run {run_id!r} not found") from None
    if not meta_path.is_file():
        raise HTTPException(status_code=404, detail=f"run {run_id!r} not found")

    _report_path, results_path = run_artifact_paths(run_id, runs_dir=runs_dir)
    if not results_path.is_file():
        raise HTTPException(
            status_code=404,
            detail=(
                f"run {run_id!r} produced no itinerary -- nothing to quote a price for or "
                "approve"
            ),
        )

    results: dict[str, Any] = json.loads(results_path.read_text(encoding="utf-8"))
    price_eur: str = results["top_itineraries"][0]["price_eur"]
    prompt = render_approval_prompt(price_eur)

    log_event(EventName.APPROVAL_REQUESTED, reason=prompt)

    recorded_at = datetime.now(UTC).isoformat()
    record = {
        "run_id": run_id,
        "approved": body.approved,
        "price_eur": price_eur,
        "prompt": prompt,
        "recorded_at": recorded_at,
    }
    atomic_write_json(approval_path(run_id, runs_dir=runs_dir), record)

    log_event(
        EventName.APPROVAL_RECORDED,
        decision="approved" if body.approved else "denied",
        reason=prompt,
    )

    decision_word = "approved" if body.approved else "denied"
    message = (
        f"Decision recorded: {decision_word}. This is an audit record only -- no booking "
        "action was taken, because this codebase has no booking capability to authorize "
        "(master plan section 8.4)."
    )

    return ApprovalResponse(
        run_id=run_id,
        approved=body.approved,
        price_eur=price_eur,
        prompt=prompt,
        recorded_at=recorded_at,
        message=message,
    )
