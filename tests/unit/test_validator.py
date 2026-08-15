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
no reachable path at this layer, proven rather than assumed).

T21 (Phase 3) adds the exhaustive boundary suite master plan section 10
calls for, on top of what Phase 2 and T18 already covered:
``TestLayoverElapsedTimeUsesUtcNotClockDelta`` (overnight, month-boundary,
26h, and exactly-24h layovers), ``TestDstAtValidatorLevel`` (the real
2027 Europe/Amsterdam fall-back/spring-forward instants carried all the
way through ``validate()``, not just the domain-model arithmetic),
``TestLocalTimeValidityArriveLocalField`` (T18's confirming tests only
ever exercised ``depart_local`` -- these exercise ``arrive_local`` too),
``TestStopCountDerivedFromSegments`` (a 3-stop, 4-segment itinerary), and
one more method on ``TestEngineAccumulatesAllRejections`` proving the
no-short-circuit contract holds with a T18 rule mixed in, not just
Phase 2's original four.
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


def _one_stop_leg_with_layover_window(
    *,
    layover_arrive_utc: datetime,
    layover_duration: timedelta,
) -> Leg:
    """AMS -> DXB -> DEL, ``stop_count == 1``, with explicit control over
    the DXB layover's arrival instant and duration -- unlike
    ``_one_stop_leg`` (which derives everything from one fixed anchor plus
    a minutes offset), this lets a test pin the LOCAL wall-clock reading
    at each end of the layover (e.g. to construct a midnight- or
    month-boundary-crossing local window) while the duration the rule
    actually consumes stays exactly what ``Layover._validate_consistency``
    computes from the two UTC instants underneath (D8, domain/segment.py)
    -- never a local wall-clock subtraction, which is exactly what these
    tests exist to prove is NOT what is happening.
    """
    inbound_depart_utc = layover_arrive_utc - timedelta(hours=4)
    outbound_depart_utc = layover_arrive_utc + layover_duration
    outbound_arrive_utc = outbound_depart_utc + timedelta(hours=3)

    inbound = _segment(
        segment_id="ams-dxb",
        origin="AMS",
        destination="DXB",
        depart_utc=inbound_depart_utc,
        arrive_utc=layover_arrive_utc,
        origin_tz="Europe/Amsterdam",
        destination_tz="Asia/Dubai",
    )
    outbound = _segment(
        segment_id="dxb-del",
        origin="DXB",
        destination="DEL",
        depart_utc=outbound_depart_utc,
        arrive_utc=outbound_arrive_utc,
        origin_tz="Asia/Dubai",
        destination_tz="Asia/Kolkata",
    )
    layover = Layover(
        airport="DXB",
        arrive_utc=layover_arrive_utc,
        depart_utc=outbound_depart_utc,
        duration=layover_duration,
        local_window=(inbound.arrive_local, outbound.depart_local),
        requires_airport_change=False,
        requires_terminal_change=False,
    )
    return Leg(segments=(inbound, outbound), layovers=(layover,))


def _one_stop_leg_via_amsterdam(
    *,
    layover_arrive_utc: datetime,
    layover_depart_utc: datetime,
) -> Leg:
    """CDG -> AMS -> DEL, ``stop_count == 1``, with explicit control over
    the AMS layover's two UTC instants -- lets a test place the layover
    directly across a real Europe/Amsterdam DST transition and assert on
    ``Layover.duration`` (UTC-elapsed) rather than whatever the local
    wall-clock delta at each end might suggest.
    """
    inbound_depart_utc = layover_arrive_utc - timedelta(hours=3)
    outbound_arrive_utc = layover_depart_utc + timedelta(hours=8)

    inbound = _segment(
        segment_id="cdg-ams",
        origin="CDG",
        destination="AMS",
        depart_utc=inbound_depart_utc,
        arrive_utc=layover_arrive_utc,
        origin_tz="Europe/Paris",
        destination_tz="Europe/Amsterdam",
    )
    outbound = _segment(
        segment_id="ams-del",
        origin="AMS",
        destination="DEL",
        depart_utc=layover_depart_utc,
        arrive_utc=outbound_arrive_utc,
        origin_tz="Europe/Amsterdam",
        destination_tz="Asia/Kolkata",
    )
    layover = Layover(
        airport="AMS",
        arrive_utc=layover_arrive_utc,
        depart_utc=layover_depart_utc,
        duration=layover_depart_utc - layover_arrive_utc,
        local_window=(inbound.arrive_local, outbound.depart_local),
        requires_airport_change=False,
        requires_terminal_change=False,
    )
    return Leg(segments=(inbound, outbound), layovers=(layover,))


