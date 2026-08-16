"""Amadeus Flight Offers Search v2 -> domain mapping.

The REAL, working piece of T49 (unlike ``provider.py``/``auth.py``/
``errors.py``'s stubs in this same package): a pure-function pipeline that
turns one already-parsed Amadeus offer dict (a ``data[]`` element of
``tests/fixtures/providers/amadeus_offers_sample.json``, or a real response
with the identical documented shape) into a ``RawOffer``, plus a companion
``extract_fare_option`` that reads the same dict's fare-brand/baggage/
refundability detail into a ``FareOption`` (spikes/mapping_sketch.md
S3.1/S3.2/S3.6 -- the exact gap that document exists to close).

Two hazards this module exists specifically to get right
(spikes/mapping_sketch.md S4):

1. ISO-8601 durations (``PT6H25M``, ``PT17H0M``, ``PT9H35M``, ...) --
   delegated to pydantic's own ``timedelta`` coercion
   (``_DURATION_ADAPTER`` below) rather than a hand-rolled regex, per that
   document's own recommendation ("Pydantic v2 will coerce these into
   timedelta ... the cheapest correct route ... Ideally: do not hand-roll
   one"). This also correctly handles a day component (``P1DT2H``), unlike
   a ``PT(\\d+H)?(\\d+M)?``-shaped regex would.
2. Offsetless local wall-clock times (``"2027-07-17T14:55:00"``, no zone, no
   offset) -- every Amadeus timestamp on ``departure.at``/``arrival.at`` is
   a NAIVE local reading that means nothing until paired with the airport's
   IANA zone. ``_resolve_iana_zone`` below is that pairing; naive
   subtraction of two such readings across a timezone change is wrong
   while still looking entirely plausible (the +02:00 -> +05:30 AMS->DEL
   case spikes/mapping_sketch.md S4.3 documents, and
   ``tests/unit/test_amadeus_mapping.py`` asserts against directly).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import TypeAdapter

from flightagent.airports import registry as airport_registry
from flightagent.domain.enums import CabinClass
from flightagent.domain.itinerary import FareOption, Leg, RawOffer
from flightagent.domain.money import Money
from flightagent.domain.segment import Layover, Segment

_DURATION_ADAPTER: TypeAdapter[timedelta] = TypeAdapter(timedelta)

BaggageState = Literal["included", "not_included", "unknown"]
RefundableState = Literal["allowed", "not_allowed", "unknown"]

_HUB_TZ_FALLBACK: dict[str, str] = {
    # Connection hubs this fixture's one-stop itineraries route through that
    # fall outside config/airports.yaml's 18-airport catalog (the project's
    # 10 European origins + 8 Indian destinations, per that file's own
    # docstring) -- flightagent.airports.registry legitimately has no entry
    # for a third-country transit hub, so this is a small, adapter-local
    # supplement, not a second copy of the project's own origin/destination
    # data. Mirrors the same three zones already established (for the
    # identical reason) in providers.mock.generator._AIRPORT_TZ.
    "DXB": "Asia/Dubai",
    "IST": "Europe/Istanbul",
    "DOH": "Asia/Qatar",
}


class UnmappableAmadeusOfferError(ValueError):
    """Raised when an Amadeus offer dict is missing a field this mapper
    requires, names an IATA code no zone can be resolved for, or states a
    duration that disagrees with its own endpoints. Never silently
    skipped -- a bad mapper input must fail loudly (master plan S4), not
    produce a partially-populated ``RawOffer``.
    """


def _resolve_iana_zone(iata: str) -> str:
    """The IANA zone for one IATA code -- the project catalog first, the
    small hub fallback above second. Raises if neither knows it (never
    default to UTC, master plan S4)."""
    try:
        return airport_registry.get(iata).iana_tz
    except airport_registry.UnknownAirportError:
        pass
    try:
        return _HUB_TZ_FALLBACK[iata]
    except KeyError:
        raise UnmappableAmadeusOfferError(
            f"no IANA zone known for IATA code {iata!r} -- not in config/airports.yaml "
            f"and not in this mapper's hub fallback table"
        ) from None


def parse_iso8601_duration(value: str) -> timedelta:
    """Parse an Amadeus ISO-8601 duration string (``PT6H25M``, ``PT17H0M``,
    ``PT17H``, ``PT9H35M``, ...). See module docstring, hazard 1.
    """
    return _DURATION_ADAPTER.validate_python(value)


def _parse_naive_local(value: str) -> datetime:
    """Parse an offsetless local wall-clock string
    (``"2027-07-17T14:55:00"``) into a NAIVE ``datetime`` -- the caller
    attaches the zone. Raises if ``value`` unexpectedly carries an offset,
    since that would mean this mapper's "every Amadeus flight time is
    zoneless local" assumption no longer holds.
    """
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is not None:
        raise UnmappableAmadeusOfferError(
            f"expected an offsetless local timestamp, got one carrying tzinfo: {value!r}"
        )
    return parsed


def _to_utc(local_naive: datetime, zone: ZoneInfo, *, fold: int = 0) -> datetime:
    return local_naive.replace(tzinfo=zone, fold=fold).astimezone(UTC)


_CABIN_BY_AMADEUS_VALUE: dict[str, CabinClass] = {
    "ECONOMY": CabinClass.ECONOMY,
    "PREMIUM_ECONOMY": CabinClass.PREMIUM_ECONOMY,
    "BUSINESS": CabinClass.BUSINESS,
    "FIRST": CabinClass.FIRST,
}


def _fare_details_by_segment_id(offer: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Join key for cabin (spikes/mapping_sketch.md S1.1) and for
    ``extract_fare_option``'s baggage/brand lookup. D3 fixes one adult, so
    ``travelerPricings[0]`` is always the single traveler this project ever
    prices."""
    traveler_pricing = offer["travelerPricings"][0]
    return {detail["segmentId"]: detail for detail in traveler_pricing["fareDetailsBySegment"]}


