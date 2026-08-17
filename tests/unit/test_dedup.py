"""Unit tests for flightagent.normalize.dedup (T20, dedup engine v1).

T20 keys dedup on `shape_key` — (segment origin/destination/depart_utc/
arrive_utc tuples, cabin, adults), deliberately EXCLUDING carrier and
flight number — so that codeshare siblings (one physical flight sold under
several marketing flight numbers) collapse into a single survivor
(finding 0.2). Every itinerary fixture below is built through the real
T11 -> T20 pipeline (`RawOffer` -> `build_normalized_itinerary` ->
`deduplicate`) rather than hand-stamping a `shape_key` string, so these
tests exercise the actual shape-key formula `dedup.py` relies on, not a
stand-in for it.

`TestPriceTieDeterminism` is this file's `TestFullTieDeterminism` analogue
(see test_ranker.py): finding 0.3's determinism requirement means an exact
price tie within one shape-key group must resolve to the SAME survivor
regardless of input arrival order — proven here by running `deduplicate`
on both orderings of the same pair and checking the winner never changes,
not just that "a" winner is produced.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from flightagent.domain.enums import CabinClass
from flightagent.domain.itinerary import Leg, NormalizedItinerary, RawOffer
from flightagent.domain.money import Money
from flightagent.domain.segment import Layover, Segment
from flightagent.normalize.builder import build_normalized_itinerary
from flightagent.normalize.dedup import deduplicate
from flightagent.observability.context import run_context
from flightagent.observability.logging import setup_logging

_DEPART = datetime(2027, 7, 17, 8, 0, tzinfo=UTC)
_ARRIVE = datetime(2027, 7, 17, 16, 0, tzinfo=UTC)
_FARE_AS_OF = datetime(2027, 6, 1, tzinfo=UTC)


def _make_itinerary(
    *,
    provider_offer_id: str,
    origin: str = "AMS",
    destination: str = "DEL",
    depart_utc: datetime = _DEPART,
    arrive_utc: datetime = _ARRIVE,
    marketing_carrier: str = "KL",
    flight_number: str = "891",
    price_eur: Decimal,
    booking_url: str,
) -> NormalizedItinerary:
    """One fully-built `NormalizedItinerary`, through the real
    `RawOffer` -> `build_normalized_itinerary` pipeline (T11), so its
    `shape_key` and `itinerary_id` are genuinely computed, not stubbed."""
    segment = Segment(
        segment_id=f"{origin}-{destination}-{flight_number}",
        origin=origin,
        destination=destination,
        depart_utc=depart_utc,
        arrive_utc=arrive_utc,
        depart_local=depart_utc,
        arrive_local=arrive_utc,
        origin_tz="UTC",
        destination_tz="UTC",
        marketing_carrier=marketing_carrier,
        flight_number=flight_number,
        cabin=CabinClass.ECONOMY,
        duration=arrive_utc - depart_utc,
    )
    leg = Leg(segments=(segment,), layovers=())
    price = Money(amount=price_eur, currency="EUR")
    raw_offer = RawOffer(
        provider="mock",
        provider_offer_id=provider_offer_id,
        legs=(leg,),
        price=price,
        raw_payload_ref=f"ref:{provider_offer_id}",
        provider_booking_url=booking_url,
    )
    return build_normalized_itinerary(
        raw_offer, adults=1, cabin=CabinClass.ECONOMY, fare_as_of=_FARE_AS_OF
    )


class TestCodeshareCollapse:
    """Finding 0.2's core case: same shape, different carrier/flight_number."""

    def test_codeshare_pair_collapses_to_cheaper_survivor_with_duplicate_metadata(self) -> None:
        cheap = _make_itinerary(
            provider_offer_id="kl-cheap",
            marketing_carrier="KL",
            flight_number="891",
            price_eur=Decimal("500.00"),
            booking_url="https://mock.example/book/kl-cheap",
        )
        pricier = _make_itinerary(
            provider_offer_id="af-pricier",
            marketing_carrier="AF",
            flight_number="1234",
            price_eur=Decimal("650.00"),
            booking_url="https://mock.example/book/af-pricier",
        )
        # Sanity: this really is the codeshare case the fixture claims — a
        # genuinely shared shape_key despite different carrier/flight_number.
        assert cheap.shape_key == pricier.shape_key
        assert cheap.itinerary_id != pricier.itinerary_id

        result = deduplicate([cheap, pricier])

        assert len(result) == 1
        survivor = result[0]
        assert survivor.itinerary_id == cheap.itinerary_id
        assert survivor.price_eur == cheap.price_eur
        assert survivor.duplicate_count == 2

        assert len(survivor.also_offered_by) == 1
        non_survivor_ref = survivor.also_offered_by[0]
        assert non_survivor_ref.marketing_carrier == "AF"
        assert non_survivor_ref.flight_number == "1234"

        # CodeshareReference carries no price — the non-survivor's price
        # lives in fare_options instead, one entry per itinerary in the
        # group including the survivor.
        fare_prices = {fare.price.amount for fare in survivor.fare_options}
        assert fare_prices == {Decimal("500.00"), Decimal("650.00")}


