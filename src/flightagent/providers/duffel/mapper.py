"""Duffel v2 -> domain mapping.

The REAL, working piece of T49 (unlike ``provider.py``/``auth.py``/
``errors.py``'s stubs in this same package): a pure-function pipeline that
turns one already-parsed Duffel offer dict (a ``data[]`` element of
``tests/fixtures/providers/duffel_offers_sample.json``'s
``offers_list_response`` -- see that fixture's own ``_fixture_note``: "a real
capture of either endpoint is only the inner object") into a ``RawOffer``,
plus a companion ``extract_fare_option`` for the same fare-brand/baggage/
refundability detail ``amadeus/mapper.py`` closes (spikes/mapping_sketch.md
S3.1/S3.2/S3.6).

Duffel's payload shape differs from Amadeus's in several ways this module
has to absorb (spikes/mapping_sketch.md S2, read in full before touching
this file):

- Cabin lives at ``segments[].passengers[].cabin_class``, lower-case,
  already spelled identically to ``CabinClass``'s own values -- a join by
  passenger position, not by a ``segmentId`` back-reference like Amadeus.
- Duffel gives BOTH a marketing and an operating flight number
  (``marketing_carrier_flight_number`` / ``operating_carrier_flight_number``)
  -- Amadeus can only ever give the operating carrier CODE, never its flight
  number (S2.1).
- Technical stops are `len(segment["stops"])`, not a `numberOfStops` field.
- The offer price (`total_amount`/`total_currency`) is NOT forced to EUR --
  D14/S2.4: fixture offer `...0003` is genuinely USD in an otherwise-EUR
  result set, and this mapper must carry that through verbatim.
- Refundability (`conditions.refund_before_departure.allowed`) is genuinely
  tri-state: `true`/`false`/`null`, where `null` means "the airline didn't
  say", never `false` (S3.6).
- Every airport object inline-carries a `time_zone` -- a consistency CHECK
  against the project's own catalog, never the authority (S1.1/S4.3): "Do
  not trust Duffel's inline time_zone ... provider data is untrusted input
  (S8.1)".
"""

from __future__ import annotations

import logging
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

_logger = logging.getLogger(__name__)

_DURATION_ADAPTER: TypeAdapter[timedelta] = TypeAdapter(timedelta)

BaggageState = Literal["included", "not_included", "unknown"]
RefundableState = Literal["allowed", "not_allowed", "unknown"]

_HUB_TZ_FALLBACK: dict[str, str] = {
    # Identical rationale and identical table to
    # amadeus/mapper.py._HUB_TZ_FALLBACK -- kept as an independent copy
    # rather than a shared import, matching providers/base.py's own design
    # philosophy that the Amadeus and Duffel adapters "never need to share
    # a common base class, only the same callable shape".
    "DXB": "Asia/Dubai",
    "IST": "Europe/Istanbul",
    "DOH": "Asia/Qatar",
}


class UnmappableDuffelOfferError(ValueError):
    """Raised when a Duffel offer dict is missing a field this mapper
    requires, names an IATA code no zone can be resolved for, or states a
    duration that disagrees with its own endpoints. Never silently skipped
    (master plan S4).
    """


def _resolve_iana_zone(iata: str, *, duffel_hint: str | None) -> str:
    """Project catalog first, hub fallback second -- identical resolution
    order to ``amadeus/mapper.py``'s own ``_resolve_iana_zone``.
    ``duffel_hint`` (the provider's own inline ``time_zone``) is
    cross-checked against the resolved value and a mismatch is LOGGED,
    never trusted as the authority (spikes/mapping_sketch.md S1.1/S4.3).

    This is a plain ``logging`` warning, not a structured ``log_event``:
    ``observability.events.EventName`` is a closed enum (T6) with no
    existing member for "provider tz data disagreed with our catalog", and
    extending that shared, closed schema is out of scope for this task.
    """
    try:
        resolved = airport_registry.get(iata).iana_tz
    except airport_registry.UnknownAirportError:
        try:
            resolved = _HUB_TZ_FALLBACK[iata]
        except KeyError:
            raise UnmappableDuffelOfferError(
                f"no IANA zone known for IATA code {iata!r} -- not in config/airports.yaml "
                f"and not in this mapper's hub fallback table"
            ) from None

    if duffel_hint is not None and duffel_hint != resolved:
        _logger.warning(
            "Duffel-supplied time_zone %r for %s disagrees with catalog zone %r -- using "
            "the catalog; provider tz data is untrusted input (mapping_sketch.md S8.1)",
            duffel_hint,
            iata,
            resolved,
        )
    return resolved


