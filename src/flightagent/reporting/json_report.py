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

Phase 5 (T33) adds the direct-vs-stop policy output: ``build_results_document``
now optionally accepts a sequence of ``DestinationAnalysis`` (D10) and
always emits a top-level ``destination_analyses`` array -- empty, never
omitted, when none were computed (the single-``--dest`` pipeline never
computes this at all, since D10's comparison is inherently a
per-destination, both-modes-searched thing).

Phase 6 (T39) adds the early-stop replay annotation: ``build_results_document``
now optionally accepts a ``{destination: EarlyStopEvaluation}`` mapping
(``orchestration.waves.replay_early_stop``, D12) and always emits a
top-level ``early_stop_analysis`` array -- empty, never omitted, when none
were computed. Purely additive/informational: computing it never changes
``top_itineraries`` or ``accepted_count``.

Phase 6 (T41) adds three more top-level arrays/objects, each following the
identical "always present, empty when nothing was computed" convention
every field above already sets:

- ``origin_comparison``: one entry per ``OriginSummary`` (T40, master plan
  A2-5c) -- an origin with no accepted itinerary still gets an entry, with
  ``cheapest_fare_eur``/``destination`` null and ``reason`` naming why.
- ``self_transfer_rejections``: one entry per ``RejectedItinerary`` (D5) --
  every itinerary excluded specifically for a ``RejectionCode.SELF_TRANSFER``
  rejection, never present in ``top_itineraries``.
- ``top_itineraries_by_destination``: D15's "top 3 per destination
  additionally in the JSON" (D15's own wording scopes this to the JSON
  artifact only -- ``reporting.markdown`` does not render it). Keyed by
  destination IATA code, sorted alphabetically for determinism (finding
  0.3) rather than left in whatever order the input/grouping happened to
  produce; each value is a list of the SAME per-itinerary shape
  ``top_itineraries`` entries use.

All money and score values are emitted as **decimal-shaped strings**, never
JSON numbers -- JSON has no decimal type, and round-tripping a ``Decimal``
through a JSON float would reintroduce exactly the float-nondeterminism
finding 0.3 exists to eliminate from this codebase's money/score
arithmetic. ``destination_analyses`` entries follow the identical
convention for ``price_eur``/``price_difference_eur``/``relative_difference``,
and ``time_saved_minutes`` is a plain (possibly negative) integer, matching
this file's own ``layover_minutes``/``total_duration_minutes`` convention
(whole minutes, exact ``timedelta`` floor-division) rather than seconds or
an ISO-8601 duration string.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from types import MappingProxyType
from typing import Any

from flightagent.domain.itinerary import NormalizedItinerary
from flightagent.domain.policy import DestinationAnalysis, EarlyStopEvaluation
from flightagent.domain.run import TaskOutcome
from flightagent.domain.scoring import OriginSummary, ScoreComponents, ScoredItinerary
from flightagent.domain.validation import RejectedItinerary
from flightagent.reporting.booking_link import BookingUrlRejected, DataSource, validate_booking_url
from flightagent.reporting.view import (
    airline_string,
    destination_from_task_id,
    direct_tier_recommendation_label,
    failed_task_outcomes,
    first_segment,
    last_segment,
    origin_absence_reason,
    route_string,
    self_transfer_airports,
    total_duration_minutes,
    total_layover_minutes,
)

_ONE_MINUTE = timedelta(minutes=1)

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


