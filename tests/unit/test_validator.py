"""Tests for ``flightagent.validation`` (Phase 3 / T12 — v1 subset).

Master plan finding 0.4 and DECISIONS.md D8/D11/D13 are the authority for
every boundary asserted here:

- D8: layover validity is the CLOSED interval ``[180, 360]`` minutes,
  computed on UTC-elapsed time. 179 and 361 are rejected; 180 and 360 are
  accepted.
- D13: a direct itinerary (``stop_count == 0``) is not subject to the
  layover rule AT ALL — getting this wrong makes every direct itinerary
  unvalidatable, which is why it gets its own test rather than being
  folded into the boundary table.
- D11: the departure date is evaluated in the ORIGIN's LOCAL date, never
  UTC. ``TestDepartureDateOriginLocal.test_local_date_after_utc_date_is_rejected``
  is the single most important test in this file — see its docstring.

Also proves the engine's core contract: it never short-circuits on the
first failing rule (``TestEngineAccumulatesAllRejections``).

T18 (Phase 3) extends this file, in place, with one confirming test per
rule it added: ``TestDestinationMismatchRule`` (the missing counterpart
to origin match), ``TestLocalTimeValidityRule`` (DST-ambiguous/nonexistent
local times, using real confirmed 2027 EU DST transition instants),
``TestSelfTransferRule`` (D5), and ``TestMissingTimezoneUnreachable``
(the investigation conclusion that ``RejectionCode.MISSING_TIMEZONE`` has
no reachable path at this layer, proven rather than assumed). The
exhaustive boundary suite for all of these is T21's job, not this file's.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

import pytest

from flightagent.airports.registry import UnknownAirportError
from flightagent.domain.enums import CabinClass, RejectionCode
from flightagent.domain.itinerary import Leg, NormalizedItinerary, RawOffer
from flightagent.domain.money import Money
from flightagent.domain.run import SearchRequest
from flightagent.domain.segment import Layover, Segment
from flightagent.normalize.builder import build_normalized_itinerary
from flightagent.normalize.timezones import zone_for
from flightagent.validation.engine import validate

_FARE_AS_OF = datetime(2027, 7, 1, 12, 0, tzinfo=UTC)
_DEFAULT_LAYOVER_MIN = timedelta(minutes=180)
_DEFAULT_LAYOVER_MAX = timedelta(minutes=360)


def _segment(
    *,
    segment_id: str,
    origin: str,
    destination: str,
    depart_utc: datetime,
    arrive_utc: datetime,
    origin_tz: str,
    destination_tz: str,
) -> Segment:
    origin_zone = ZoneInfo(origin_tz)
    destination_zone = ZoneInfo(destination_tz)
    return Segment(
        segment_id=segment_id,
        origin=origin,
        destination=destination,
        depart_utc=depart_utc,
        arrive_utc=arrive_utc,
        depart_local=depart_utc.astimezone(origin_zone),
        arrive_local=arrive_utc.astimezone(destination_zone),
        origin_tz=origin_tz,
        destination_tz=destination_tz,
        marketing_carrier="EK",
        flight_number="1",
        cabin=CabinClass.ECONOMY,
        duration=arrive_utc - depart_utc,
    )


def _direct_leg(
    *,
    origin: str = "AMS",
    destination: str = "DEL",
    depart_utc: datetime,
    duration: timedelta = timedelta(hours=8),
    origin_tz: str = "Europe/Amsterdam",
    destination_tz: str = "Asia/Kolkata",
) -> Leg:
    """A single-segment (``stop_count == 0``) leg — by construction, its
    ``layovers`` tuple is empty (``Leg._validate_shape`` requires
    ``len(layovers) == len(segments) - 1``)."""
    arrive_utc = depart_utc + duration
    segment = _segment(
        segment_id="direct",
        origin=origin,
        destination=destination,
        depart_utc=depart_utc,
        arrive_utc=arrive_utc,
        origin_tz=origin_tz,
        destination_tz=destination_tz,
    )
    return Leg(segments=(segment,), layovers=())


def _one_stop_leg(*, layover_minutes: int) -> Leg:
    """AMS -> DXB -> DEL, ``stop_count == 1``, with a controllable
    UTC-elapsed layover at DXB — the fixture the D8 boundary table drives.
    """
    depart_utc = datetime(2027, 7, 17, 10, 0, tzinfo=UTC)
    inbound_arrive = depart_utc + timedelta(hours=4)
    outbound_depart = inbound_arrive + timedelta(minutes=layover_minutes)
    outbound_arrive = outbound_depart + timedelta(hours=3)

    inbound = _segment(
        segment_id="ams-dxb",
        origin="AMS",
        destination="DXB",
        depart_utc=depart_utc,
        arrive_utc=inbound_arrive,
        origin_tz="Europe/Amsterdam",
        destination_tz="Asia/Dubai",
    )
    outbound = _segment(
        segment_id="dxb-del",
        origin="DXB",
        destination="DEL",
        depart_utc=outbound_depart,
        arrive_utc=outbound_arrive,
        origin_tz="Asia/Dubai",
        destination_tz="Asia/Kolkata",
    )
    layover = Layover(
        airport="DXB",
        arrive_utc=inbound_arrive,
        depart_utc=outbound_depart,
        duration=outbound_depart - inbound_arrive,
        local_window=(inbound.arrive_local, outbound.depart_local),
        requires_airport_change=False,
        requires_terminal_change=False,
    )
    return Leg(segments=(inbound, outbound), layovers=(layover,))


def _one_stop_leg_with_self_transfer(*, layover_minutes: int = 240) -> Leg:
    """Same shape as ``_one_stop_leg`` (AMS -> DXB -> DEL), but the DXB
    layover is flagged ``is_self_transfer=True`` -- the D5 fixture
    ``check_self_transfer`` exists to catch. ``layover_minutes`` defaults
    inside the ordinary [180, 360] window on purpose, so a rejection here
    can only be attributed to D5, never to ``check_layover_window``.
    """
    depart_utc = datetime(2027, 7, 17, 10, 0, tzinfo=UTC)
    inbound_arrive = depart_utc + timedelta(hours=4)
    outbound_depart = inbound_arrive + timedelta(minutes=layover_minutes)
    outbound_arrive = outbound_depart + timedelta(hours=3)

    inbound = _segment(
        segment_id="ams-dxb",
        origin="AMS",
        destination="DXB",
        depart_utc=depart_utc,
        arrive_utc=inbound_arrive,
        origin_tz="Europe/Amsterdam",
        destination_tz="Asia/Dubai",
    )
    outbound = _segment(
        segment_id="dxb-del",
        origin="DXB",
        destination="DEL",
        depart_utc=outbound_depart,
        arrive_utc=outbound_arrive,
        origin_tz="Asia/Dubai",
        destination_tz="Asia/Kolkata",
    )
    layover = Layover(
        airport="DXB",
        arrive_utc=inbound_arrive,
        depart_utc=outbound_depart,
        duration=outbound_depart - inbound_arrive,
        local_window=(inbound.arrive_local, outbound.depart_local),
        requires_airport_change=False,
        requires_terminal_change=False,
        is_self_transfer=True,
    )
    return Leg(segments=(inbound, outbound), layovers=(layover,))


def _direct_leg_with_local_time(
    *,
    depart_naive_local: datetime,
    depart_fold: int,
    origin: str = "AMS",
    origin_tz: str = "Europe/Amsterdam",
    destination: str = "DEL",
    destination_tz: str = "Asia/Kolkata",
    duration: timedelta = timedelta(hours=8),
) -> Leg:
    """A direct leg whose DEPARTURE local time is deliberately an
    ambiguous (DST fall-back) or nonexistent (DST spring-forward) wall
    clock reading, per an explicit ``(depart_naive_local, depart_fold)``
    pair -- ``check_local_time_validity`` exists to catch this.

    Unlike ``_segment``, this does NOT derive ``depart_local`` via
    ``depart_utc.astimezone(zone)`` -- that always produces a "normal"
    reading (it is, by construction, the ONE actual instant in time), so
    it can never exercise this rule. Instead ``depart_local`` is built
    directly from the naive local reading plus an explicit fold, exactly
    as ``Segment._resolve_utc`` (domain/segment.py) does internally, and
    ``depart_utc`` is derived FROM that (not the other way around) so the
    two stay mutually consistent per ``Segment``'s own construction-time
    invariant. ``arrive_local`` is computed the ordinary
    (always-"normal") way, so exactly one field is the one under test.
    """
    origin_zone = ZoneInfo(origin_tz)
    destination_zone = ZoneInfo(destination_tz)
    depart_local = depart_naive_local.replace(tzinfo=origin_zone, fold=depart_fold)
    depart_utc = depart_local.astimezone(UTC)
    arrive_utc = depart_utc + duration
    arrive_local = arrive_utc.astimezone(destination_zone)
    segment = Segment(
        segment_id="direct",
        origin=origin,
        destination=destination,
        depart_utc=depart_utc,
        arrive_utc=arrive_utc,
        depart_local=depart_local,
        arrive_local=arrive_local,
        origin_tz=origin_tz,
        destination_tz=destination_tz,
        marketing_carrier="EK",
        flight_number="1",
        cabin=CabinClass.ECONOMY,
        duration=duration,
        depart_fold=depart_fold,
    )
    return Leg(segments=(segment,), layovers=())


def _itinerary_from_leg(leg: Leg, *, provider_offer_id: str = "offer-1") -> NormalizedItinerary:
    raw_offer = RawOffer(
        provider="mock",
        provider_offer_id=provider_offer_id,
        legs=(leg,),
        price=Money(amount=Decimal("650.00"), currency="EUR"),
        offer_expires_at=None,
        raw_payload_ref=f"mock:{provider_offer_id}",
        provider_booking_url=None,
    )
    return build_normalized_itinerary(
        raw_offer, adults=1, cabin=CabinClass.ECONOMY, fare_as_of=_FARE_AS_OF
    )


def _request(
    *,
    origin: str = "AMS",
    destination: str = "DEL",
    departure_date: date = date(2027, 7, 17),
    max_stops: Literal[0, 1] = 1,
    layover_min: timedelta = _DEFAULT_LAYOVER_MIN,
    layover_max: timedelta = _DEFAULT_LAYOVER_MAX,
) -> SearchRequest:
    return SearchRequest(
        origin=origin,
        destination=destination,
        departure_date=departure_date,
        cabin=CabinClass.ECONOMY,
        max_stops=max_stops,
        adults=1,
        currency="EUR",
        layover_min=layover_min,
        layover_max=layover_max,
    )


class TestLayoverWindowBoundary:
    """D8: closed ``[180, 360]`` minutes, UTC-elapsed. The exact boundary
    table — 179 and 361 rejected, 180 and 360 accepted."""

    @pytest.mark.parametrize(
        ("layover_minutes", "expected_code"),
        [
            (179, RejectionCode.LAYOVER_TOO_SHORT),
            (180, None),
            (360, None),
            (361, RejectionCode.LAYOVER_TOO_LONG),
        ],
    )
    def test_boundary(
        self, layover_minutes: int, expected_code: RejectionCode | None
    ) -> None:
        itinerary = _itinerary_from_leg(_one_stop_leg(layover_minutes=layover_minutes))
        request = _request(max_stops=1)

        result = validate(itinerary, request)

        if expected_code is None:
            assert result.is_valid
            assert result.rejections == ()
        else:
            assert not result.is_valid
            codes = {rejection.code for rejection in result.rejections}
            assert codes == {expected_code}


class TestDirectItineraryBypassesLayoverRule:
    """D13: a direct itinerary (``stop_count == 0``) is not subject to the
    layover rule at all. Getting this wrong means every direct itinerary
    becomes unvalidatable, per the task brief's own warning."""

    def test_direct_itinerary_with_no_layover_is_accepted(self) -> None:
        depart_utc = datetime(2027, 7, 17, 10, 0, tzinfo=UTC)
        itinerary = _itinerary_from_leg(_direct_leg(depart_utc=depart_utc))
        request = _request(max_stops=1)

        result = validate(itinerary, request)

        assert result.is_valid
        assert result.rejections == ()