def parse_iso8601_duration(value: str) -> timedelta:
    """Parse a Duffel ISO-8601 duration string (``PT6H25M``, ``PT15H25M``,
    ``PT1H0M``, ...) via pydantic's own ``timedelta`` coercion -- see
    ``amadeus/mapper.py``'s identical helper for the full rationale against
    hand-rolling a regex.
    """
    return _DURATION_ADAPTER.validate_python(value)


def _parse_naive_local(value: str) -> datetime:
    """Parse an offsetless local wall-clock string
    (``"2027-07-17T14:55:00"``, Duffel's ``departing_at``/``arriving_at``)
    into a NAIVE ``datetime``. Raises if it unexpectedly carries an offset.
    """
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is not None:
        raise UnmappableDuffelOfferError(
            f"expected an offsetless local timestamp, got one carrying tzinfo: {value!r}"
        )
    return parsed


def _parse_utc_timestamp(value: str) -> datetime:
    """Duffel metadata timestamps (``expires_at``, ``created_at``, ...) are
    UTC with a ``Z`` suffix -- a DIFFERENT shape from
    ``departing_at``/``arriving_at``'s offsetless local strings
    (spikes/mapping_sketch.md S2.5/S4.4). ``datetime.fromisoformat`` accepts
    the ``Z`` suffix natively as of Python 3.11+.
    """
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise UnmappableDuffelOfferError(
            f"expected a UTC timestamp carrying an offset, got a naive one: {value!r}"
        )
    return parsed.astimezone(UTC)


def _to_utc(local_naive: datetime, zone: ZoneInfo, *, fold: int = 0) -> datetime:
    return local_naive.replace(tzinfo=zone, fold=fold).astimezone(UTC)


_CABIN_BY_DUFFEL_VALUE: dict[str, CabinClass] = {
    "economy": CabinClass.ECONOMY,
    "premium_economy": CabinClass.PREMIUM_ECONOMY,
    "business": CabinClass.BUSINESS,
    "first": CabinClass.FIRST,
}


def _cabin_for_passenger(passenger: dict[str, Any]) -> CabinClass:
    """spikes/mapping_sketch.md S2.2: Duffel's ``cabin_class`` is already
    lower-case and already spelled identically to ``CabinClass``'s own
    values -- unlike Amadeus, no case-folding is needed, only validation
    that the value is one this project's closed enum actually recognises.
    """
    raw_cabin = passenger["cabin_class"]
    try:
        return _CABIN_BY_DUFFEL_VALUE[raw_cabin]
    except KeyError:
        raise UnmappableDuffelOfferError(f"unrecognised Duffel cabin_class {raw_cabin!r}") from None


