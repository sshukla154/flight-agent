"""Per-origin ranking view (T40) — groups already-scored ``ScoredItinerary``
objects by departure airport and reduces each origin to a single summary
row for the Origin Comparison table (T41, master plan acceptance
criterion A2-5c).

This is a SEPARATE reduction from ``ranking.rank_itineraries``'s global
top-N, not a replacement for it, a filter applied before/after it, or a
re-ranking of it. T41 needs BOTH views computed over the exact SAME
underlying ``ScoredItinerary`` set:

- the existing global top-10 ranking (``ranking.rank_itineraries``,
  unchanged, D16) — "one global top-10 ranking across everything";
- this per-origin view — for each of the 10 configured origins, that
  origin's own single cheapest valid itinerary.

Both views must AGREE about what a given itinerary's fare is (a fare in
the global top-10 also appears correctly attributed to its origin here) —
see ``test_ranker.py``'s cross-view agreement test. That only holds if
both are computed from the same, UNTRUNCATED ``ScoredItinerary`` sequence:
``rank_itineraries`` already supports this (``top_n`` "larger than the
input returns every item" — its own docstring), so a caller wanting both
views should call ``rank_itineraries(scored, top_n=len(scored))`` (or any
``top_n`` covering the whole set) once, slice ``[:10]`` for the global
display table, and pass the SAME full list to ``summarize_by_origin``
below for the per-origin table. Passing an already-truncated top-10 list
to ``summarize_by_origin`` instead would silently make most origins show
``best=None`` even when they genuinely have valid, accepted itineraries
that just did not make the global top-10 cut — exactly the kind of
silent-drop this project's own "never silently discard" convention (see
``normalize.dedup``, ``cli._warn_on_truncated_destinations``) forbids.

Also deliberately NOT the same grouping as D12's early-stop rule (T39,
``orchestration``): that rule groups by DESTINATION and compares each
destination's cheapest fare across origins already completed. This module
groups by ORIGIN and, for each one, picks its single overall cheapest
itinerary across every destination — master plan A2-5c's "the origin
comparison table lists a cheapest-valid price for all 10 origins" is one
number per origin, not a per-destination breakdown.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence

from flightagent.domain.scoring import OriginSummary, ScoredItinerary


def _origin(item: ScoredItinerary) -> str:
    """The itinerary's departure airport — its first leg's first segment's
    origin.

    Inlined rather than imported from ``reporting.view.first_segment``:
    module boundaries (master plan S3) restrict ``scoring`` to importing
    ``domain`` + ``config`` only — ``reporting`` is a sibling package, not
    a dependency of ``scoring``. ``validation.rules`` already reaches into
    ``itinerary.legs[0].segments[0].origin`` directly for the identical
    reason (it too may not import ``reporting``).
    """
    return item.itinerary.legs[0].segments[0].origin


def summarize_by_origin(
    itineraries: Sequence[ScoredItinerary],
    *,
    origins: Sequence[str],
) -> list[OriginSummary]:
    """Reduce ``itineraries`` to one ``OriginSummary`` per entry in
    ``origins``, in the exact order ``origins`` is given.

    ``origins`` is an explicit parameter, never read from
    ``airports.registry`` here — ``scoring`` may not import that module
    (module boundaries, master plan S3); the caller (orchestration/cli,
    which already resolved the run's real origin set) always knows it
    already, the same reasoning ``normalize.builder.build_normalized_itinerary``
    already applies to ``adults``/``cabin`` ("the caller always knows this,
    defaulting it here would let a caller silently mis-key it").

    Acceptance criterion A2-5c ("all 10 origins ... or an explicit reason
    for absence"): every IATA code in ``origins`` gets exactly one
    ``OriginSummary`` in the returned list, even one with zero itineraries
    (``best=None``) — an origin is never silently dropped for having no
    results. An itinerary whose origin is NOT in ``origins`` is silently
    excluded from every summary (there is no row for it to attribute to);
    callers are expected to pass the run's real, complete origin set.

    Winner selection reuses ``ScoredItinerary.tiebreak_key`` — the exact
    same ``(adjusted_score, price_eur, duration_seconds, itinerary_id)``
    ordering ``rank_itineraries`` sorts the global ranking by (D16) — so an
    origin's "best" itinerary here is never a different notion of "best"
    than the global ranking would compute for that same itinerary. Ties
    within one origin resolve via that same key's final ``itinerary_id``
    term (finding 0.3), so the winner picked here can never depend on
    ``itineraries``' arrival order, exactly like ``rank_itineraries`` and
    ``normalize.dedup._collapse_group``.

    Does not mutate or re-rank its input — the ``rank_by_*`` fields on a
    returned ``OriginSummary.best`` are whatever the caller already
    computed for it (e.g. via a prior whole-set ``rank_itineraries`` call),
    not renumbered per-origin by this function.
    """
    by_origin: dict[str, list[ScoredItinerary]] = defaultdict(list)
    for item in itineraries:
        by_origin[_origin(item)].append(item)

    summaries: list[OriginSummary] = []
    for origin in origins:
        group = by_origin.get(origin, [])
        if not group:
            summaries.append(OriginSummary(origin=origin))
            continue
        best = min(group, key=lambda item: item.tiebreak_key)
        summaries.append(OriginSummary(origin=origin, best=best))
    return summaries