class TestDepartureDateOriginLocal:
    """D11: departure date is evaluated in the ORIGIN's LOCAL date, never
    UTC."""

    def test_local_date_after_utc_date_is_rejected(self) -> None:
        """The exact D11 case from the task brief: a segment departing
        2027-07-18 00:30 CEST is 2027-07-17 22:30 UTC. A UTC-date reading
        would wrongly ACCEPT this against a 2027-07-17 request; the
        origin-local reading correctly REJECTS it, because the traveller's
        own local calendar already reads the 18th.
        """
        depart_utc = datetime(2027, 7, 17, 22, 30, tzinfo=UTC)
        # Sanity on the fixture itself: this really is 00:30 CEST, i.e.
        # 2027-07-18 in Europe/Amsterdam local time.
        depart_local = depart_utc.astimezone(ZoneInfo("Europe/Amsterdam"))
        assert depart_local.date() == date(2027, 7, 18)

        itinerary = _itinerary_from_leg(_direct_leg(depart_utc=depart_utc))
        request = _request(departure_date=date(2027, 7, 17), max_stops=1)

        result = validate(itinerary, request)

        assert not result.is_valid
        codes = {rejection.code for rejection in result.rejections}
        assert codes == {RejectionCode.DATE_MISMATCH}
        (rejection,) = result.rejections
        assert rejection.observed == "2027-07-18"
        assert rejection.expected == "2027-07-17"

    def test_local_date_matching_request_is_accepted_even_when_utc_date_differs(
        self,
    ) -> None:
        """The mirror case DECISIONS.md D11 names explicitly: late local
        time on the requested day must be ACCEPTED even though its UTC
        instant already rolled into the next UTC day. 23:30 EDT on
        2027-07-17 in New York is 2027-07-18 03:30 UTC — if this rule used
        the UTC date it would wrongly reject a flight that, for the
        traveller, genuinely leaves on the requested day.
        """
        depart_utc = datetime(2027, 7, 18, 3, 30, tzinfo=UTC)
        depart_local = depart_utc.astimezone(ZoneInfo("America/New_York"))
        assert depart_local.date() == date(2027, 7, 17)
        assert depart_utc.date() == date(2027, 7, 18)  # UTC date genuinely differs

        itinerary = _itinerary_from_leg(
            _direct_leg(
                origin="JFK",
                depart_utc=depart_utc,
                origin_tz="America/New_York",
            )
        )
        request = _request(origin="JFK", departure_date=date(2027, 7, 17), max_stops=1)

        result = validate(itinerary, request)

        assert result.is_valid
        assert result.rejections == ()


