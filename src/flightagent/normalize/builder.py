"""Build a ``NormalizedItinerary`` from a ``RawOffer``.

Phase 2 scope note (T11 brief): the only input source this phase has is
the mock provider (T10), which constructs ``Segment``/``Leg`` objects
directly with real UTC timestamps. So there is no raw provider JSON to
tz-convert here — that mapping work is Phase 7's adapter layer
(``providers/amadeus/mapper.py`` / ``providers/duffel/mapper.py``). What
THIS module actually has to do, once ``RawOffer.legs`` already holds
fully-formed, individually-validated ``Leg``/``Segment`` objects, turns
out to be much narrower than "compute the derived fields" — see the
module-level finding below.

**Finding worth flagging to whoever builds the next layer on top of this
(the validator, T-something in Phase 3):** ``total_duration``,
``stop_count`` and ``technical_stop_count`` are ALREADY ``computed_field``
properties — on ``Leg`` (``total_duration``, ``connection_count``,
``technical_stop_count`` in domain/itinerary.py) AND, aggregated across
legs, on ``NormalizedItinerary`` itself (``total_duration``,
``stop_count``, ``technical_stop_count``, also in domain/itinerary.py).
Nothing in this module recomputes them — they simply exist the moment
``legs=...`` is passed to the ``NormalizedItinerary`` constructor. The
endpoints-vs-sum invariant (master plan S4: "derive the total from
endpoints, then ASSERT the sum matches, rather than computing it twice
and hoping") is ALSO already enforced, inside ``Leg._validate_shape``, a
pydantic ``model_validator`` that runs at ``Leg`` construction time and
raises ``ValueError`` if the sum of segment + layover durations doesn't
match the endpoint-derived total. Since ``RawOffer.legs: tuple[Leg, ...]``
can only ever hold already-validated ``Leg`` instances under normal
construction, this function cannot actually receive a ``Leg`` whose
invariant is violated — there is no code path to it.

``_assert_leg_duration_invariant`` below re-checks the identical
condition anyway, because the task brief explicitly asks for it and
because defense-in-depth at the normalize-layer boundary is cheap
insurance against a future caller that bypasses validation via
``Leg.model_construct(...)`` (e.g. a hand-rolled test fixture or a buggy
mapper that skips the constructor). Under any *normal* construction path
it is genuinely dead code — that is a feature, not a gap: it means the
domain layer already did the load-bearing part of this job in Phase 1.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import timedelta
from typing import Literal

from pydantic import AwareDatetime, HttpUrl

from flightagent.domain.airport import CarrierCode
from flightagent.domain.enums import CabinClass
from flightagent.domain.ids import compute_itinerary_id
from flightagent.domain.itinerary import Leg, NormalizedItinerary, RawOffer
from flightagent.domain.money import Money


class NormalizationInvariantError(RuntimeError):
    """Raised when a leg's endpoints-vs-sum duration invariant does not
    hold.

    This is a data-integrity assertion, not a validation-engine rejection
    (``domain.validation.Rejection`` / ``RejectionCode``, Phase 3): a
    violation means a mapper or mock-generator bug upstream, and master
    plan S4 is explicit that this must "fail loudly", never be logged and
    silently patched over with a recomputed total.
    """


class UnsupportedCurrencyError(ValueError):
    """Raised when a non-EUR ``price_original`` reaches this function.

    D14: a converted price must never be presentable as a quoted one, and
    FX conversion (``normalize/fx.py``) is out of scope until a non-EUR
    provider exists (Phase 7). Until that module exists, this function
    has no lawful way to produce a ``price_eur`` for a non-EUR offer, so
    it rejects the itinerary outright rather than mislabelling the
    original amount as EUR or inventing a rate.
    """


def _assert_leg_duration_invariant(leg: Leg) -> None:
    """Re-assert ``sum(segment durations) + sum(layover durations) ==
    total_duration`` for one leg. See the module docstring for why this
    is unreachable-but-kept under normal construction.
    """
    summed_total = sum((segment.duration for segment in leg.segments), timedelta()) + sum(
        (layover.duration for layover in leg.layovers), timedelta()
    )
    if summed_total != leg.total_duration:
        raise NormalizationInvariantError(
            f"leg duration invariant violated: sum(segment durations) + "
            f"sum(layover durations) = {summed_total} != total_duration "
            f"(last.arrive_utc - first.depart_utc) = {leg.total_duration} for leg "
            f"{leg.segments[0].origin}->{leg.segments[-1].destination} — this indicates a "
            f"mapper/generator bug upstream, per master plan S4"
        )


def compute_shape_key(legs: Iterable[Leg], *, cabin: CabinClass, adults: int) -> str:
    """The finding-0.2 dedup shape key: a content hash over
    ``(origin, destination, depart_utc, arrive_utc)`` per segment across
    all legs, plus ``cabin`` and ``adults``.

    This *computes* the key that identifies an itinerary's shape; it does
    not group itineraries by it or pick a dedup survivor — that grouping
    step is ``normalize/dedup.py`` (Phase 3, T20, explicitly out of scope
    for this task). ``domain.ids`` has no existing helper for this
    specific formula (only ``compute_itinerary_id``, which takes an
    already-built shape key as an opaque string input) — this is a new
    hashing convention, not a duplicate of one that already existed.

    Deterministic and order-preserving over ``legs``/``segments`` (unlike
    ``compute_itinerary_id``'s carrier list, segment ORDER is part of the
    itinerary's identity — AMS-DXB-DEL is a different shape from
    DEL-DXB-AMS, so this deliberately does not sort segments before
    hashing).
    """
    segment_tuples = tuple(
        (
            segment.origin,
            segment.destination,
            segment.depart_utc.isoformat(),
            segment.arrive_utc.isoformat(),
        )
        for leg in legs
        for segment in leg.segments
    )
    canonical = repr((segment_tuples, str(cabin), adults))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"shape_{digest}"


def _marketing_carriers(legs: Iterable[Leg]) -> list[CarrierCode]:
    return [segment.marketing_carrier for leg in legs for segment in leg.segments]


def _price_eur(raw_offer: RawOffer) -> Money:
    """``price_eur`` per D14: equal to ``price_original`` when it is
    already EUR; raise rather than silently convert otherwise.
    """
    if raw_offer.price.currency != "EUR":
        raise UnsupportedCurrencyError(
            f"price_original currency is {raw_offer.price.currency!r}, not EUR. D14 forbids "
            f"ever silently converting a non-EUR price, and FX conversion "
            f"(normalize/fx.py) is out of scope until a non-EUR provider exists (Phase 7) — "
            f"so this itinerary is rejected rather than mislabelled as a EUR quote."
        )
    return raw_offer.price


def build_normalized_itinerary(
    raw_offer: RawOffer,
    *,
    adults: int,
    cabin: CabinClass,
    fare_as_of: AwareDatetime,
) -> NormalizedItinerary:
    """Turn one ``RawOffer`` into a fully-computed ``NormalizedItinerary``.

    ``adults`` and ``cabin`` are required, not defaulted, even though D3
    currently fixes ``adults=1`` project-wide: they are components of the
    finding-0.2 shape key, and the caller (the future orchestration
    layer, which already has the ``SearchRequest`` that produced this
    offer) always knows both. Defaulting either one here would let a
    caller silently mis-key an itinerary's dedup identity instead of
    stating it explicitly.

    ``fare_as_of`` is likewise required rather than defaulted to "now":
    master plan S8.6 requires every itinerary to carry the instant its
    fare was retrieved, rendered plainly next to the price, and a
    normalizer that stamps its own wall-clock time would make golden-file
    testing (finding 0.3 / the mock-generator determinism property)
    non-reproducible. The caller (whatever recorded the provider
    response) is the only party that actually knows this instant.

    Deliberately NOT done here (out of scope for this task):

    - FX conversion (``normalize/fx.py``, Phase 7) — see
      ``UnsupportedCurrencyError``.
    - Dedup / survivor selection (``normalize/dedup.py``, Phase 3 T20) —
      ``duplicate_count`` stays at its model default of ``1`` and
      ``also_offered_by`` stays empty; this function normalizes exactly
      one offer into exactly one itinerary and does not know about any
      others.
    - The real ``BookingLinkStrategy`` port (finding 0.6) — not built in
      any phase yet. Until it exists, this function does the smallest
      honest thing: a provider-supplied booking URL is passed through
      labelled ``"provider_native"``; its absence is labelled
      ``"unavailable"``. It never fabricates a search-deeplink URL, which
      is what ``DeepLinkTemplate`` will require config this module does
      not have.

    Raises ``NormalizationInvariantError`` if a leg's duration invariant
    does not hold, and ``UnsupportedCurrencyError`` if the offer's price
    is not already in EUR (D14).
    """
    for leg in raw_offer.legs:
        _assert_leg_duration_invariant(leg)

    price_eur = _price_eur(raw_offer)

    shape_key = compute_shape_key(raw_offer.legs, cabin=cabin, adults=adults)
    carriers = _marketing_carriers(raw_offer.legs)
    itinerary_id = compute_itinerary_id(shape_key, carriers, price_eur)

    booking_url: HttpUrl | None
    booking_url_kind: Literal["provider_native", "unavailable"]
    if raw_offer.provider_booking_url is not None:
        booking_url = raw_offer.provider_booking_url
        booking_url_kind = "provider_native"
    else:
        booking_url = None
        booking_url_kind = "unavailable"

    return NormalizedItinerary(
        itinerary_id=itinerary_id,
        provider=raw_offer.provider,
        legs=raw_offer.legs,
        price_original=raw_offer.price,
        price_eur=price_eur,
        booking_url=booking_url,
        booking_url_kind=booking_url_kind,
        offer_expires_at=raw_offer.offer_expires_at,
        shape_key=shape_key,
        fare_as_of=fare_as_of,
    )
