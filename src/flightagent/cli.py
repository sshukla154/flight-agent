"""``flightagent`` CLI entry point (T16) -- wires T9-T15 into one command.

This is the literal Phase 2 exit criterion: a single ``flightagent run``
invocation that builds a ``SearchRequest`` from CLI flags, calls
``MockProvider.search()`` (T10), normalizes every returned ``RawOffer``
(T11), validates each ``NormalizedItinerary`` (T12), deduplicates the valid
ones by itinerary shape key (T20, finding 0.2), scores the survivors
(T13), ranks them (T14), and writes both v1 report artifacts (T15).

Master plan S1.4's canonical loop order is "validate -> dedup -> score ->
rank" -- T20 inserts the dedup step here, between the existing validate and
score steps, operating only on the itineraries that already passed
validation. Deduping before scoring means a scored/ranked itinerary's
``duplicate_count``/``also_offered_by``/``fare_options`` are already final
by the time scoring sees it, and a codeshare quartet is never scored (and
never occupies four ranked-list slots) as four separate entries.

**Determinism, not wall-clock, for every timestamp this command emits.**
``normalize.builder.build_normalized_itinerary`` requires a caller-supplied
``fare_as_of``, and both ``reporting.markdown.render_markdown_report`` and
``reporting.json_report.build_results_document`` require a caller-supplied
``generated_at`` -- both values are rendered directly into the artifacts
(the JSON's top-level ``generated_at`` key, every itinerary's
``fare_as_of``, and the Markdown summary's "Report generated ..." line).
Master plan S5's own "one subtle determinism trap" is exactly this shape of
bug applied to the mock generator's RNG seed; the identical trap exists
here for wall-clock time. Calling ``datetime.now()`` at this layer would
make two back-to-back runs of the identical command produce two
byte-different artifacts, which the phase's own exit criterion (two runs,
byte-identical output) forbids outright. ``_deterministic_as_of`` below
reuses the mock generator's own request-derived seed (T10's
``compute_seed``) to produce a stable, request-scoped instant instead --
never "now", so identical CLI args always produce identical files. There is
no real "instant this fare was retrieved" to report for synthetic data
anyway (the ``SYNTHETIC DATA`` banner already says so), so a deterministic
stand-in is the honest choice available at this layer, not a compromise on
top of a real one.

D6: only ``--provider mock`` is wired up. Any other provider name raises
``ProviderNotConfigured`` (a ``ProviderConfigError`` subclass, T9's own
taxonomy) rather than silently falling back to mock -- Amadeus/Duffel ship
interface-complete but uncredentialed in Phase 7, and there is no adapter
of either name registered in this codebase yet for Phase 2 to fall back
onto.

Exit code discipline (anticipating, but not building, Phase 4's full
``no_results``/``RunStatus`` contract -- finding 0.5): exit ``0`` only when
at least one itinerary validated, was ranked, and both artifacts were
written. Zero valid itineraries is a nonzero exit and writes NOTHING to
``out/`` -- silently emitting an empty-looking report and claiming success
would be worse than a loud failure. The rejection information collected
while filtering (``ValidationResult.rejections``) is not discarded in that
path; it is summarized (a rejection-code histogram) into the error message
this command prints, so a human immediately sees *why* nothing validated
instead of just *that* nothing did.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Annotated

import typer

from flightagent.config.loader import load_config
from flightagent.domain.enums import CabinClass, RejectionCode, StopMode
from flightagent.domain.itinerary import NormalizedItinerary, RawOffer
from flightagent.domain.run import SearchRequest
from flightagent.domain.scoring import ScoredItinerary
from flightagent.normalize.builder import build_normalized_itinerary
from flightagent.normalize.dedup import deduplicate
from flightagent.providers.base import CallBudget, FlightProvider
from flightagent.providers.errors import ProviderNotConfigured
from flightagent.providers.mock.generator import compute_seed
from flightagent.providers.mock.provider import MockProvider
from flightagent.reporting.json_report import build_results_document
from flightagent.reporting.markdown import render_markdown_report
from flightagent.reporting.writer import write_report_artifacts
from flightagent.scoring.ranking import rank_itineraries
from flightagent.scoring.score import score_itinerary
from flightagent.validation.engine import validate

app = typer.Typer(
    name="flightagent",
    help="Autonomous flight-search agent (Nieuwegein-area -> India, mock provider, Phase 2).",
    add_completion=False,
    no_args_is_help=True,
)

NO_VALID_ITINERARIES_EXIT_CODE = 1
"""Exit code when zero itineraries survived validation. A placeholder
single value, not Phase 4's full ``RunStatus``-derived exit-code table
(finding 0.5) -- this task only has to get "zero valid == nonzero exit"
right, not the full NO_RESULTS-vs-FAILED distinction."""

_DEFAULT_ADULTS = 1
"""D3: 1 adult, 0 children, 0 infants -- not a CLI flag, the spec takes no
passenger count as input."""

_DEFAULT_CABIN = CabinClass.ECONOMY
"""D3 scopes this project to economy only -- not a CLI flag."""

_DEFAULT_CURRENCY = "EUR"
"""Every itinerary's ``price_eur`` is already the report's native currency
(D14); not a CLI flag in Phase 2."""


@app.callback()
def _callback() -> None:
    """Autonomous flight-search agent (Nieuwegein-area -> India, Phase 2: mock provider only).

    An explicit (empty) Typer callback -- with only one subcommand
    registered (``run``), Typer would otherwise collapse this app so that
    subcommand's name is optional (``flightagent --origin ...`` would work
    without ``run``). Registering ANY callback forces Typer to keep
    ``run`` as a required, named subcommand, matching the exact target
    invocation shape (``flightagent run --origin ...``) this phase's exit
    criterion specifies verbatim.
    """


def _to_stop_mode(value: int) -> StopMode:
    """Narrow a plain ``int`` (already range-checked 0..1 by the
    ``--max-stops`` option's own ``min=0, max=1``) to the ``StopMode``
    literal type ``SearchRequest.max_stops`` requires.

    A second, explicit check here rather than trusting the CLI-level range
    check alone: this is a plain function a future non-Typer caller could
    invoke directly, bypassing Click's own validation entirely.
    """
    if value == 0:
        return 0
    if value == 1:
        return 1
    raise typer.BadParameter("--max-stops must be 0 or 1 (D13: at most one stop)")


def _parse_departure_date(value: str) -> date:
    """Parse ``--date`` as a strict ISO ``YYYY-MM-DD`` date.

    Raises ``typer.BadParameter`` (a clean CLI usage error, exit code 2)
    rather than letting a malformed string reach ``SearchRequest``'s own
    pydantic validation, whose error would be a much less legible
    traceback for what is, at this layer, simple CLI input.
    """
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(
            f"--date must be an ISO YYYY-MM-DD date, got {value!r}"
        ) from exc


def _build_provider(name: str) -> FlightProvider:
    """Resolve ``--provider`` to a ``FlightProvider`` instance.

    D6: only ``"mock"`` ships working in Phase 2. Anything else raises
    ``ProviderNotConfigured`` -- a ``ProviderConfigError`` subclass (T9's
    error taxonomy) -- naming exactly D6's own scenario (Amadeus/Duffel
    ship interface-complete but uncredentialed) even though neither
    adapter class exists in this codebase yet to actually import. This
    must never silently fall back to ``MockProvider`` for an unrecognised
    name -- that would make a typo'd ``--provider`` name look like it
    searched a real provider when it did not.
    """
    if name == "mock":
        return MockProvider()
    raise ProviderNotConfigured(
        f"provider {name!r} is not configured -- Phase 2 ships mock-only (D6); "
        f"only '--provider mock' is wired up. Real adapters (Amadeus, Duffel) land in "
        f"Phase 7 and will raise this same error at runtime until credentials are supplied.",
        provider=name,
    )


_AS_OF_LOOKBACK = timedelta(days=90)
"""Width of the deterministic window ``_deterministic_as_of`` places its
result in -- always strictly before the requested departure date, never
after. Only affects how plausible the reported "fare retrieved" instant
reads; the determinism property does not depend on this specific width."""


def _deterministic_as_of(request: SearchRequest) -> datetime:
    """A deterministic, request-scoped UTC instant -- reused for both
    ``fare_as_of`` (T11) and ``generated_at`` (T15) so that two runs of an
    identical CLI invocation produce byte-identical artifacts.

    Reuses T10's own ``compute_seed`` (a sha256 over the request's
    canonical search fields) rather than re-deriving a second hash formula
    -- one seed function for "this request, deterministically" is
    plenty, and reusing it here means this value changes exactly when the
    mock generator's own offers would, never independently. See this
    module's docstring for why "now" is wrong at this layer.

    The result always falls somewhere in the ``_AS_OF_LOOKBACK`` window
    immediately before midnight UTC on ``request.departure_date`` -- a
    "fare last checked shortly before departure" instant is what a real
    caller would report, so a deterministic stand-in should land somewhere
    plausible too, not on an arbitrary epoch offset that could land decades
    away from the trip it is dated against.
    """
    seed = compute_seed(request)
    offset_seconds = seed % int(_AS_OF_LOOKBACK.total_seconds())
    window_start = datetime.combine(request.departure_date, time.min, tzinfo=UTC) - _AS_OF_LOOKBACK
    return window_start + timedelta(seconds=offset_seconds)


def _normalize_and_validate(
    request: SearchRequest,
    raw_offers: tuple[RawOffer, ...],
    *,
    as_of: datetime,
) -> tuple[list[NormalizedItinerary], Counter[RejectionCode]]:
    """Run every ``RawOffer`` through T11 (normalize) then T12 (validate).

    Returns ``(valid_itineraries, rejection_counts)`` -- the rejection
    histogram is never discarded even when it ends up empty, so the
    caller can always explain a zero-valid outcome.
    """
    valid_itineraries: list[NormalizedItinerary] = []
    rejection_counts: Counter[RejectionCode] = Counter()
    for raw_offer in raw_offers:
        itinerary = build_normalized_itinerary(
            raw_offer,
            adults=request.adults,
            cabin=request.cabin,
            fare_as_of=as_of,
        )
        validation_result = validate(itinerary, request)
        if validation_result.is_valid:
            valid_itineraries.append(itinerary)
        else:
            rejection_counts.update(rejection.code for rejection in validation_result.rejections)
    return valid_itineraries, rejection_counts


@app.command()
def run(
    origin: Annotated[
        str, typer.Option("--origin", help="Origin IATA airport code, e.g. AMS.")
    ],
    dest: Annotated[
        str, typer.Option("--dest", help="Destination IATA airport code, e.g. DEL.")
    ],
    date_str: Annotated[
        str, typer.Option("--date", help="Departure date, ISO format YYYY-MM-DD.")
    ],
    max_stops: Annotated[
        int,
        typer.Option(
            "--max-stops",
            min=0,
            max=1,
            help="Maximum stops: 0 (direct only) or 1 (at most one stop, D13).",
        ),
    ],
    provider: Annotated[
        str,
        typer.Option("--provider", help="Provider to search. Only 'mock' works in Phase 2."),
    ] = "mock",
) -> None:
    """Search, validate, dedup, score, rank, and report one origin/destination pair.

    Pipeline (in order): build a ``SearchRequest`` -> ``MockProvider.search()``
    (T10) -> normalize every offer (T11) -> validate every itinerary,
    keeping only the valid ones (T12) -> deduplicate by itinerary shape key
    (T20) -> score (T13) -> rank (T14) -> write both artifacts (T15). Exits
    ``0`` only if at least one itinerary validated and both artifacts were
    written; exits ``NO_VALID_ITINERARIES_EXIT_CODE`` if zero itineraries
    validated, writing nothing to ``out/``. Dedup runs on the
    already-valid set and can only ever reduce or preserve its size, never
    turn a zero-valid outcome into a non-zero one or vice versa.
    """
    # Resolve the provider FIRST -- a bad --provider value must fail before
    # any config is loaded or any (deterministic, harmless) mock work runs.
    provider_instance = _build_provider(provider)

    settings = load_config()
    departure_date = _parse_departure_date(date_str)
    stop_mode = _to_stop_mode(max_stops)

    request = SearchRequest(
        origin=origin.upper(),
        destination=dest.upper(),
        departure_date=departure_date,
        cabin=_DEFAULT_CABIN,
        max_stops=stop_mode,
        adults=_DEFAULT_ADULTS,
        currency=_DEFAULT_CURRENCY,
        layover_min=timedelta(minutes=settings.layover.layover_min_minutes),
        layover_max=timedelta(minutes=settings.layover.layover_max_minutes),
    )

    as_of = _deterministic_as_of(request)

    search_result = asyncio.run(provider_instance.search(request, CallBudget()))
    total_offers = len(search_result.offers)

    valid_itineraries, rejection_counts = _normalize_and_validate(
        request, search_result.offers, as_of=as_of
    )

    if not valid_itineraries:
        if rejection_counts:
            breakdown = ", ".join(
                f"{code.value}={count}"
                for code, count in sorted(
                    rejection_counts.items(), key=lambda item: item[0].value
                )
            )
        else:
            breakdown = "provider returned no offers"
        typer.echo(
            f"flightagent: 0 valid itineraries out of {total_offers} offer(s) for "
            f"{request.origin}->{request.destination} on {request.departure_date} "
            f"-- rejections: {breakdown}. No report written.",
            err=True,
        )
        raise typer.Exit(code=NO_VALID_ITINERARIES_EXIT_CODE)

    deduplicated_itineraries = deduplicate(valid_itineraries)

    scored_itineraries = [
        ScoredItinerary(
            itinerary=itinerary,
            components=score_itinerary(
                itinerary, scoring_settings=settings.scoring, layover_settings=settings.layover
            ),
            rank_by_adjusted_score=1,
            rank_by_total_journey_score=1,
            rank_by_price=1,
        )
        for itinerary in deduplicated_itineraries
    ]

    ranked = rank_itineraries(scored_itineraries, top_n=settings.output.top_n_global)
    accepted_count = len(scored_itineraries)

    markdown = render_markdown_report(
        ranked,
        departure_date=request.departure_date,
        accepted_count=accepted_count,
        generated_at=as_of,
        data_source="mock",
    )
    json_document = build_results_document(
        ranked,
        departure_date=request.departure_date,
        accepted_count=accepted_count,
        top_n=settings.output.top_n_global,
        generated_at=as_of,
        data_source="mock",
    )

    report_path, results_path = write_report_artifacts(
        markdown=markdown,
        json_data=json_document,
        report_path=Path(settings.output.report_path),
        results_path=Path(settings.output.results_path),
    )

    typer.echo(
        f"flightagent: {accepted_count} valid itinerary(ies) out of {total_offers} offer(s) "
        f"for {request.origin}->{request.destination} on {request.departure_date}; "
        f"wrote {report_path} and {results_path}"
    )


def main() -> None:
    """Console-script / ``python -m flightagent`` entry point."""
    app()


if __name__ == "__main__":
    main()