class TestStopCountRule:
    def test_too_many_stops_is_rejected(self) -> None:
        itinerary = _itinerary_from_leg(_one_stop_leg(layover_minutes=240))
        request = _request(max_stops=0)

        result = validate(itinerary, request)

        assert not result.is_valid
        codes = {rejection.code for rejection in result.rejections}
        assert codes == {RejectionCode.TOO_MANY_STOPS}

    def test_within_stop_limit_is_accepted(self) -> None:
        itinerary = _itinerary_from_leg(_one_stop_leg(layover_minutes=240))
        request = _request(max_stops=1)

        result = validate(itinerary, request)

        assert result.is_valid
        assert result.rejections == ()


class TestOriginMismatchRule:
    def test_origin_mismatch_is_rejected(self) -> None:
        depart_utc = datetime(2027, 7, 17, 10, 0, tzinfo=UTC)
        itinerary = _itinerary_from_leg(_direct_leg(origin="AMS", depart_utc=depart_utc))
        request = _request(origin="CDG", max_stops=1)

        result = validate(itinerary, request)

        assert not result.is_valid
        codes = {rejection.code for rejection in result.rejections}
        assert codes == {RejectionCode.ORIGIN_MISMATCH}
        (rejection,) = result.rejections
        assert rejection.observed == "AMS"
        assert rejection.expected == "CDG"


