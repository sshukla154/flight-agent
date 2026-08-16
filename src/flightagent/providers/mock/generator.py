"""Seeded, per-request synthetic offer generation for ``MockProvider``'s
programmatic mode (master plan S5, T10).

Master plan S5's "one subtle determinism trap": the mock generator must be
seeded from a hash of the search request, never a module-level
``random.Random`` -- a shared RNG across a 160-way concurrent fan-out would
make the "deterministic" mock run depend on coroutine interleaving instead
of on the request itself. Every public entry point below builds a fresh,
call-scoped ``random.Random`` (see ``compute_seed``/``generate_offers``)
and nothing in this module ever touches the ``random`` module's global
state (no bare ``random.random()``/``random.choice()`` calls) or stores an
RNG instance anywhere it could be reused across requests.

Offers are built by constructing ``Segment``/``Layover``/``Leg``/``RawOffer``
domain objects directly (this task's explicit scope) -- there is no JSON
parsing here at all, unlike the fixture-file mode in ``provider.py``.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from random import Random
from zoneinfo import ZoneInfo

from flightagent.domain.enums import CabinClass
from flightagent.domain.itinerary import Leg, RawOffer
from flightagent.domain.money import Money
from flightagent.domain.run import SearchRequest
from flightagent.domain.segment import Layover, Segment

# IANA zone per IATA code. Covers the 10 European origins + 8 Indian
# destinations this project ever searches (config/airports.yaml) plus the
# three hub airports a one-stop connection may route through. Hardcoded
# here rather than read from config/airports.yaml at runtime: the mock
# provider is a permanent, zero-I/O code path (D6), and every offer this
# module produces is clearly synthetic regardless of where the zone table
# lives.
_AIRPORT_TZ: dict[str, str] = {
    # European origins
    "AMS": "Europe/Amsterdam",
    "EIN": "Europe/Amsterdam",
    "RTM": "Europe/Amsterdam",
    "MST": "Europe/Amsterdam",
    "GRQ": "Europe/Amsterdam",
    "DUS": "Europe/Berlin",
    "NRN": "Europe/Berlin",
    "CGN": "Europe/Berlin",
    "BRU": "Europe/Brussels",
    "CRL": "Europe/Brussels",
    # Indian destinations -- one timezone nationwide, no DST.
    "DEL": "Asia/Kolkata",
    "BOM": "Asia/Kolkata",
    "BLR": "Asia/Kolkata",
    "HYD": "Asia/Kolkata",
    "MAA": "Asia/Kolkata",
    "CCU": "Asia/Kolkata",
    "LKO": "Asia/Kolkata",
    "VNS": "Asia/Kolkata",
    # Hub airports a one-stop connection may route through.
    "DXB": "Asia/Dubai",
    "IST": "Europe/Istanbul",
    "DOH": "Asia/Qatar",
}

# Destination -> preferred connecting hub. "Your call" per the task brief;
# these are real, plausible Europe-India one-stop transit hubs (Emirates
# via DXB, Turkish via IST, Qatar Airways via DOH all fly exactly these
# connections) -- AMS->DEL routes through DXB, matching the task brief's
# own example.
_HUB_FOR_DESTINATION: dict[str, str] = {
    "DEL": "DXB",
    "BOM": "DXB",
    "BLR": "DOH",
    "HYD": "DOH",
    "MAA": "DOH",
    "CCU": "IST",
    "LKO": "IST",
    "VNS": "IST",
}

_ALTERNATE_HUBS: tuple[str, ...] = ("DXB", "IST", "DOH")

# The single marketing carrier operating the through-connection at each hub.
# A real one-stop fare sold through a single hub is almost always one
# airline's own connection, not a mixed-carrier itinerary, so both segments
# of a generated one-stop offer share this carrier.
_CARRIER_FOR_HUB: dict[str, str] = {"DXB": "EK", "IST": "TK", "DOH": "QR"}

_DIRECT_CARRIERS: tuple[str, ...] = ("AI", "9W", "6E")

_ONE_HUNDRED = Decimal(100)

_NO_DIRECT_SERVICE_DESTINATIONS: frozenset[str] = frozenset({"VNS"})
"""Destinations with no real nonstop Europe-India service (Phase 5, T29).