def _cabin_for_segment(
    segment_id: str, fare_details_by_segment: dict[str, dict[str, Any]]
) -> CabinClass:
    """spikes/mapping_sketch.md S1.1/S2.2: Amadeus puts cabin on
    ``travelerPricings[].fareDetailsBySegment[]``, joined by ``segmentId``,
    never on the segment object itself, and spells it upper-case."""
    try:
        detail = fare_details_by_segment[segment_id]
    except KeyError:
        raise UnmappableAmadeusOfferError(
            f"no fareDetailsBySegment entry for segmentId {segment_id!r}"
        ) from None
    raw_cabin = detail["cabin"]
    try:
        return _CABIN_BY_AMADEUS_VALUE[raw_cabin]
    except KeyError:
        raise UnmappableAmadeusOfferError(
            f"unrecognised Amadeus cabin value {raw_cabin!r}"
        ) from None


def map_segment(
    raw_segment: dict[str, Any], fare_details_by_segment: dict[str, dict[str, Any]]
) -> Segment:
    """One ``itineraries[].segments[]`` element -> one ``Segment``."""
    origin = raw_segment["departure"]["iataCode"]
    destination = raw_segment["arrival"]["iataCode"]
    origin_tz = _resolve_iana_zone(origin)
    destination_tz = _resolve_iana_zone(destination)
    origin_zone = ZoneInfo(origin_tz)
    destination_zone = ZoneInfo(destination_tz)

    depart_local_naive = _parse_naive_local(raw_segment["departure"]["at"])
    arrive_local_naive = _parse_naive_local(raw_segment["arrival"]["at"])
    depart_utc = _to_utc(depart_local_naive, origin_zone)
    arrive_utc = _to_utc(arrive_local_naive, destination_zone)

    marketing_carrier = raw_segment["carrierCode"]
    # spikes/mapping_sketch.md S2.1 / README item 1: Amadeus MAY omit
    # `operating` entirely when it equals the marketing carrier --
    # unverified against a live response, so absence is treated as "same
    # carrier", never as "unknown", exactly as that document specifies. If
    # this defaulted to None instead, every non-codeshare segment's
    # also_offered_by output downstream would be wrong (finding 0.2).
    operating_carrier = raw_segment.get("operating", {}).get("carrierCode", marketing_carrier)

    segment_id = raw_segment["id"]

    return Segment(
        segment_id=segment_id,
        origin=origin,
        destination=destination,
        depart_utc=depart_utc,
        arrive_utc=arrive_utc,
        depart_local=depart_local_naive.replace(tzinfo=origin_zone),
        arrive_local=arrive_local_naive.replace(tzinfo=destination_zone),
        origin_tz=origin_tz,
        destination_tz=destination_tz,
        marketing_carrier=marketing_carrier,
        operating_carrier=operating_carrier,
        flight_number=raw_segment["number"],
        cabin=_cabin_for_segment(segment_id, fare_details_by_segment),
        technical_stops=raw_segment.get("numberOfStops", 0),
        duration=parse_iso8601_duration(raw_segment["duration"]),
    )


def _build_layover(prev: Segment, nxt: Segment) -> Layover:
    zone = ZoneInfo(prev.destination_tz)
    return Layover(
        airport=prev.destination,
        arrive_utc=prev.arrive_utc,
        depart_utc=nxt.depart_utc,
        duration=nxt.depart_utc - prev.arrive_utc,
        local_window=(prev.arrive_utc.astimezone(zone), nxt.depart_utc.astimezone(zone)),
        requires_airport_change=False,
        requires_terminal_change=False,
    )