class TestEngineAccumulatesAllRejections:
    """Proves the engine never short-circuits: an itinerary that fails
    three independent rules at once must carry all three rejections in
    one ``ValidationResult``, not just whichever rule ran first."""

    def test_multiply_invalid_itinerary_carries_every_rejection(self) -> None:
        # One-stop itinerary (stop_count == 1) with a too-short layover
        # (100 min < 180), requested against max_stops=0 (too many stops)
        # and the wrong origin (request "CDG", itinerary actually departs
        # AMS). Departure date is left matching so exactly three
        # rejections fire, not four -- isolating the "no short-circuit"
        # proof from any date-boundary concern already covered above.
        itinerary = _itinerary_from_leg(_one_stop_leg(layover_minutes=100))
        request = _request(origin="CDG", max_stops=0)

        result = validate(itinerary, request)

        assert not result.is_valid
        codes = {rejection.code for rejection in result.rejections}
        assert codes == {
            RejectionCode.TOO_MANY_STOPS,
            RejectionCode.LAYOVER_TOO_SHORT,
            RejectionCode.ORIGIN_MISMATCH,
        }
        # Exactly one rejection per failing rule -- if the engine had
        # stopped after the first hit (stop count), this would be 1, not 3.
        assert len(result.rejections) == 3


class TestDestinationMismatchRule:
    """T18 gap 1: ``check_destination_match`` is ``check_origin_match``'s
    missing counterpart -- only the departure end was checked before."""

    def test_destination_mismatch_is_rejected(self) -> None:
        depart_utc = datetime(2027, 7, 17, 10, 0, tzinfo=UTC)
        itinerary = _itinerary_from_leg(
            _direct_leg(origin="AMS", destination="DEL", depart_utc=depart_utc)
        )
        request = _request(destination="BOM", max_stops=1)

        result = validate(itinerary, request)

        assert not result.is_valid
        codes = {rejection.code for rejection in result.rejections}
        assert codes == {RejectionCode.DESTINATION_MISMATCH}
        (rejection,) = result.rejections
        assert rejection.observed == "DEL"
        assert rejection.expected == "BOM"

    def test_destination_match_is_accepted(self) -> None:
        depart_utc = datetime(2027, 7, 17, 10, 0, tzinfo=UTC)
        itinerary = _itinerary_from_leg(
            _direct_leg(origin="AMS", destination="DEL", depart_utc=depart_utc)
        )
        request = _request(destination="DEL", max_stops=1)

        result = validate(itinerary, request)

        assert result.is_valid
        assert result.rejections == ()