class TestSurvivorKeepsOwnBookingUrl:
    def test_cheaper_itinerary_arriving_second_keeps_its_own_booking_url(self) -> None:
        pricier_first = _make_itinerary(
            provider_offer_id="af-pricier",
            marketing_carrier="AF",
            flight_number="1234",
            price_eur=Decimal("650.00"),
            booking_url="https://mock.example/book/af-pricier",
        )
        cheap_second = _make_itinerary(
            provider_offer_id="kl-cheap",
            marketing_carrier="KL",
            flight_number="891",
            price_eur=Decimal("500.00"),
            booking_url="https://mock.example/book/kl-cheap",
        )

        # Deliberately: the cheaper itinerary is NOT first in the input list.
        result = deduplicate([pricier_first, cheap_second])

        assert len(result) == 1
        assert result[0].booking_url == cheap_second.booking_url
        assert str(result[0].booking_url) != str(pricier_first.booking_url)


class TestDistinctShapesStaySeparate:
    """Guards the other side of finding 0.2: a genuinely different shape_key
    must never be merged, no matter what causes the difference."""

    def test_different_flight_number_and_different_time_stays_two_entries(self) -> None:
        first = _make_itinerary(
            provider_offer_id="first",
            flight_number="100",
            price_eur=Decimal("500.00"),
            booking_url="https://mock.example/book/first",
        )
        shifted_depart = _DEPART + timedelta(hours=3)
        shifted_arrive = _ARRIVE + timedelta(hours=3)
        second = _make_itinerary(
            provider_offer_id="second",
            flight_number="200",
            depart_utc=shifted_depart,
            arrive_utc=shifted_arrive,
            price_eur=Decimal("550.00"),
            booking_url="https://mock.example/book/second",
        )
        assert first.shape_key != second.shape_key

        result = deduplicate([first, second])

        assert len(result) == 2
        assert {itinerary.duplicate_count for itinerary in result} == {1}
        assert {itinerary.itinerary_id for itinerary in result} == {
            first.itinerary_id,
            second.itinerary_id,
        }

    def test_same_flight_number_different_departure_time_stays_two_entries(self) -> None:
        """A different day's flight under the same marketing number is a
        different itinerary, not a duplicate."""
        day_one = _make_itinerary(
            provider_offer_id="day-one",
            flight_number="891",
            price_eur=Decimal("500.00"),
            booking_url="https://mock.example/book/day-one",
        )
        next_day_depart = _DEPART + timedelta(days=1)
        next_day_arrive = _ARRIVE + timedelta(days=1)
        day_two = _make_itinerary(
            provider_offer_id="day-two",
            flight_number="891",
            depart_utc=next_day_depart,
            arrive_utc=next_day_arrive,
            price_eur=Decimal("500.00"),
            booking_url="https://mock.example/book/day-two",
        )
        assert day_one.shape_key != day_two.shape_key

        result = deduplicate([day_one, day_two])

        assert len(result) == 2

    def test_different_origin_stays_two_entries(self) -> None:
        """Guards against a future cross-origin dedup accident once Phase 6
        introduces multiple origins."""
        from_ams = _make_itinerary(
            provider_offer_id="from-ams",
            origin="AMS",
            price_eur=Decimal("500.00"),
            booking_url="https://mock.example/book/from-ams",
        )
        from_lhr = _make_itinerary(
            provider_offer_id="from-lhr",
            origin="LHR",
            price_eur=Decimal("500.00"),
            booking_url="https://mock.example/book/from-lhr",
        )
        assert from_ams.shape_key != from_lhr.shape_key

        result = deduplicate([from_ams, from_lhr])

        assert len(result) == 2


