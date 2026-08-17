"""Pydantic request/response models for the API (T46).

Deliberately distinct from the domain models in ``flightagent.domain`` --
these are the API's OWN wire contract, even where a field wraps a domain
value verbatim (e.g. ``SearchResponse.status`` mirrors a
``domain.enums.RunStatus`` value as a plain string). A future change to a
domain model's shape must never silently change what this HTTP contract
promises a caller; keeping the two type hierarchies separate is what
makes that true.
"""

from __future__ import annotations

from datetime import date as _date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SearchRequestBody(BaseModel):
    """``POST /search`` request body.

    Mirrors ``cli.py``'s own ``run`` command parameters exactly (T46's own
    instruction: "the same logical parameters cli.py's run() command
    does") -- ``origin``/``date``/``max_stops`` plus ``dest`` XOR
    ``all_destinations``, and ``provider`` (D6: only ``"mock"`` is wired
    up; anything else 422s, matching the CLI's ``ProviderNotConfigured``
    behaviour translated to an HTTP error).
    """

    model_config = ConfigDict(extra="forbid")

    origin: str = Field(description="Origin IATA airport code, e.g. AMS.")
    date: _date = Field(description="Departure date, ISO format YYYY-MM-DD.")
    max_stops: Literal[0, 1] = Field(
        default=1,
        description="Maximum stops: 0 (direct only) or 1 (at most one stop, D13). Only "
        "applies when dest is given; all_destinations always searches both modes.",
    )
    dest: str | None = Field(
        default=None,
        description="Destination IATA airport code, e.g. DEL. Required unless "
        "all_destinations is true; mutually exclusive with it.",
    )
    all_destinations: bool = Field(
        default=False,
        description="Search origin against all 8 registry destinations at once, instead "
        "of a single dest. Mutually exclusive with dest.",
    )
    provider: str = Field(
        default="mock", description="Provider to search. Only 'mock' is configured (D6)."
    )


class SearchResponse(BaseModel):
    """``POST /search`` response -- a ``run_id`` plus enough of the outcome
    for a caller to decide whether to fetch ``GET /runs/{run_id}/report.md``
    at all (``status`` is one of ``domain.enums.RunStatus``'s values;
    ``no_results``/``failed`` runs write no report, exactly like the CLI).
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: str
    accepted_count: int
    total_offers: int | None = Field(
        default=None, description="Only set for a single-destination (non-all_destinations) run."
    )
    message: str


class RunSummary(BaseModel):
    """``GET /runs/{run_id}`` response -- this run's own persisted metadata
    (``api.routes_runs.RUN_META_FILENAME``), written once by
    ``routes_search`` at the end of the pipeline call that created it.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: str
    accepted_count: int
    origin: str
    destination: str | None
    all_destinations: bool
    generated_at: str | None
    message: str
    report_available: bool


class HealthResponse(BaseModel):
    """``GET /healthz`` response -- a trivial liveness check, nothing more."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"]


class ApprovalRequestBody(BaseModel):
    """``POST /runs/{run_id}/approve`` request body (T47) -- the human's
    decision, and nothing else.

    Deliberately just one boolean field, ``extra="forbid"``: there is no
    free-text field anywhere on this request for an attacker (or an
    over-eager caller) to smuggle content through that later reappears in
    the response or the persisted record -- see
    ``api.routes_approval``'s own module docstring for why that matters
    here specifically.
    """

    model_config = ConfigDict(extra="forbid")

    approved: bool = Field(
        description="True to record approval, False to record denial. This endpoint only "
        "RECORDS the decision -- this codebase has no booking tool/capability for either "
        "value to trigger (master plan section 8.4)."
    )


class ApprovalResponse(BaseModel):
    """``POST /runs/{run_id}/approve`` response (T47) -- confirms the
    decision was recorded, never a booking confirmation (there is no
    booking capability to confirm).
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    approved: bool
    price_eur: str = Field(description="The quoted price from this run's top-ranked itinerary.")
    prompt: str = Field(description="The exact rendered approval-gate prompt shown for this run.")
    recorded_at: str = Field(description="ISO-8601 UTC instant this decision was recorded.")
    message: str