def map_segment(raw_segment: dict[str, Any]) -> Segment:
    """One ``slices[].segments[]`` element -> one ``Segment``."""
    origin = raw_segment["origin"]["iata_code"]
    destination = raw_segment["destination"]["iata_code"]
    origin_tz = _resolve_iana_zone(origin, duffel_hint=raw_segment["origin"].get("time_zone"))
    destination_tz = _resolve_iana_zone(
        destination, duffel_hint=raw_segment["destination"].get("time_zone")
    )
    origin_zone = ZoneInfo(origin_tz)
    destination_zone = ZoneInfo(destination_tz)

    depart_local_naive = _parse_naive_local(raw_segment["departing_at"])
    arrive_local_naive = _parse_naive_local(raw_segment["arriving_at"])
    depart_utc = _to_utc(depart_local_naive, origin_zone)
    arrive_utc = _to_utc(arrive_local_naive, destination_zone)

    passenger = raw_segment["passengers"][0]  # D3: one adult, always index 0

    return Segment(
        segment_id=raw_segment["id"],
        origin=origin,
        destination=destination,
        depart_utc=depart_utc,
        arrive_utc=arrive_utc,
        depart_local=depart_local_naive.replace(tzinfo=origin_zone),
        arrive_local=arrive_local_naive.replace(tzinfo=destination_zone),
        origin_tz=origin_tz,
        destination_tz=destination_tz,
        marketing_carrier=raw_segment["marketing_carrier"]["iata_code"],
        operating_carrier=raw_segment["operating_carrier"]["iata_code"],
        flight_number=raw_segment["marketing_carrier_flight_number"],
        operating_flight_number=raw_segment.get("operating_carrier_flight_number"),
        cabin=_cabin_for_passenger(passenger),
        technical_stops=len(raw_segment.get("stops", [])),
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


def map_leg(raw_slice: dict[str, Any]) -> Leg:
    """One ``slices[]`` element -> one ``Leg``."""
    segments = tuple(map_segment(raw_segment) for raw_segment in raw_slice["segments"])
    layovers = tuple(
        _build_layover(prev, nxt) for prev, nxt in zip(segments, segments[1:])  # noqa: B905
    )
    leg = Leg(segments=segments, layovers=layovers)

    stated_duration = parse_iso8601_duration(raw_slice["duration"])
    if leg.total_duration != stated_duration:
        raise UnmappableDuffelOfferError(
            f"slice duration {stated_duration} does not match endpoints-derived "
            f"{leg.total_duration}"
        )
    return leg


def map_offer(offer: dict[str, Any], *, raw_payload_ref: str) -> RawOffer:
    """One Duffel ``data[]`` element (from ``offers_list_response``) -> one
    ``RawOffer``.

    ``raw_payload_ref`` is supplied by the caller -- this module has no
    cache layer of its own.
    """
    legs = tuple(map_leg(raw_slice) for raw_slice in offer["slices"])
    # D14/spikes/mapping_sketch.md S2.4: NEVER force EUR here -- Duffel
    # returns the currency the airline actually priced in, and a single
    # result set can be genuinely mixed-currency (fixture offer ...0003 is
    # USD).
    price = Money(amount=Decimal(offer["total_amount"]), currency=offer["total_currency"])

    expires_at_raw = offer.get("expires_at")
    offer_expires_at = _parse_utc_timestamp(expires_at_raw) if expires_at_raw is not None else None

    return RawOffer(
        provider="duffel",
        provider_offer_id=offer["id"],
        legs=legs,
        price=price,
        offer_expires_at=offer_expires_at,
        raw_payload_ref=raw_payload_ref,
        # finding 0.6 CONFIRMED + spikes/mapping_sketch.md S3.8/S3.9: no
        # consumer booking URL exists (only logo/T&C links, deliberately
        # never carried into the domain), and passenger-identity/name
        # fields are dropped at this boundary too, simply by never being
        # read anywhere in this module (keeps the master plan S8.3 CI
        # denylist over the literal string "passport" a sharp control,
        # never an exclusion path around it).
        provider_booking_url=None,
    )


def _checked_baggage_state(passenger: dict[str, Any]) -> BaggageState:
    """spikes/mapping_sketch.md S3.2: Duffel's ``baggages`` list has a
    ``"checked"`` entry with an explicit ``quantity`` (possibly ``0`` -- a
    genuine hand-baggage-only fare, fixture offer ``...0003``) or, in
    principle, no ``"checked"`` entry at all -- genuinely unknown, never
    assumed to mean zero.
    """
    for bag in passenger.get("baggages", []):
        if bag.get("type") == "checked":
            quantity = bag.get("quantity")
            if quantity is None:
                return "unknown"
            return "included" if quantity > 0 else "not_included"
    return "unknown"


def _refundable_state(offer: dict[str, Any]) -> RefundableState:
    """spikes/mapping_sketch.md S3.6: Duffel's ``allowed`` is genuinely
    tri-state -- ``true``, ``false``, and ``null`` meaning "the airline
    didn't tell us". ``null`` must NEVER collapse to ``false`` (that would
    invent a restriction that may not exist).
    """
    refund = offer.get("conditions", {}).get("refund_before_departure")
    if refund is None:
        return "unknown"
    allowed = refund.get("allowed")
    if allowed is None:
        return "unknown"
    return "allowed" if allowed else "not_allowed"


def extract_fare_option(offer: dict[str, Any], *, price: Money) -> FareOption:
    """One Duffel offer's fare-brand/baggage/refundability detail ->
    ``FareOption`` -- see ``amadeus/mapper.py``'s identical function for why
    ``price`` is caller-supplied rather than re-derived here.
    """
    first_slice = offer["slices"][0]
    first_segment_passenger = first_slice["segments"][0]["passengers"][0]
    fare_brand = first_slice.get("fare_brand_name")

    return FareOption(
        price=price,
        fare_brand=fare_brand,
        checked_baggage=_checked_baggage_state(first_segment_passenger),
        refundable=_refundable_state(offer),
        provider_offer_id=offer["id"],
    )