class TestPriceTieDeterminism:
    """The test that actually proves finding 0.3's tiebreak applies here
    too — see module docstring. A test that only checks "one survivor came
    out" would still pass for a dedup engine whose tiebreak silently
    depended on input arrival order."""

    def test_exact_price_tie_resolves_to_same_survivor_regardless_of_input_order(self) -> None:
        itin_a = _make_itinerary(
            provider_offer_id="itin-a",
            marketing_carrier="KL",
            flight_number="891",
            price_eur=Decimal("500.00"),
            booking_url="https://mock.example/book/itin-a",
        )
        itin_b = _make_itinerary(
            provider_offer_id="itin-b",
            marketing_carrier="AF",
            flight_number="1234",
            price_eur=Decimal("500.00"),
            booking_url="https://mock.example/book/itin-b",
        )
        assert itin_a.shape_key == itin_b.shape_key
        assert itin_a.price_eur.amount == itin_b.price_eur.amount
        # Guard against a degenerate fixture: the tiebreak below is vacuous
        # unless the two itinerary_ids genuinely differ.
        assert itin_a.itinerary_id != itin_b.itinerary_id

        expected_survivor_id = min(itin_a.itinerary_id, itin_b.itinerary_id)

        result_ab = deduplicate([itin_a, itin_b])
        result_ba = deduplicate([itin_b, itin_a])

        assert len(result_ab) == 1
        assert len(result_ba) == 1
        assert result_ab[0].itinerary_id == expected_survivor_id
        assert result_ba[0].itinerary_id == expected_survivor_id


class TestEmptyInput:
    def test_empty_list_returns_empty_list_without_error(self) -> None:
        assert deduplicate([]) == []


class TestNoDuplicates:
    def test_all_unique_itineraries_preserve_count_and_get_duplicate_count_one(self) -> None:
        items = [
            _make_itinerary(
                provider_offer_id=f"itin-{i}",
                flight_number=str(100 + i),
                depart_utc=_DEPART + timedelta(hours=i),
                arrive_utc=_ARRIVE + timedelta(hours=i),
                price_eur=Decimal("500.00") + i,
                booking_url=f"https://mock.example/book/itin-{i}",
            )
            for i in range(5)
        ]
        # Sanity: five genuinely distinct shape_keys, not an accidental collision.
        assert len({itinerary.shape_key for itinerary in items}) == 5

        result = deduplicate(items)

        assert len(result) == 5
        assert {itinerary.itinerary_id for itinerary in result} == {
            itinerary.itinerary_id for itinerary in items
        }
        assert all(itinerary.duplicate_count == 1 for itinerary in result)


class TestFareOptionsLoneItineraryConvention:
    """T20's documented reading of a lone itinerary as "a collapse of
    exactly one offer, not zero" (dedup.py, `_collapse_group` docstring):
    a group of size 1 still gets a ONE-entry fare_options tuple
    representing itself, not an empty one."""

    def test_lone_itinerary_gets_single_entry_fare_options_representing_itself(self) -> None:
        lone = _make_itinerary(
            provider_offer_id="solo",
            price_eur=Decimal("500.00"),
            booking_url="https://mock.example/book/solo",
        )

        result = deduplicate([lone])

        assert len(result) == 1
        survivor = result[0]
        assert survivor.duplicate_count == 1
        assert survivor.also_offered_by == ()
        assert len(survivor.fare_options) == 1
        assert survivor.fare_options[0].price == survivor.price_eur