def _destination_analysis_to_json(analysis: DestinationAnalysis) -> dict[str, Any]:
    """One "Direct Flight Analysis" entry (Phase 5, T33, D10) -- the same
    fields the Markdown table's row shows (destination, airline, price,
    price difference, recommendation) plus the underlying numbers a
    consumer would need to recompute or double-check the tier
    (``relative_difference``, ``tier``/``tier_reason``, ``time_saved_minutes``,
    the divergence flag/explanation).

    ``airline``/``price_eur`` are ``None`` exactly when ``cheapest_direct``
    is ``None`` (``DirectTier.NOT_AVAILABLE`` -- ``domain.policy.DestinationAnalysis``'s
    own validator forbids ``cheapest_direct`` being present any other time
    that tier is set). ``price_difference_eur``/``relative_difference`` are
    ``None`` in that same case AND in the degenerate "direct exists, no
    valid one-stop alternative" case (T31) -- there is nothing priced to
    compare against either way.
    """
    if analysis.cheapest_direct is not None:
        airline: str | None = airline_string(analysis.cheapest_direct)
        price_eur: str | None = f"{analysis.cheapest_direct.price_eur.amount:.2f}"
    else:
        airline = None
        price_eur = None

    return {
        "destination": str(analysis.destination),
        "airline": airline,
        "price_eur": price_eur,
        "price_difference_eur": (
            f"{analysis.price_difference.amount:.2f}"
            if analysis.price_difference is not None
            else None
        ),
        "relative_difference": (
            str(analysis.relative_difference) if analysis.relative_difference is not None else None
        ),
        "tier": analysis.tier.value,
        "tier_reason": analysis.tier_reason,
        "recommendation": direct_tier_recommendation_label(analysis.tier),
        "time_saved_minutes": (
            analysis.time_saved // _ONE_MINUTE if analysis.time_saved is not None else None
        ),
        "score_policy_divergence": analysis.score_policy_divergence,
        "divergence_explanation": analysis.divergence_explanation,
    }


def _early_stop_evaluation_to_json(
    destination: str, evaluation: EarlyStopEvaluation
) -> dict[str, Any]:
    """One "Early Stop Analysis" entry (Phase 6, T39, D12) -- the post-hoc
    replay's annotation for one destination. ``destination`` comes from the
    caller's ``early_stop_evaluations`` mapping key, not from the
    evaluation itself -- ``EarlyStopEvaluation`` only carries
    ``triggering_destination``, which its own validator forbids from being
    set when ``triggered`` is ``False``, so the mapping key is the one
    place "which destination is this" is recorded for the non-triggered
    case. ``triggering_origin``/``margin_eur`` are ``None`` exactly when
    ``triggered`` is ``False``. ``compared_against`` is always present --
    D12/master plan S4: "an order-dependent rule with an implicit
    comparison set is unauditable" -- even when empty (fewer than two
    origins ever had a valid fare to this destination).
    """
    return {
        "destination": destination,
        "evaluated_at_wave": evaluation.evaluated_at_wave,
        "triggered": evaluation.triggered,
        "triggering_origin": evaluation.triggering_origin,
        "margin_eur": (
            f"{evaluation.margin.amount:.2f}" if evaluation.margin is not None else None
        ),
        "compared_against": list(evaluation.compared_against),
        "mode": evaluation.mode,
    }


def _origin_summary_to_json(
    summary: OriginSummary, *, task_outcomes: Sequence[TaskOutcome]
) -> dict[str, Any]:
    """One "Origin Comparison" entry (Phase 6, T41, master plan A2-5c).

    ``cheapest_fare_eur``/``destination``/``itinerary_id`` are ``None``
    exactly when ``summary.best`` is ``None`` (this origin produced zero
    accepted itineraries) -- ``reason`` is populated in that same case,
    read off ``task_outcomes`` (``reporting.view.origin_absence_reason``),
    and ``None`` otherwise (a real fare needs no absence explanation).
    """
    if summary.best is not None:
        itinerary = summary.best.itinerary
        return {
            "origin": str(summary.origin),
            "ground_minutes": summary.ground_minutes,
            "cheapest_fare_eur": f"{itinerary.price_eur.amount:.2f}",
            "destination": last_segment(itinerary).destination,
            "itinerary_id": itinerary.itinerary_id,
            "reason": None,
        }
    return {
        "origin": str(summary.origin),
        "ground_minutes": summary.ground_minutes,
        "cheapest_fare_eur": None,
        "destination": None,
        "itinerary_id": None,
        "reason": origin_absence_reason(summary.origin, task_outcomes),
    }