def _direct_leg_with_arrival_local_time(
    *,
    arrive_naive_local: datetime,
    arrive_fold: int,
    origin: str,
    origin_tz: str,
    destination: str,
    destination_tz: str,
    duration: timedelta = timedelta(hours=6),
) -> Leg:
    """Mirror of ``_direct_leg_with_local_time``, but the ARRIVAL local
    reading is the deliberately ambiguous/nonexistent one instead of the
    departure -- proves ``check_local_time_validity`` inspects
    ``arrive_local`` too, not only ``depart_local`` (T18's own confirming
    tests, ``TestLocalTimeValidityRule`` above, only ever exercised the
    departure field). ``origin``/``origin_tz`` deliberately default to
    nothing so a caller always picks a non-DST zone (e.g. Asia/Dubai) for
    the untested end, keeping exactly one field under test.
    """
    origin_zone = ZoneInfo(origin_tz)
    destination_zone = ZoneInfo(destination_tz)
    arrive_local = arrive_naive_local.replace(tzinfo=destination_zone, fold=arrive_fold)
    arrive_utc = arrive_local.astimezone(UTC)
    depart_utc = arrive_utc - duration
    depart_local = depart_utc.astimezone(origin_zone)
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
        arrive_fold=arrive_fold,
    )
    return Leg(segments=(segment,), layovers=())


def _three_stop_leg() -> Leg:
    """AMS -> DXB -> BOM -> SIN -> DEL: 4 segments, ``stop_count == 3``.
    Every layover (240, 200, 210 minutes) sits inside the default
    [180, 360] window, so the ONLY rejection this itinerary can produce is
    ``TOO_MANY_STOPS`` -- proving ``stop_count`` is derived purely from
    ``len(segments) - 1`` summed across legs (the existing
    ``NormalizedItinerary.stop_count``/``Leg.connection_count`` computed
    fields), never read off any provider-supplied stop-count field (there
    is no such field on ``Segment``/``Leg``/``RawOffer`` to begin with).
    """
    ams_dep = datetime(2027, 7, 17, 2, 0, tzinfo=UTC)
    dxb_arr = ams_dep + timedelta(hours=4)
    dxb_dep = dxb_arr + timedelta(minutes=240)
    bom_arr = dxb_dep + timedelta(hours=3)
    bom_dep = bom_arr + timedelta(minutes=200)
    sin_arr = bom_dep + timedelta(hours=5)
    sin_dep = sin_arr + timedelta(minutes=210)
    del_arr = sin_dep + timedelta(hours=5)

    seg_ams_dxb = _segment(
        segment_id="ams-dxb",
        origin="AMS",
        destination="DXB",
        depart_utc=ams_dep,
        arrive_utc=dxb_arr,
        origin_tz="Europe/Amsterdam",
        destination_tz="Asia/Dubai",
    )
    seg_dxb_bom = _segment(
        segment_id="dxb-bom",
        origin="DXB",
        destination="BOM",
        depart_utc=dxb_dep,
        arrive_utc=bom_arr,
        origin_tz="Asia/Dubai",
        destination_tz="Asia/Kolkata",
    )
    seg_bom_sin = _segment(
        segment_id="bom-sin",
        origin="BOM",
        destination="SIN",
        depart_utc=bom_dep,
        arrive_utc=sin_arr,
        origin_tz="Asia/Kolkata",
        destination_tz="Asia/Singapore",
    )
    seg_sin_del = _segment(
        segment_id="sin-del",
        origin="SIN",
        destination="DEL",
        depart_utc=sin_dep,
        arrive_utc=del_arr,
        origin_tz="Asia/Singapore",
        destination_tz="Asia/Kolkata",
    )

    layover_dxb = Layover(
        airport="DXB",
        arrive_utc=dxb_arr,
        depart_utc=dxb_dep,
        duration=dxb_dep - dxb_arr,
        local_window=(seg_ams_dxb.arrive_local, seg_dxb_bom.depart_local),
        requires_airport_change=False,
        requires_terminal_change=False,
    )
    layover_bom = Layover(
        airport="BOM",
        arrive_utc=bom_arr,
        depart_utc=bom_dep,
        duration=bom_dep - bom_arr,
        local_window=(seg_dxb_bom.arrive_local, seg_bom_sin.depart_local),
        requires_airport_change=False,
        requires_terminal_change=False,
    )
    layover_sin = Layover(
        airport="SIN",
        arrive_utc=sin_arr,
        depart_utc=sin_dep,
        duration=sin_dep - sin_arr,
        local_window=(seg_bom_sin.arrive_local, seg_sin_del.depart_local),
        requires_airport_change=False,
        requires_terminal_change=False,
    )

    return Leg(
        segments=(seg_ams_dxb, seg_dxb_bom, seg_bom_sin, seg_sin_del),
        layovers=(layover_dxb, layover_bom, layover_sin),
    )


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

    def test_multiply_invalid_itinerary_including_a_t18_rule_carries_every_rejection(
        self,
    ) -> None:
        """T21: the same proof, but with a fourth rule mixed in that
        Phase 2 never had at all (``check_destination_match``, added by
        T18) -- confirms the engine's never-short-circuit contract still
        holds once ``RULES`` has grown beyond the four callables Phase 2
        shipped with, per the task brief's own example combination
        (too many stops AND a too-short layover AND wrong destination).
        """
        # Same one-stop, too-short-layover itinerary as above (AMS -> DXB
        # -> DEL), but now requested with a mismatched origin, a
        # mismatched destination, AND max_stops=0 -- four independent
        # rules fail at once, none of them departure-date related.
        itinerary = _itinerary_from_leg(_one_stop_leg(layover_minutes=100))
        request = _request(origin="CDG", destination="BOM", max_stops=0)

        result = validate(itinerary, request)

        assert not result.is_valid
        codes = {rejection.code for rejection in result.rejections}
        assert codes == {
            RejectionCode.TOO_MANY_STOPS,
            RejectionCode.LAYOVER_TOO_SHORT,
            RejectionCode.ORIGIN_MISMATCH,
            RejectionCode.DESTINATION_MISMATCH,
        }
        assert len(result.rejections) == 4


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


