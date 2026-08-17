"""Ranker v1 (T14) — turns a list of scored itineraries into a deterministic,
ranked list.

Master plan finding 0.3: ``score = price + 3*duration + penalty`` makes the
spec's own "then price, then duration" tiebreakers near-dead code, because
two genuinely distinct itineraries (different carriers, same price, same
times) can tie on all three keys. Python's ``sorted()`` is stable, so
without a further fix the tied items keep whatever order they arrived in —
which is provider response arrival order, and under a future concurrent
160-way fan-out (Phase 4+) that order is not reproducible. That breaks
golden-file testing and the "reproducible report" requirement outright.

Finding 0.3's resolution has two independent parts, and getting only one of
them right is not enough:

1. Pre-sort the INPUT by ``itinerary_id`` before any ranking sort runs, so
   Python's stable sort has no arrival-order-dependent state left to leak,
   no matter how short a later sort key is (see ``_rank_by_price`` below,
   whose own key is price alone).
2. Every published ordering ends in ``itinerary_id`` as its final tiebreak.
   ``ScoredItinerary.tiebreak_key`` already does this for the main ranking;
   this module's own price-only key does the same for the row-2 finding
   (spec's §2.6 "rank by lowest price" vs §5.8 "rank by score" — resolution
   per DECISIONS.md D16: publish adjusted_score as the primary ranking,
   ADDITIONALLY publish a price ranking, never pick one over the other).

``ScoredItinerary`` is frozen, and its three ``rank_by_*`` fields are
required (``ge=1``) at construction — callers build the pre-ranked
instances with placeholder rank values (e.g. ``1``), and ``rank_itineraries``
returns NEW instances (via ``model_copy``) carrying the real, computed rank
positions. It never mutates its input.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from decimal import Decimal

from flightagent.domain.scoring import ScoredItinerary


def _itinerary_id_sort_key(item: ScoredItinerary) -> str:
    return item.itinerary.itinerary_id


def _price_sort_key(item: ScoredItinerary) -> tuple[Decimal, str]:
    """Price-only ranking (finding 0.3 / spec §2.6-vs-§5.8), with its own
    ``itinerary_id`` final tiebreak per DECISIONS.md D16 note 1: "every sort
    key needs itinerary_id as its final tiebreak", not just the shipped
    main ranking."""
    return (item.itinerary.price_eur.amount, item.itinerary.itinerary_id)


def rank_itineraries(
    itineraries: Sequence[ScoredItinerary],
    *,
    top_n: int = 10,
) -> list[ScoredItinerary]:
    """Rank ``itineraries`` and return at most ``top_n`` of them.

    Main ranking (what the report presents, D16): ascending by
    ``adjusted_score``, then ``price_eur``, then ``total_duration``, then
    ``itinerary_id`` — exactly ``ScoredItinerary.tiebreak_key``.

    Also computes, on every returned item:

    - ``rank_by_adjusted_score``: position in the main ranking above.
    - ``rank_by_price``: position in a SEPARATE ranking by price alone
      (``_price_sort_key``) — the spec contradicts itself on price-vs-score
      ranking (§2.6 vs §5.8); D16 resolves this by publishing both, not by
      picking a winner.
    - ``rank_by_total_journey_score``: Phase 2 has no ground-travel scoring
      (that lands in Phase 6 / T38), so ``total_journey_score`` reduces to
      exactly ``adjusted_score`` right now (``ground_cost_component`` and
      ``ground_time_component`` are always zero). Ranking by it is therefore
      IDENTICAL to ranking by ``adjusted_score`` today — not a placeholder
      standing in for a different ranking, an actually-correct one that will
      diverge once Phase 6 populates the ground components.

    Ranks are computed over the FULL input before truncation, so an item
    returned in a top-10 slice still carries its true global rank rather
    than a 1..10 renumbering — matching D16 ("truncation... applies to the
    presentation... only").

    Finding 0.3, part 1: ``itineraries`` is sorted by ``itinerary_id`` FIRST,
    before either ranking sort below runs, so Python's stable ``sorted()``
    can never leak this function's caller's arrival order into a tie.

    ``top_n`` defaults to 10 (D16's shipped "global top 10") but stays a
    keyword parameter so a future concurrent fan-out (Phase 4+) or the
    per-destination top-3 table (D16) can call this with a different value
    without a signature change. ``top_n <= 0`` returns an empty list;
    ``top_n`` larger than the input returns every item.
    """
    pre_sorted = sorted(itineraries, key=_itinerary_id_sort_key)

    by_adjusted_score = sorted(pre_sorted, key=lambda item: item.tiebreak_key)
    by_price = sorted(pre_sorted, key=_price_sort_key)

    # Keyed by object identity, not itinerary_id: the two sorts above
    # reorder the SAME objects, so `id()` maps a ranked position back to
    # its source item without relying on itinerary_id being collision-free.
    price_rank_by_identity = {id(item): rank for rank, item in enumerate(by_price, start=1)}

    ranked: list[ScoredItinerary] = []
    for rank, item in enumerate(by_adjusted_score, start=1):
        ranked.append(
            item.model_copy(
                update={
                    "rank_by_adjusted_score": rank,
                    "rank_by_price": price_rank_by_identity[id(item)],
                    # See docstring: exactly equal to rank_by_adjusted_score
                    # in Phase 2, not a placeholder — total_journey_score
                    # IS adjusted_score until Phase 6 populates the ground
                    # components.
                    "rank_by_total_journey_score": rank,
                }
            )
        )

    if top_n <= 0:
        return []
    return ranked[:top_n]


def _destination(item: ScoredItinerary) -> str:
    """The itinerary's arrival airport -- its last leg's last segment's
    destination. Inlined rather than imported from ``reporting.view``:
    module boundaries (master plan S3) restrict ``scoring`` to importing
    ``domain`` + ``config`` only, the identical reasoning
    ``scoring.origin_summary``'s own ``_origin`` helper already documents.
    """
    return item.itinerary.legs[-1].segments[-1].destination


def top_n_by_destination(
    itineraries: Sequence[ScoredItinerary], *, top_n: int = 3
) -> dict[str, list[ScoredItinerary]]:
    """Group ``itineraries`` by arrival airport and reduce each group to its
    own top ``top_n``, ordered by the identical ``tiebreak_key`` the global
    ranking uses (D16) -- D15's "top 3 per destination additionally in the
    JSON" requirement (T41), ``settings.output.top_n_per_destination``'s
    consumer.

    A SEPARATE reduction from ``rank_itineraries``'s global top-N, not a
    filter over it, a re-ranking of it, or scoped to whatever the global
    ranking already truncated to -- exactly the relationship
    ``scoring.origin_summary.summarize_by_origin`` documents for its own
    per-origin reduction. A caller wanting both the global top-10 and this
    per-destination view passes the SAME full, untruncated ``itineraries``
    sequence to both.

    Does not renumber ``rank_by_*`` -- every returned item keeps whatever
    its caller already computed for it globally (e.g. via a prior
    ``rank_itineraries`` call over the same full set), matching
    ``summarize_by_origin``'s identical "does not mutate or re-rank its
    input" contract.

    A destination with zero itineraries in ``itineraries`` is simply absent
    from the returned mapping -- there is nothing to rank for it. Unlike
    ``summarize_by_origin``'s "every origin gets a row even when empty"
    contract (A2-5c), D15 does not ask for an explicit reason-for-absence
    entry here; a destination's visibility gap is already covered by the
    Direct Flight Analysis table (T33, every registry destination
    unconditionally) and the ``RANK_DESTINATION_DROPPED`` log event.
    """
    by_destination: dict[str, list[ScoredItinerary]] = defaultdict(list)
    for item in itineraries:
        by_destination[_destination(item)].append(item)

    return {
        destination: sorted(group, key=lambda item: item.tiebreak_key)[:top_n]
        for destination, group in by_destination.items()
    }