class TestDedupCompletedEvent:
    def test_dedup_completed_event_carries_correct_counts_on_a_real_batch(self) -> None:
        stream = io.StringIO()
        setup_logging(stream=stream)

        # A genuine codeshare pair (collapses to 1) plus two more itineraries
        # that are each unique on their own -> input_count=4, output_count=3,
        # duplicate_count=1 (one non-survivor collapsed away, total).
        dup_a = _make_itinerary(
            provider_offer_id="dup-a",
            marketing_carrier="KL",
            flight_number="891",
            price_eur=Decimal("500.00"),
            booking_url="https://mock.example/book/dup-a",
        )
        dup_b = _make_itinerary(
            provider_offer_id="dup-b",
            marketing_carrier="AF",
            flight_number="1234",
            price_eur=Decimal("600.00"),
            booking_url="https://mock.example/book/dup-b",
        )
        unique_one = _make_itinerary(
            provider_offer_id="unique-one",
            flight_number="500",
            depart_utc=_DEPART + timedelta(hours=5),
            arrive_utc=_ARRIVE + timedelta(hours=5),
            price_eur=Decimal("300.00"),
            booking_url="https://mock.example/book/unique-one",
        )
        unique_two = _make_itinerary(
            provider_offer_id="unique-two",
            origin="LHR",
            price_eur=Decimal("400.00"),
            booking_url="https://mock.example/book/unique-two",
        )

        with run_context("dedup-test-run"):
            result = deduplicate([dup_a, dup_b, unique_one, unique_two])

        assert len(result) == 3

        lines = stream.getvalue().strip().splitlines()
        payloads = [json.loads(line) for line in lines]
        dedup_payloads = [payload for payload in payloads if payload["event"] == "dedup.completed"]

        assert len(dedup_payloads) == 1
        payload = dedup_payloads[0]
        assert payload["input_count"] == 4
        assert payload["output_count"] == 3
        assert payload["duplicate_count"] == 1
        assert payload["run_id"] == "dedup-test-run"