def _rejected_self_transfer_to_json(rejected: RejectedItinerary) -> dict[str, Any]:
    """One "Self-Transfer Itineraries" appendix entry (D5, T41) -- enough to
    audit the exclusion without re-running validation: which itinerary,
    which airport(s) triggered it, and the exact ``Rejection.message``.
    """
    itinerary = rejected.itinerary
    departure_segment = first_segment(itinerary)
    arrival_segment = last_segment(itinerary)
    return {
        "itinerary_id": itinerary.itinerary_id,
        "origin": departure_segment.origin,
        "destination": arrival_segment.destination,
        "airline": airline_string(itinerary),
        "route": route_string(itinerary),
        "price_eur": f"{itinerary.price_eur.amount:.2f}",
        "self_transfer_airports": self_transfer_airports(itinerary),
        "reason": rejected.rejection.message,
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
    destination_analyses: Sequence[DestinationAnalysis] = (),
    early_stop_evaluations: Mapping[str, EarlyStopEvaluation] = MappingProxyType({}),
    origin_summaries: Sequence[OriginSummary] = (),
    self_transfer_rejections: Sequence[RejectedItinerary] = (),
    top_n_by_destination: Mapping[str, Sequence[ScoredItinerary]] = MappingProxyType({}),
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

    ``destination_analyses`` is one ``DestinationAnalysis`` per registry
    destination (Phase 5, T33, D10) and becomes the top-level
    ``destination_analyses`` array verbatim, in the order given -- always
    present, empty when none were supplied (including the default), never
    omitted, for the same reason ``failed_searches`` never is.

    ``early_stop_evaluations`` is the T39 (Phase 6, D12) post-hoc replay's
    per-destination annotation (``orchestration.waves.replay_early_stop``),
    keyed by destination -- becomes the top-level ``early_stop_analysis``
    array, one entry per mapping item, in the mapping's own iteration
    order -- always present, empty when none were supplied (including the
    default), never omitted, for the same reason ``failed_searches`` never
    is. Purely an annotation: it never removes or reorders anything in
    ``top_itineraries`` -- see ``orchestration.waves``' own docstring.

    ``origin_summaries`` is T40's per-origin reduction
    (``scoring.origin_summary.summarize_by_origin``) and becomes the
    top-level ``origin_comparison`` array verbatim, in the order given --
    always present, empty when none were supplied (including the default),
    never omitted, for the same reason ``failed_searches`` never is.

    ``self_transfer_rejections`` is D5's excluded-itinerary set (T41) and
    becomes the top-level ``self_transfer_rejections`` array verbatim, in
    the order given -- always present, empty when none were supplied
    (including the default), for the same reason as every array above.
    None of these itineraries appear in ``top_itineraries`` -- D5 excludes
    them from the valid ranked set entirely, upstream of this function.

    ``top_n_by_destination`` is D15's "top 3 per destination" reduction
    (``scoring.ranking.top_n_by_destination``), keyed by destination --
    becomes the top-level ``top_itineraries_by_destination`` OBJECT (not an
    array, since it is keyed), sorted by destination for determinism
    (finding 0.3) rather than left in the mapping's own iteration order.
    Always present, an empty object when none were supplied (including the
    default).

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
        "destination_analyses": [
            _destination_analysis_to_json(analysis) for analysis in destination_analyses
        ],
        "early_stop_analysis": [
            _early_stop_evaluation_to_json(destination, evaluation)
            for destination, evaluation in early_stop_evaluations.items()
        ],
        "origin_comparison": [
            _origin_summary_to_json(summary, task_outcomes=task_outcomes)
            for summary in origin_summaries
        ],
        "self_transfer_rejections": [
            _rejected_self_transfer_to_json(rejected) for rejected in self_transfer_rejections
        ],
        "top_itineraries_by_destination": {
            destination: [
                _itinerary_to_json(item, data_source=data_source)
                for item in top_n_by_destination[destination]
            ]
            for destination in sorted(top_n_by_destination)
        },
    }