class TestSelfTransferRule:
    """T18 gap 3 (D5): self-transfer / separate-ticket itineraries are
    EXCLUDED from the valid ranked set, regardless of how generous the
    layover looks -- a 3h layover on separate tickets is not a real 3h
    layover."""

    def test_self_transfer_layover_is_rejected_even_within_layover_window(self) -> None:
        # 240 minutes sits comfortably inside the default [180, 360]
        # window -- the ONLY reason this itinerary can be rejected is D5,
        # never LAYOVER_TOO_SHORT/LAYOVER_TOO_LONG.
        itinerary = _itinerary_from_leg(_one_stop_leg_with_self_transfer(layover_minutes=240))
        request = _request(max_stops=1)

        result = validate(itinerary, request)

        assert not result.is_valid
        codes = {rejection.code for rejection in result.rejections}
        assert codes == {RejectionCode.SELF_TRANSFER}

    def test_layover_defaults_to_not_self_transfer(self) -> None:
        """The default MUST be ``False`` -- every Phase 1/2 ``Layover(...)``
        call site predates D5 and constructs without this field at all;
        if the default were ever anything but ``False`` every one of
        those 206 existing tests would start failing this new rule."""
        itinerary = _itinerary_from_leg(_one_stop_leg(layover_minutes=240))
        request = _request(max_stops=1)

        result = validate(itinerary, request)

        assert result.is_valid
        assert result.rejections == ()