class TestLayoverElapsedTimeUsesUtcNotClockDelta:
    """T21: ``Layover.duration`` already computes exclusively on
    UTC-elapsed time (D8, domain/segment.py) -- this is not new logic.
    These tests PROVE that correctness holds all the way through
    ``check_layover_window`` for overnight, month-boundary, and multi-day
    spans, none of which Phase 2's boundary table (``TestLayoverWindowBoundary``
    above) exercised.
    """

    def test_overnight_layover_crossing_midnight_is_accepted(self) -> None:
        """22:30 to 01:45 the next day at DXB = 195 minutes, inside the
        default [180, 360] window."""
        leg = _one_stop_leg_with_layover_window(
            layover_arrive_utc=datetime(2027, 7, 17, 18, 30, tzinfo=UTC),
            layover_duration=timedelta(minutes=195),
        )
        layover = leg.layovers[0]
        # Sanity on the fixture: the local window really does cross
        # midnight, and the UTC-elapsed duration really is 195 minutes.
        assert layover.local_window[0].isoformat() == "2027-07-17T22:30:00+04:00"
        assert layover.local_window[1].isoformat() == "2027-07-18T01:45:00+04:00"
        assert layover.duration == timedelta(minutes=195)

        itinerary = _itinerary_from_leg(leg)
        request = _request(departure_date=date(2027, 7, 17), max_stops=1)

        result = validate(itinerary, request)

        assert result.is_valid
        assert result.rejections == ()

    def test_layover_crossing_a_month_boundary_is_accepted_when_in_window(self) -> None:
        """31 July 23:00 to 1 August 02:30 at DXB = 210 minutes, inside
        [180, 360] -- rolling over a MONTH boundary, not just a day, is
        irrelevant to a duration computed purely from two UTC instants."""
        leg = _one_stop_leg_with_layover_window(
            layover_arrive_utc=datetime(2027, 7, 31, 19, 0, tzinfo=UTC),
            layover_duration=timedelta(minutes=210),
        )
        layover = leg.layovers[0]
        assert layover.local_window[0].isoformat() == "2027-07-31T23:00:00+04:00"
        assert layover.local_window[1].isoformat() == "2027-08-01T02:30:00+04:00"
        assert layover.duration == timedelta(minutes=210)

        itinerary = _itinerary_from_leg(leg)
        request = _request(departure_date=date(2027, 7, 31), max_stops=1)

        result = validate(itinerary, request)

        assert result.is_valid
        assert result.rejections == ()

    def test_26_hour_layover_is_rejected_as_too_long(self) -> None:
        """A 26-hour layover proves TRUE elapsed time is used, not a
        same-day clock delta that might wrongly read a 26-hour span as
        some small same-clock-time difference."""
        itinerary = _itinerary_from_leg(_one_stop_leg(layover_minutes=26 * 60))
        request = _request(max_stops=1)

        result = validate(itinerary, request)

        assert not result.is_valid
        codes = {rejection.code for rejection in result.rejections}
        assert codes == {RejectionCode.LAYOVER_TOO_LONG}

    def test_exactly_24_hour_layover_is_rejected_as_too_long(self) -> None:
        """Exactly 24 hours -- a sanity check that huge layovers are
        firmly rejected, not accidentally treated as in-range by some
        modulo-24h clock-delta bug that would see the same wall-clock
        time on both ends and mistake it for a near-zero gap."""
        itinerary = _itinerary_from_leg(_one_stop_leg(layover_minutes=24 * 60))
        request = _request(max_stops=1)

        result = validate(itinerary, request)

        assert not result.is_valid
        codes = {rejection.code for rejection in result.rejections}
        assert codes == {RejectionCode.LAYOVER_TOO_LONG}


