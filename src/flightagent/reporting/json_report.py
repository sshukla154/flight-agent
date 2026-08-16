"""JSON results-artifact builder v1 (T15).

Master plan S8.6 (CRITICAL): mock output must carry ``"data_source": "mock"``
as a **structural top-level field** -- not buried in a nested object, not
optional. ``build_results_document`` puts it there directly.

Scope note, per this task's brief: this is deliberately NOT the full
``domain.run.RunEnvelope``/``RunMeta`` machinery -- that requires a
``config_digest`` and a ``tzdata_version``, neither of which any
orchestration layer produces in this phase. This is a smaller, honest
subset: ``data_source``, ``departure_date``, and the ranked itineraries
with their score components, shaped closely enough to what master plan
section 4 eventually wants (``top_itineraries``, ``accepted_count``) that
migrating into a real ``RunEnvelope`` later is an additive move, not a
rewrite. ``config/results.schema.json`` validates exactly this shape --
see that file for the field-level contract.

Phase 4 (T26) adds ``TaskOutcome``-derived data: ``build_results_document``
now optionally accepts the run's task ledger and always emits a top-level
``failed_searches`` array (destination/error_type/error_detail per
error-state task) -- empty, never omitted, when nothing failed.

All money and score values are emitted as **decimal-shaped strings**, never
JSON numbers -- JSON has no decimal type, and round-tripping a ``Decimal``
through a JSON float would reintroduce exactly the float-nondeterminism
finding 0.3 exists to eliminate from this codebase's money/score
arithmetic.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

from flightagent.domain.itinerary import NormalizedItinerary
from flightagent.domain.run import TaskOutcome
from flightagent.domain.scoring import ScoreComponents, ScoredItinerary
from flightagent.reporting.booking_link import BookingUrlRejected, DataSource, validate_booking_url
from flightagent.reporting.view import (
    airline_string,
    destination_from_task_id,
    failed_task_outcomes,
    first_segment,
    last_segment,
    route_string,
    total_duration_minutes,
    total_layover_minutes,
)

SCHEMA_VERSION = "1.0"


def _score_to_json(components: ScoreComponents) -> dict[str, str]:
    return {
        "fare_eur": str(components.fare_eur),
        "elapsed_time_component": str(components.elapsed_time_component),
        "layover_penalty": str(components.layover_penalty),
        "direct_bonus": str(components.direct_bonus),
        "score": str(components.score),
        "adjusted_score": str(components.adjusted_score),
    }


def _booking_json(
    itinerary: NormalizedItinerary, *, data_source: DataSource
) -> tuple[str | None, bool]:
    """``(booking_url, booking_url_valid)`` for the JSON artifact.

    Unlike the Markdown renderer (which never emits a raw URL that failed
    validation), the JSON artifact keeps the ORIGINAL url string even when
    it fails S8.2's checks -- JSON is not itself a clickable rendering
    surface, so preserving the raw value is a debugging/audit aid, not a
    render-time hazard. ``booking_url_valid`` is the structural signal a
    downstream consumer must check before treating this field as safe to
    present as a link.
    """
    if itinerary.booking_url is None:
        return None, False
    url_str = str(itinerary.booking_url)
    try:
        validate_booking_url(url_str, data_source=data_source)
        return url_str, True
    except BookingUrlRejected:
        return url_str, False


def _failed_search_to_json(outcome: TaskOutcome) -> dict[str, Any]:
    """One "Failed Searches" entry (Phase 4, T26) -- destination, error
    class, and detail for one error-state ``TaskOutcome``. Matches the
    naming convention ``build_results_document`` already uses for its
    other top-level fields (plain snake_case, no nesting for a value this
    small).
    """
    return {
        "destination": destination_from_task_id(outcome.task_id),
        "error_type": outcome.error_type,
        "error_detail": outcome.error_detail,
    }


def _itinerary_to_json(item: ScoredItinerary, *, data_source: DataSource) -> dict[str, Any]:
    itinerary = item.itinerary
    departure_segment = first_segment(itinerary)
    arrival_segment = last_segment(itinerary)
    booking_url, booking_url_valid = _booking_json(itinerary, data_source=data_source)

    return {
        "rank_by_adjusted_score": item.rank_by_adjusted_score,
        "rank_by_price": item.rank_by_price,
        "rank_by_total_journey_score": item.rank_by_total_journey_score,
        "itinerary_id": itinerary.itinerary_id,
        "provider": itinerary.provider,
        "airline": airline_string(itinerary),
        "route": route_string(itinerary),
        "origin": departure_segment.origin,
        "destination": arrival_segment.destination,
        "stop_count": itinerary.stop_count,
        "departure_utc": departure_segment.depart_utc.isoformat(),
        "departure_local": departure_segment.depart_local.isoformat(),
        "departure_tz": departure_segment.origin_tz,
        "arrival_utc": arrival_segment.arrive_utc.isoformat(),
        "arrival_local": arrival_segment.arrive_local.isoformat(),
        "arrival_tz": arrival_segment.destination_tz,
        "layover_minutes": total_layover_minutes(itinerary),
        "total_duration_minutes": total_duration_minutes(itinerary),
        "price_eur": f"{itinerary.price_eur.amount:.2f}",
        "fare_as_of": itinerary.fare_as_of.isoformat(),
        "booking_url": booking_url,
        "booking_url_kind": itinerary.booking_url_kind,
        "booking_url_valid": booking_url_valid,
        "score": _score_to_json(item.components),
    }


def build_results_document(
    ranked: Sequence[ScoredItinerary],
    *,
    departure_date: date,
    accepted_count: int,
    top_n: int,
    generated_at: datetime,
    data_source: DataSource = "mock",
    task_outcomes: Sequence[TaskOutcome] = (),
) -> dict[str, Any]:
    """Build the v1 JSON results document (not yet written to disk -- see
    ``reporting.writer``).

    ``ranked`` is the already-ranked, already-top-N-truncated list (D15);
    it becomes ``top_itineraries`` verbatim, in the order given. ``top_n``
    records the truncation limit that produced it (D15's "global top 10"),
    and ``accepted_count`` is the full valid-itinerary count *before*
    truncation -- D15 requires truncation to apply to presentation only,
    so this document keeps both numbers rather than only the truncated
    one.

    ``task_outcomes`` is the run's full task ledger (T26, Phase 4) -- every
    ``TaskOutcome``, successful or not. This function filters it down to
    the error-state subset itself (``reporting.view.failed_task_outcomes``)
    and emits it, one entry per failed destination, as the top-level
    ``failed_searches`` array -- always present, empty when nothing failed
    (including when ``task_outcomes`` is left at its default), never
    omitted, so a consumer never has to distinguish a missing key from an
    empty list.

    Raises ``ValueError`` if ``accepted_count`` is smaller than
    ``len(ranked)`` -- truncation can only ever shrink what is *shown*,
    never invent itineraries that were not actually accepted.
    """
    if accepted_count < len(ranked):
        raise ValueError(
            f"accepted_count ({accepted_count}) cannot be smaller than the number of "
            f"ranked itineraries supplied ({len(ranked)}) -- truncation only shrinks the "
            f"presented list, never the count it was truncated from"
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "data_source": data_source,
        "departure_date": departure_date.isoformat(),
        "generated_at": generated_at.isoformat(),
        "accepted_count": accepted_count,
        "top_n": top_n,
        "top_itineraries": [_itinerary_to_json(item, data_source=data_source) for item in ranked],
        "failed_searches": [
            _failed_search_to_json(outcome) for outcome in failed_task_outcomes(task_outcomes)
        ],
    }