class TestLocalTimeValidityRule:
    """T18 gap 2: ``Segment.ambiguous_local_time`` was computed but never
    acted on. ``check_local_time_validity`` reuses ``classify_local_time``
    (domain/segment.py) to pick between ``AMBIGUOUS_LOCAL_TIME`` (DST
    fall-back, the reading occurs twice) and ``NONEXISTENT_LOCAL_TIME``
    (DST spring-forward, the reading never occurs) -- both real,
    confirmed 2027 EU DST transitions (Europe/Amsterdam), not
    hypothetical dates.
    """

    def test_ambiguous_local_time_is_rejected(self) -> None:
        # 2027-10-31 02:30 Europe/Amsterdam: EU DST ends (fall back from
        # CEST to CET), so this wall-clock reading occurs twice. fold=0
        # picks the first (CEST) occurrence.
        leg = _direct_leg_with_local_time(
            depart_naive_local=datetime(2027, 10, 31, 2, 30),
            depart_fold=0,
        )
        itinerary = _itinerary_from_leg(leg)
        request = _request(departure_date=date(2027, 10, 31), max_stops=1)

        result = validate(itinerary, request)

        assert not result.is_valid
        codes = {rejection.code for rejection in result.rejections}
        assert codes == {RejectionCode.AMBIGUOUS_LOCAL_TIME}
        (rejection,) = result.rejections
        assert rejection.observed == "ambiguous"
        assert rejection.expected == "normal"

    def test_nonexistent_local_time_is_rejected(self) -> None:
        # 2027-03-28 02:30 Europe/Amsterdam: EU DST starts (spring forward
        # from CET to CEST, clocks jump 02:00 -> 03:00), so this
        # wall-clock reading never occurs at all.
        leg = _direct_leg_with_local_time(
            depart_naive_local=datetime(2027, 3, 28, 2, 30),
            depart_fold=0,
        )
        itinerary = _itinerary_from_leg(leg)
        request = _request(departure_date=date(2027, 3, 28), max_stops=1)

        result = validate(itinerary, request)

        assert not result.is_valid
        codes = {rejection.code for rejection in result.rejections}
        assert codes == {RejectionCode.NONEXISTENT_LOCAL_TIME}
        (rejection,) = result.rejections
        assert rejection.observed == "imaginary"
        assert rejection.expected == "normal"

    def test_normal_local_time_is_accepted(self) -> None:
        """A depart_utc converted the ordinary way (``.astimezone(zone)``)
        always lands on a genuine, single-occurrence instant -- this is
        the every-day case the rule must NOT flag."""
        depart_utc = datetime(2027, 7, 17, 10, 0, tzinfo=UTC)
        itinerary = _itinerary_from_leg(_direct_leg(depart_utc=depart_utc))
        request = _request(max_stops=1)

        result = validate(itinerary, request)

        assert result.is_valid
        assert result.rejections == ()


class TestMissingTimezoneUnreachable:
    """Investigation conclusion (T18 gap 4): ``RejectionCode.MISSING_TIMEZONE``
    is NOT reachable at the validator layer, so no rule was written to
    produce it. Two independent, already-existing upstream guarantees
    close this off before an itinerary can ever reach ``validate()``:

    1. ``Segment.origin_tz``/``destination_tz`` are required, non-optional
       ``str`` fields (domain/segment.py) -- there is no way to construct
       a ``Segment`` that OMITS them, so "missing" in the sense of
       "absent" cannot occur at all; pydantic itself refuses construction.
    2. Even a syntactically-present but WRONG zone key cannot survive
       construction either: ``Segment._validate_consistency`` calls
       ``_resolve_zone`` unconditionally on both zone strings, which
       raises ``ValueError`` for anything ``zoneinfo`` cannot resolve --
       so a ``Segment`` with a bad zone key never exists as a value the
       validator could inspect in the first place
       (``test_segment_construction_rejects_unresolvable_zone_key`` below).

    And one level upstream of THAT: ``flightagent.normalize.timezones.zone_for``
    -- the module any future provider mapper is documented to resolve a
    zone through -- raises ``UnknownAirportError`` for an unrecognised
    IATA code before a zone key is even produced, per its own
    ``registry.get`` (``test_zone_for_raises_on_unknown_iata`` below). No
    validator rule was invented just to exercise the otherwise-dead
    ``MISSING_TIMEZONE`` enum member -- these two tests verify the
    conclusion instead of merely asserting it.
    """

    def test_zone_for_raises_on_unknown_iata(self) -> None:
        with pytest.raises(UnknownAirportError):
            zone_for("ZZZ")

    def test_segment_construction_rejects_unresolvable_zone_key(self) -> None:
        depart_utc = datetime(2027, 7, 17, 10, 0, tzinfo=UTC)
        arrive_utc = depart_utc + timedelta(hours=8)

        with pytest.raises(ValueError, match="unknown IANA zone"):
            Segment(
                segment_id="bad-zone",
                origin="AMS",
                destination="DEL",
                depart_utc=depart_utc,
                arrive_utc=arrive_utc,
                depart_local=depart_utc.astimezone(ZoneInfo("UTC")),
                arrive_local=arrive_utc.astimezone(ZoneInfo("UTC")),
                origin_tz="Not/AZone",
                destination_tz="Asia/Kolkata",
                marketing_carrier="EK",
                flight_number="1",
                cabin=CabinClass.ECONOMY,
                duration=arrive_utc - depart_utc,
            )