class TestDstAtValidatorLevel:
    """Phase 1's spike (spikes/tz_arithmetic.py) proved the tz ARITHMETIC
    is right; these tests prove the VALIDATOR RULE correctly consumes that
    arithmetic through the full ``NormalizedItinerary`` -> ``validate()``
    path, using real, confirmed 2027 Europe/Amsterdam DST transition
    instants (independently verified against ``zoneinfo`` directly, not
    assumed)."""

    def test_fall_back_layover_is_accepted_with_duration_exactly_210_minutes(self) -> None:
        """2027-10-31 fall-back: EU clocks go back at 01:00 UTC (02:00
        CEST local repeats as 02:00 CET). A layover arriving 01:00 CEST
        and departing 03:30 CET -- both comfortably outside the repeated
        02:00-03:00 local hour, so neither reading is itself ambiguous --
        is 3h30m = 210 minutes of real UTC-elapsed time, matching
        spikes/tz_arithmetic.py's proven result exactly."""
        leg = _one_stop_leg_via_amsterdam(
            layover_arrive_utc=datetime(2027, 10, 30, 23, 0, tzinfo=UTC),
            layover_depart_utc=datetime(2027, 10, 31, 2, 30, tzinfo=UTC),
        )
        layover = leg.layovers[0]
        assert layover.local_window[0].isoformat() == "2027-10-31T01:00:00+02:00"
        assert layover.local_window[1].isoformat() == "2027-10-31T03:30:00+01:00"
        assert layover.duration == timedelta(minutes=210)

        itinerary = _itinerary_from_leg(leg)
        request = _request(
            origin="CDG", destination="DEL", departure_date=date(2027, 10, 30), max_stops=1
        )

        result = validate(itinerary, request)

        assert result.is_valid
        assert result.rejections == ()

    def test_spring_forward_layover_is_rejected_as_too_short(self) -> None:
        """2027-03-28 spring-forward: EU clocks jump forward at 01:00 UTC
        (02:00 CET -> 03:00 CEST, the 02:00-03:00 local hour never
        occurs). A layover arriving 01:30 CET and departing 05:00 CEST
        reads as 3h30m on a naive local-clock subtraction, but the real
        UTC-elapsed duration is only 2h30m = 150 minutes -- below the
        180-minute minimum, so this must be REJECTED, not accepted on the
        strength of the misleading local-clock delta."""
        leg = _one_stop_leg_via_amsterdam(
            layover_arrive_utc=datetime(2027, 3, 28, 0, 30, tzinfo=UTC),
            layover_depart_utc=datetime(2027, 3, 28, 3, 0, tzinfo=UTC),
        )
        layover = leg.layovers[0]
        assert layover.local_window[0].isoformat() == "2027-03-28T01:30:00+01:00"
        assert layover.local_window[1].isoformat() == "2027-03-28T05:00:00+02:00"
        assert layover.duration == timedelta(minutes=150)

        itinerary = _itinerary_from_leg(leg)
        request = _request(
            origin="CDG", destination="DEL", departure_date=date(2027, 3, 27), max_stops=1
        )

        result = validate(itinerary, request)

        assert not result.is_valid
        codes = {rejection.code for rejection in result.rejections}
        assert codes == {RejectionCode.LAYOVER_TOO_SHORT}