def map_leg(
    raw_itinerary: dict[str, Any], fare_details_by_segment: dict[str, dict[str, Any]]
) -> Leg:
    """One ``itineraries[]`` element -> one ``Leg``."""
    segments = tuple(
        map_segment(raw_segment, fare_details_by_segment)
        for raw_segment in raw_itinerary["segments"]
    )
    layovers = tuple(
        _build_layover(prev, nxt) for prev, nxt in zip(segments, segments[1:])  # noqa: B905
    )
    leg = Leg(segments=segments, layovers=layovers)

    # Defense-in-depth per spikes/mapping_sketch.md S4.1: assert the
    # ASSEMBLED leg's own endpoints-derived total matches the provider's
    # independently-stated itinerary-level `duration`. A real mismatch
    # means either a mapper bug or (README item 3) Amadeus not including a
    # technical stop's ground time inside its segment's own duration,
    # contrary to this mapper's assumption.
    stated_duration = parse_iso8601_duration(raw_itinerary["duration"])
    if leg.total_duration != stated_duration:
        raise UnmappableAmadeusOfferError(
            f"itinerary duration {stated_duration} does not match endpoints-derived "
            f"{leg.total_duration} -- see tests/fixtures/providers/README.md item 3"
        )
    return leg


def map_offer(offer: dict[str, Any], *, raw_payload_ref: str) -> RawOffer:
    """One Amadeus ``data[]`` element -> one ``RawOffer``.

    ``raw_payload_ref`` is supplied by the caller, not derived here -- this
    module has no cache layer of its own, and spikes/mapping_sketch.md
    S2.6 requires it to point at the WHOLE response body (``dictionaries``
    is response-scoped, not offer-scoped).
    """
    fare_details_by_segment = _fare_details_by_segment_id(offer)
    legs = tuple(
        map_leg(itinerary, fare_details_by_segment) for itinerary in offer["itineraries"]
    )
    # spikes/mapping_sketch.md S4.2: always read `grandTotal`, never `total`
    # -- they only happen to be equal in the fixture (README item 5).
    price = Money(
        amount=Decimal(offer["price"]["grandTotal"]), currency=offer["price"]["currency"]
    )

    return RawOffer(
        provider="amadeus",
        provider_offer_id=offer["id"],
        legs=legs,
        price=price,
        # spikes/mapping_sketch.md S3.7: Amadeus has no offer-expiry
        # equivalent. `lastTicketingDate`/`lastTicketingDateTime` are the
        # AIRLINE's ticketing deadline (days/weeks out), not "this quote is
        # stale after" -- mapping either into offer_expires_at would mark a
        # fare valid for months. Leave it None; the TTL ladder handles it.
        offer_expires_at=None,
        raw_payload_ref=raw_payload_ref,
        # finding 0.6 CONFIRMED (spikes/mapping_sketch.md S3.8): no
        # consumer booking URL exists anywhere in this response shape.
        provider_booking_url=None,
    )


def _checked_baggage_state(fare_detail: dict[str, Any]) -> BaggageState:
    """spikes/mapping_sketch.md S3.2: two mutually exclusive shapes
    (``{"quantity": N}`` or ``{"weight": N, "weightUnit": "KG"}``), plus a
    genuinely absent key meaning "provider didn't say" -- never collapsed
    into ``not_included``."""
    included = fare_detail.get("includedCheckedBags")
    if included is None:
        return "unknown"
    quantity = included.get("quantity")
    if quantity is not None:
        return "included" if quantity > 0 else "not_included"
    weight = included.get("weight")
    if weight is not None:
        return "included" if weight > 0 else "not_included"
    return "unknown"


def _refundable_state(offer: dict[str, Any]) -> RefundableState:
    pricing_options = offer.get("pricingOptions")
    if pricing_options is None or "refundableFare" not in pricing_options:
        return "unknown"
    return "allowed" if pricing_options["refundableFare"] else "not_allowed"


def extract_fare_option(offer: dict[str, Any], *, price: Money) -> FareOption:
    """One Amadeus offer's fare-brand/baggage/refundability detail ->
    ``FareOption`` (spikes/mapping_sketch.md S3.1/S3.2/S3.6) -- the tri-state
    gap analysis exists specifically to close.

    ``price`` is supplied by the caller (e.g. the same ``Money`` ``map_offer``
    produced, or its FX-converted successor once normalize/dedup builds the
    real dedup survivor) rather than re-read from ``offer`` here, so the
    result always reflects whatever price the caller is actually tracking
    this fare option against -- ``RawOffer`` itself has no ``fare_options``
    field (that lives only on ``NormalizedItinerary``, built post-dedup), so
    this is a standalone helper a future normalize/dedup caller assembles
    from, not something ``map_offer`` embeds directly.
    """
    fare_details_by_segment = _fare_details_by_segment_id(offer)
    first_detail = next(iter(fare_details_by_segment.values()))
    fare_brand = first_detail.get("brandedFareLabel") or first_detail.get("brandedFare")

    return FareOption(
        price=price,
        fare_brand=fare_brand,
        checked_baggage=_checked_baggage_state(first_detail),
        refundable=_refundable_state(offer),
        provider_offer_id=offer["id"],
    )