class TestCrossOriginDedupGuard:
    """T40 investigation: does the shape-key formula ((segment
    origin/destination/depart_utc/arrive_utc) tuples, cabin, adults) already
    prevent two itineraries departing from genuinely different origin
    airports from ever collapsing into one dedup survivor, now that Phase 6
    fans a run out across all 10 real origins?

    Finding: YES, structurally, with NO code change needed in `dedup.py`.
    `normalize.builder.compute_shape_key` hashes each segment's own
    `origin` field as the first element of that segment's tuple. Two
    itineraries whose first segments have different `origin` values
    therefore always hash to different `shape_key`s — this holds
    regardless of how many segments follow, what carrier operates them, or
    what price they carry, because `dedup.py` groups by nothing OTHER than
    the `shape_key` each itinerary already carries (see `deduplicate`'s own
    docstring: "this module does not compute it; it only groups an
    already-normalized list by the key each itinerary already carries").
    There is no separate origin-comparison code path in `dedup.py` for a
    cross-origin accident to slip through.

    This exact scenario already had a regression test —
    `TestDistinctShapesStaySeparate.test_different_origin_stays_two_entries`
    above, added in Phase 3 (commit 92b0db3), whose own docstring says
    "Guards against a future cross-origin dedup accident once Phase 6
    introduces multiple origins" — Phase 6 is this task. That test already
    passes unchanged against the existing code, confirming the finding
    above. The two tests below EXTEND that existing guard rather than
    duplicate it: one replaces the two-airport (AMS/LHR) stand-in with the
    real, full 10-origin Phase 6 set (config/ground_access.yaml) checked
    pairwise all at once, and one covers a one-stop (2-segment) itinerary,
    which the existing single-segment test does not exercise — a one-stop
    shape_key hashes a SECOND segment tuple too, so this proves the
    never-merge guarantee survives a connection, not just a direct flight.

    No guard code was added to `dedup.py` for this task — there is no gap
    to close, only more coverage of an already-correct design.
    """

    def test_every_one_of_the_ten_real_phase6_origins_produces_a_distinct_shape_key(
        self,
    ) -> None:
        """The actual 10-origin set this project searches
        (config/ground_access.yaml priority order), not just a two-airport
        stand-in -- every origin's shape_key must be pairwise distinct from
        every other's, and none may collapse into another's survivor."""
        origins = ("AMS", "EIN", "RTM", "DUS", "BRU", "NRN", "CGN", "CRL", "MST", "GRQ")
        itineraries = [
            _make_itinerary(
                provider_offer_id=f"from-{origin}",
                origin=origin,
                price_eur=Decimal("500.00"),
                booking_url=f"https://mock.example/book/from-{origin}",
            )
            for origin in origins
        ]
        # Sanity: ten genuinely distinct shape_keys, not an accidental
        # collision that would make the assertions below vacuous.
        shape_keys = {itinerary.shape_key for itinerary in itineraries}
        assert len(shape_keys) == len(origins)

        result = deduplicate(itineraries)

        assert len(result) == len(origins)
        assert {itinerary.itinerary_id for itinerary in result} == {
            itinerary.itinerary_id for itinerary in itineraries
        }
        assert all(itinerary.duplicate_count == 1 for itinerary in result)

    def test_one_stop_itineraries_from_different_origins_stay_two_entries(self) -> None:
        """The existing origin guard only covers a single-segment (direct)
        itinerary. A one-stop itinerary's shape_key hashes EVERY segment's
        tuple, not just the first -- this proves the same never-merge
        guarantee holds with a second, third-airport leg downstream of the
        differing origin too, at identical connection times and identical
        price for both origins (the closest a routing coincidence could
        plausibly get)."""
        via = "FRA"
        destination = "DEL"
        depart_utc = _DEPART
        via_arrive = depart_utc + timedelta(hours=2)
        via_depart = via_arrive + timedelta(hours=1, minutes=30)
        final_arrive = via_depart + timedelta(hours=6)

        def _one_stop_from(origin: str, provider_offer_id: str) -> NormalizedItinerary:
            first_leg_segment = Segment(
                segment_id=f"{origin}-{via}-100",
                origin=origin,
                destination=via,
                depart_utc=depart_utc,
                arrive_utc=via_arrive,
                depart_local=depart_utc,
                arrive_local=via_arrive,
                origin_tz="UTC",
                destination_tz="UTC",
                marketing_carrier="LH",
                flight_number="100",
                cabin=CabinClass.ECONOMY,
                duration=via_arrive - depart_utc,
            )
            second_leg_segment = Segment(
                segment_id=f"{via}-{destination}-200",
                origin=via,
                destination=destination,
                depart_utc=via_depart,
                arrive_utc=final_arrive,
                depart_local=via_depart,
                arrive_local=final_arrive,
                origin_tz="UTC",
                destination_tz="UTC",
                marketing_carrier="LH",
                flight_number="200",
                cabin=CabinClass.ECONOMY,
                duration=final_arrive - via_depart,
            )
            layover = Layover(
                airport=via,
                arrive_utc=via_arrive,
                depart_utc=via_depart,
                duration=via_depart - via_arrive,
                local_window=(via_arrive, via_depart),
                requires_airport_change=False,
                requires_terminal_change=False,
            )
            leg = Leg(segments=(first_leg_segment, second_leg_segment), layovers=(layover,))
            price = Money(amount=Decimal("500.00"), currency="EUR")
            raw_offer = RawOffer(
                provider="mock",
                provider_offer_id=provider_offer_id,
                legs=(leg,),
                price=price,
                raw_payload_ref=f"ref:{provider_offer_id}",
                provider_booking_url=f"https://mock.example/book/{provider_offer_id}",
            )
            return build_normalized_itinerary(
                raw_offer, adults=1, cabin=CabinClass.ECONOMY, fare_as_of=_FARE_AS_OF
            )

        from_ams = _one_stop_from("AMS", "ams-onestop")
        from_dus = _one_stop_from("DUS", "dus-onestop")
        assert from_ams.shape_key != from_dus.shape_key

        result = deduplicate([from_ams, from_dus])

        assert len(result) == 2
        assert {itinerary.duplicate_count for itinerary in result} == {1}