Varanasi (VNS) is a regional/pilgrimage-city airport: every real-world
itinerary from a European origin connects through DEL/BOM or a Gulf hub
(DXB/IST/DOH) -- there is no genuine direct flight to reproduce here.
Before this change every destination's ``max_stops=0`` search fabricated
two direct offers regardless of real-world route existence, which made it
impossible for Phase 5's own Direct Flight Analysis table to ever show a
``NOT_AVAILABLE`` row (D10) -- ``_generate_direct_offers`` below returns
``()`` for any destination in this set, a legitimate "no direct service"
answer, never an error. One-stop searches for these destinations
(``_generate_one_stop_offers``) are entirely unaffected -- VNS genuinely
is reachable one-stop through a hub, exactly like every other destination.
"""


def _zone_for(iata: str) -> str:
    try:
        return _AIRPORT_TZ[iata]
    except KeyError as exc:
        raise ValueError(
            f"mock generator has no IANA zone for IATA code {iata!r} -- add it to "
            f"generator._AIRPORT_TZ (never default to UTC, master plan S4)"
        ) from exc


def compute_seed(request: SearchRequest) -> int:
    """Derive a deterministic seed from exactly the request's canonical
    search fields: origin, destination, departure_date, cabin, max_stops,
    adults, currency.

    Deliberately NOT provider name or api_version -- those feed the
    two-layer *cache* key in master plan S5, a different key for a
    different purpose. Two ``SearchRequest``s with identical canonical
    fields always produce the same seed, and therefore identical offers
    from ``generate_offers``, regardless of call order or concurrency.
    """
    canonical = "|".join(
        (
            request.origin,
            request.destination,
            request.departure_date.isoformat(),
            request.cabin.value,
            str(request.max_stops),
            str(request.adults),
            request.currency,
        )
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big")


def generate_offers(request: SearchRequest) -> tuple[RawOffer, ...]:
    """Produce a small, deterministic set of synthetic offers for one
    search request.

    A fresh ``random.Random(compute_seed(request))`` is created here, used
    for this call only, and never stored or shared (see module docstring).
    For ``max_stops == 1``, the first returned offer always has a layover
    strictly inside ``[request.layover_min, request.layover_max]``, so a
    one-stop 3-6h layover validation always has at least one itinerary to
    accept -- master plan S5's stated reason this matters: it's the offer
    the CLI needs to actually recommend something.
    """
    seed = compute_seed(request)
    rng = Random(seed)
    if request.max_stops == 0:
        return _generate_direct_offers(request, rng)
    return _generate_one_stop_offers(request, rng)


def _generate_direct_offers(request: SearchRequest, rng: Random) -> tuple[RawOffer, ...]:
    if request.destination in _NO_DIRECT_SERVICE_DESTINATIONS:
        # Legitimate "no direct service exists" answer (D10's NOT_AVAILABLE
        # tier, master plan/T29) -- not an error, not a shortage of RNG
        # draws, just nothing to return. See _NO_DIRECT_SERVICE_DESTINATIONS'
        # own docstring above.
        return ()

    depart_times = (time(7, 30), time(13, 45))
    offers = []
    origin_zone = _zone_for(request.origin)
    for index, local_depart in enumerate(depart_times):
        duration = timedelta(hours=8, minutes=rng.randint(0, 90))
        segment = _build_segment(
            origin=request.origin,
            destination=request.destination,
            depart_utc=_local_to_utc(request.departure_date, local_depart, origin_zone),
            duration=duration,
            marketing_carrier=rng.choice(_DIRECT_CARRIERS),
            flight_number=str(rng.randint(100, 999)),
            cabin=request.cabin,
            segment_id=f"{request.origin}-{request.destination}-{index}",
        )
        leg = Leg(segments=(segment,), layovers=())
        offers.append(_build_offer(request, rng, legs=(leg,), index=index))
    return tuple(offers)


def _generate_one_stop_offers(request: SearchRequest, rng: Random) -> tuple[RawOffer, ...]:
    min_minutes = int(request.layover_min.total_seconds() // 60)
    max_minutes = int(request.layover_max.total_seconds() // 60)
    primary_hub = _HUB_FOR_DESTINATION.get(request.destination, _ALTERNATE_HUBS[0])
    guaranteed_layover_minutes = min_minutes + (max_minutes - min_minutes) // 2

    offers = [
        _build_one_stop_offer(
            request,
            rng,
            hub=primary_hub,
            layover_minutes=guaranteed_layover_minutes,
            outbound_local_depart=time(9, 0),
            index=0,
        )
    ]

    # A second, freely-randomized offer for variety. This generator only
    # GUARANTEES the first offer's layover satisfies the request's window;
    # this one's layover is drawn from `rng` and may land outside it --
    # deliberately, so two different requests (or two different max_stops
    # modes) don't just return the same guaranteed offer twice.
    alternate_hub = rng.choice(_ALTERNATE_HUBS)
    jitter_low = max(30, min_minutes - 120)
    jitter_high = max_minutes + 120
    alternate_layover_minutes = rng.randint(jitter_low, jitter_high)
    offers.append(
        _build_one_stop_offer(
            request,
            rng,
            hub=alternate_hub,
            layover_minutes=alternate_layover_minutes,
            outbound_local_depart=time(14, 30),
            index=1,
        )
    )
    return tuple(offers)


def _build_one_stop_offer(
    request: SearchRequest,
    rng: Random,
    *,
    hub: str,
    layover_minutes: int,
    outbound_local_depart: time,
    index: int,
) -> RawOffer:
    carrier = _CARRIER_FOR_HUB[hub]
    outbound_duration = timedelta(hours=6, minutes=rng.randint(0, 90))
    inbound_duration = timedelta(hours=3, minutes=rng.randint(0, 90))

    outbound_depart_utc = _local_to_utc(
        request.departure_date, outbound_local_depart, _zone_for(request.origin)
    )
    outbound_segment = _build_segment(
        origin=request.origin,
        destination=hub,
        depart_utc=outbound_depart_utc,
        duration=outbound_duration,
        marketing_carrier=carrier,
        flight_number=str(rng.randint(100, 999)),
        cabin=request.cabin,
        segment_id=f"{request.origin}-{hub}-{index}",
    )

    inbound_depart_utc = outbound_segment.arrive_utc + timedelta(minutes=layover_minutes)
    inbound_segment = _build_segment(
        origin=hub,
        destination=request.destination,
        depart_utc=inbound_depart_utc,
        duration=inbound_duration,
        marketing_carrier=carrier,
        flight_number=str(rng.randint(100, 999)),
        cabin=request.cabin,
        segment_id=f"{hub}-{request.destination}-{index}",
    )

    layover = _build_layover(
        airport=hub,
        arrive_utc=outbound_segment.arrive_utc,
        depart_utc=inbound_segment.depart_utc,
    )
    leg = Leg(segments=(outbound_segment, inbound_segment), layovers=(layover,))
    return _build_offer(request, rng, legs=(leg,), index=index)


def _build_segment(
    *,
    origin: str,
    destination: str,
    depart_utc: datetime,
    duration: timedelta,
    marketing_carrier: str,
    flight_number: str,
    cabin: CabinClass,
    segment_id: str,
) -> Segment:
    arrive_utc = depart_utc + duration
    origin_tz = _zone_for(origin)
    destination_tz = _zone_for(destination)
    return Segment(
        segment_id=segment_id,
        origin=origin,
        destination=destination,
        depart_utc=depart_utc,
        arrive_utc=arrive_utc,
        depart_local=depart_utc.astimezone(ZoneInfo(origin_tz)),
        arrive_local=arrive_utc.astimezone(ZoneInfo(destination_tz)),
        origin_tz=origin_tz,
        destination_tz=destination_tz,
        marketing_carrier=marketing_carrier,
        flight_number=flight_number,
        cabin=cabin,
        duration=duration,
    )


def _build_layover(*, airport: str, arrive_utc: datetime, depart_utc: datetime) -> Layover:
    zone = ZoneInfo(_zone_for(airport))
    return Layover(
        airport=airport,
        arrive_utc=arrive_utc,
        depart_utc=depart_utc,
        duration=depart_utc - arrive_utc,
        local_window=(arrive_utc.astimezone(zone), depart_utc.astimezone(zone)),
        requires_airport_change=False,
        requires_terminal_change=False,
    )


def _build_offer(
    request: SearchRequest, rng: Random, *, legs: tuple[Leg, ...], index: int
) -> RawOffer:
    seed_hex = format(compute_seed(request), "x")
    offer_id = f"mock-{seed_hex}-{index}"
    return RawOffer(
        provider="mock",
        provider_offer_id=offer_id,
        legs=legs,
        price=_random_price(rng, currency=request.currency),
        raw_payload_ref=f"mock://{request.origin}-{request.destination}-s{request.max_stops}/{offer_id}",
        provider_booking_url=f"https://mock-booking.example.test/{offer_id}",
    )


def _local_to_utc(day: date, local_time: time, zone_key: str) -> datetime:
    naive = datetime.combine(day, local_time)
    return naive.replace(tzinfo=ZoneInfo(zone_key)).astimezone(UTC)


def _random_price(rng: Random, *, currency: str) -> Money:
    # EUR550.00 - EUR950.00: master plan S0 cites this as the routine
    # mid-July AMS->India one-stop economy band.
    cents = rng.randint(55000, 95000)
    return Money(amount=Decimal(cents) / _ONE_HUNDRED, currency=currency)
