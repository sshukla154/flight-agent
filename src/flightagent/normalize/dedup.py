"""Dedup engine (T20) — collapse itineraries that share a ``shape_key``.

**Built to finding 0.2's resolution, not the spec's own literal wording.**
Spec S5.7 says dedup on "same airline + same flight numbers + same times".
That key is wrong on its own terms: the dominant duplicate in real
inventory is the codeshare (KL/AF/DL/KQ selling one physical flight under
four different marketing flight numbers at identical times), and keying on
flight number keeps all four as separate rows instead of collapsing them.

The correct key is the itinerary **shape key** — ``(segment
origin/destination/depart_utc/arrive_utc tuples, cabin, adults)`` — which
deliberately excludes carrier and flight number, so codeshare siblings
hash identically and collapse. That key is already computed at
normalization time (T11, ``normalize.builder.compute_shape_key``) and
carried on every ``NormalizedItinerary`` as ``shape_key``. This module does
not compute it; it only groups an already-normalized list by the key each
itinerary already carries, and picks a survivor per group.

``NormalizedItinerary`` is frozen (``ConfigDict(frozen=True)``), so
"populating" the survivor's ``duplicate_count``/``also_offered_by``/
``fare_options`` means building a new instance via ``model_copy(update=
...)`` — the same pattern ``scoring.ranking.rank_itineraries`` already
uses for its own frozen ``ScoredItinerary``. Every other field is carried
through unchanged; ``model_copy`` does not re-run validators, which is
fine here because none of the fields being changed are inputs to
``NormalizedItinerary._validate_fx``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence

from flightagent.domain.itinerary import CodeshareReference, FareOption, NormalizedItinerary
from flightagent.domain.segment import Segment
from flightagent.observability.events import EventName
from flightagent.observability.logging import log_event


def _all_segments(itinerary: NormalizedItinerary) -> list[Segment]:
    return [segment for leg in itinerary.legs for segment in leg.segments]


def _codeshare_reference(itinerary: NormalizedItinerary) -> CodeshareReference:
    """Build one non-survivor's display reference.

    ``CodeshareReference`` holds exactly one ``marketing_carrier`` and one
    optional ``flight_number`` (domain/itinerary.py) — it cannot represent
    a multi-segment itinerary's full list of per-segment flight numbers.
    The classic finding-0.2 case (KL/AF/DL/KQ reselling one physical
    *direct* flight under four marketing designations) is single-segment,
    so the first (only) segment's carrier/number is exact there. For a
    multi-segment non-survivor, the first segment's ``marketing_carrier``
    is used as the itinerary's representative carrier, and ``flight_number``
    is left ``None`` — a single ``str`` field cannot honestly represent more
    than one segment's number, and guessing one would misrepresent the
    itinerary as covering only that segment's leg.
    """
    segments = _all_segments(itinerary)
    flight_number = segments[0].flight_number if len(segments) == 1 else None
    return CodeshareReference(
        marketing_carrier=segments[0].marketing_carrier, flight_number=flight_number
    )


def _fare_option(itinerary: NormalizedItinerary) -> FareOption:
    """Build one collapsed offer's ``FareOption`` entry.

    ``NormalizedItinerary`` carries no ``fare_brand`` / ``checked_baggage``
    / ``refundable`` / ``provider_offer_id`` fields of its own —
    ``RawOffer.provider_offer_id`` does not survive normalization, and D4 /
    mapping_sketch.md S3.1 model fare-brand and baggage detail only on
    ``FareOption`` itself, never on ``NormalizedItinerary``. So every
    ``FareOption`` built here uses the tri-state "unknown" / ``None``
    defaults for all four fields — there genuinely is no "better
    information" on the source model to promote instead; this is a
    documented model gap (``DECISIONS.md``'s
    ``baggage_not_modelled_dedup_lossy`` open question), not a shortcut
    taken by this function.
    """
    return FareOption(price=itinerary.price_eur)


def _collapse_group(group: Sequence[NormalizedItinerary]) -> NormalizedItinerary:
    """Collapse one shape-key group into a single survivor.

    Survivor: lowest ``price_eur``, with ``itinerary_id`` as the
    deterministic tiebreak (finding 0.3's fourth sort key) so an exact
    price tie within a group never depends on input arrival order.

    ``duplicate_count`` is the group size, ``also_offered_by`` holds one
    ``CodeshareReference`` per NON-survivor, and ``fare_options`` holds one
    ``FareOption`` per itinerary in the group INCLUDING the survivor —
    this holds for a group of size 1 too (a lone itinerary gets
    ``duplicate_count=1``, an empty ``also_offered_by`` — already the
    model default — and a ONE-entry ``fare_options`` tuple representing
    itself). That is a deliberate reading of ``FareOption``'s own
    docstring ("one collapsed offer's fare-brand detail" — a lone
    itinerary is a collapse of exactly one offer, not zero), applied via
    the same code path as every other group rather than a special case for
    size-1 groups.
    """
    survivor_index = min(
        range(len(group)),
        key=lambda i: (group[i].price_eur.amount, group[i].itinerary_id),
    )
    survivor = group[survivor_index]
    others = [itinerary for index, itinerary in enumerate(group) if index != survivor_index]

    also_offered_by = tuple(_codeshare_reference(itinerary) for itinerary in others)
    fare_options = (_fare_option(survivor), *(_fare_option(itinerary) for itinerary in others))

    return survivor.model_copy(
        update={
            "duplicate_count": len(group),
            "also_offered_by": also_offered_by,
            "fare_options": fare_options,
        }
    )


def deduplicate(itineraries: Sequence[NormalizedItinerary]) -> list[NormalizedItinerary]:
    """Group ``itineraries`` by ``shape_key`` and return one survivor per
    group (finding 0.2).

    Grouping uses a plain ``dict`` keyed by ``shape_key`` (Python dicts
    preserve first-insertion order), so the returned list's order is a
    deterministic function of the input list's order — no group ever
    depends on anything but which ``shape_key``s appeared and in what
    order. This module does not additionally sort its output: the
    downstream ranker (``scoring.ranking.rank_itineraries``) already
    pre-sorts its full input by ``itinerary_id`` before computing any
    ranking (finding 0.3), so the final report's determinism does not
    depend on the order this function returns.

    Emits one ``EventName.DEDUP_COMPLETED`` event after grouping
    completes, carrying ``input_count`` (itineraries passed in),
    ``output_count`` (surviving groups) and ``duplicate_count`` (total
    non-survivor itineraries collapsed away, summed across every group —
    zero when every ``shape_key`` was unique).
    """
    groups: dict[str, list[NormalizedItinerary]] = defaultdict(list)
    for itinerary in itineraries:
        groups[itinerary.shape_key].append(itinerary)

    survivors: list[NormalizedItinerary] = []
    total_duplicates = 0
    for group in groups.values():
        survivors.append(_collapse_group(group))
        total_duplicates += len(group) - 1

    log_event(
        EventName.DEDUP_COMPLETED,
        input_count=len(itineraries),
        output_count=len(survivors),
        duplicate_count=total_duplicates,
    )
    return survivors