class TestLocalTimeValidityArriveLocalField:
    """T21: the task brief calls out 'a segment whose depart_local OR
    arrive_local genuinely falls' into a DST fall-back/spring-forward
    reading -- T18's own confirming tests (``TestLocalTimeValidityRule``
    above) only ever put the deliberately bad reading on ``depart_local``.
    These two prove ``check_local_time_validity`` inspects ``arrive_local``
    too, using the same real, confirmed 2027 EU DST transition instants.
    """

    def test_ambiguous_arrive_local_is_rejected(self) -> None:
        # 2027-10-31 02:30 Europe/Amsterdam, as the ARRIVAL reading this
        # time (DXB -> AMS): the fall-back repeated hour, occurring twice.
        # fold=0 picks the first (CEST) occurrence.
        leg = _direct_leg_with_arrival_local_time(
            arrive_naive_local=datetime(2027, 10, 31, 2, 30),
            arrive_fold=0,
            origin="DXB",
            origin_tz="Asia/Dubai",
            destination="AMS",
            destination_tz="Europe/Amsterdam",
        )
        itinerary = _itinerary_from_leg(leg)
        request = _request(
            origin="DXB",
            destination="AMS",
            departure_date=itinerary.legs[0].segments[0].depart_local.date(),
            max_stops=1,
        )

        result = validate(itinerary, request)

        assert not result.is_valid
        codes = {rejection.code for rejection in result.rejections}
        assert codes == {RejectionCode.AMBIGUOUS_LOCAL_TIME}
        (rejection,) = result.rejections
        assert rejection.observed == "ambiguous"
        assert rejection.expected == "normal"

    def test_nonexistent_arrive_local_is_rejected(self) -> None:
        # 2027-03-28 02:30 Europe/Amsterdam, as the ARRIVAL reading this
        # time: the spring-forward gap, never occurs at all.
        leg = _direct_leg_with_arrival_local_time(
            arrive_naive_local=datetime(2027, 3, 28, 2, 30),
            arrive_fold=0,
            origin="DXB",
            origin_tz="Asia/Dubai",
            destination="AMS",
            destination_tz="Europe/Amsterdam",
        )
        itinerary = _itinerary_from_leg(leg)
        request = _request(
            origin="DXB",
            destination="AMS",
            departure_date=itinerary.legs[0].segments[0].depart_local.date(),
            max_stops=1,
        )

        result = validate(itinerary, request)

        assert not result.is_valid
        codes = {rejection.code for rejection in result.rejections}
        assert codes == {RejectionCode.NONEXISTENT_LOCAL_TIME}
        (rejection,) = result.rejections
        assert rejection.observed == "imaginary"
        assert rejection.expected == "normal"


class TestStopCountDerivedFromSegments:
    """T21: ``stop_count`` must come from the actual segment chain, never
    from any provider-supplied stop-count field -- there is no such field
    on ``Segment``/``Leg``/``RawOffer`` to begin with, so this test
    constructs the segment chain itself and confirms the rule reacts to
    what it actually implies.
    """

    def test_three_stops_implied_by_four_segments_triggers_too_many_stops(self) -> None:
        itinerary = _itinerary_from_leg(_three_stop_leg())
        # Sanity on the fixture: 4 segments really do imply 3 stops.
        assert itinerary.stop_count == 3
        request = _request(max_stops=1)

        result = validate(itinerary, request)

        assert not result.is_valid
        codes = {rejection.code for rejection in result.rejections}
        assert codes == {RejectionCode.TOO_MANY_STOPS}
