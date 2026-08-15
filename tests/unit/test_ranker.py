"""Unit tests for flightagent.scoring.ranking (T14, ranker v1).

Master plan finding 0.3: two genuinely distinct itineraries can tie on
adjusted_score, price, AND duration, and Python's ``sorted()`` is stable —
so without a final, non-optional ``itinerary_id`` tiebreak, tied output
order is a function of whatever order the itinerary list arrived in, which
is not reproducible under a future concurrent fan-out (Phase 4+).

``TestFullTieDeterminism`` below is the test that actually proves that
finding is fixed. A test that only checks "the output is sorted" would
still pass for a ranker that forgot the ``itinerary_id`` tiebreak entirely:
a stable sort over a broken/short key is still "sorted" by every OTHER key
whenever those don't tie, and the bug only shows up on a full tie — which
is exactly the case this file constructs.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from flightagent.domain.enums import CabinClass
from flightagent.domain.itinerary import Leg, NormalizedItinerary
from flightagent.domain.money import Money
from flightagent.domain.scoring import ScoreComponents, ScoredItinerary
from flightagent.domain.segment import Segment
from flightagent.scoring.ranking import rank_itineraries

_PLACEHOLDER_RANK = 1
"""ScoredItinerary's three rank_by_* fields are required (ge=1) at
construction, but this file's whole point is to feed rank_itineraries()
PRE-rank fixtures and check what it computes — every fixture below is
built with this same placeholder in all three fields, and no assertion in
this file ever trusts that placeholder value."""

_UTC_ZONE = ZoneInfo("UTC")


def _segment(
    *,
    origin: str,
    destination: str,
    depart_utc: datetime,
    arrive_utc: datetime,
) -> Segment:
    """A single UTC-zoned segment — DST/fold correctness is covered
    elsewhere (test_domain_smoke.py); this file only cares about ranking
    order, so origin_tz/destination_tz are both plain "UTC"."""
    return Segment(
        segment_id=f"{origin}-{destination}-1",
        origin=origin,
        destination=destination,
        depart_utc=depart_utc,
        arrive_utc=arrive_utc,
        depart_local=depart_utc.astimezone(_UTC_ZONE),
        arrive_local=arrive_utc.astimezone(_UTC_ZONE),
        origin_tz="UTC",
        destination_tz="UTC",
        marketing_carrier="KL",
        flight_number="1",
        cabin=CabinClass.ECONOMY,
        duration=arrive_utc - depart_utc,
    )


def _itinerary(
    *, itinerary_id: str, price_eur: Decimal, duration_hours: int
) -> NormalizedItinerary:
    depart_utc = datetime(2027, 7, 17, 8, 0, tzinfo=UTC)
    arrive_utc = depart_utc + timedelta(hours=duration_hours)
    segment = _segment(
        origin="AMS", destination="DEL", depart_utc=depart_utc, arrive_utc=arrive_utc
    )
    leg = Leg(segments=(segment,), layovers=())
    price = Money(amount=price_eur, currency="EUR")
    return NormalizedItinerary(
        itinerary_id=itinerary_id,
        provider="mock",
        legs=(leg,),
        price_original=price,
        price_eur=price,
        booking_url_kind="unavailable",
        shape_key=f"shape-{itinerary_id}",
        fare_as_of=depart_utc,
    )


def _scored(
    *,
    itinerary_id: str,
    adjusted_score: Decimal,
    price_eur: Decimal,
    duration_hours: int = 10,
) -> ScoredItinerary:
    """A ScoredItinerary with a fully independent adjusted_score and
    price_eur: elapsed_time_component/layover_penalty/direct_bonus are
    pinned to 0, so adjusted_score == fare_eur exactly. That independence
    from price_eur is deliberate — the rank_by_price-diverges-from-
    rank_by_adjusted_score test below needs to set them to opposite
    orderings on purpose."""
    itinerary = _itinerary(
        itinerary_id=itinerary_id, price_eur=price_eur, duration_hours=duration_hours
    )
    components = ScoreComponents(
        fare_eur=adjusted_score,
        elapsed_time_component=Decimal("0"),
        layover_penalty=Decimal("0"),
        direct_bonus=Decimal("0"),
    )
    return ScoredItinerary(
        itinerary=itinerary,
        components=components,
        rank_by_adjusted_score=_PLACEHOLDER_RANK,
        rank_by_total_journey_score=_PLACEHOLDER_RANK,
        rank_by_price=_PLACEHOLDER_RANK,
    )


class TestAscendingSortByAdjustedScore:
    def test_three_items_sorted_ascending_by_adjusted_score(self) -> None:
        cheapest = _scored(
            itinerary_id="itin_a", adjusted_score=Decimal("500"), price_eur=Decimal("500")
        )
        middle = _scored(
            itinerary_id="itin_b", adjusted_score=Decimal("700"), price_eur=Decimal("700")
        )
        priciest = _scored(
            itinerary_id="itin_c", adjusted_score=Decimal("900"), price_eur=Decimal("900")
        )

        # Deliberately unsorted input.
        ranked = rank_itineraries([priciest, cheapest, middle])

        assert [item.itinerary.itinerary_id for item in ranked] == ["itin_a", "itin_b", "itin_c"]
        assert [item.rank_by_adjusted_score for item in ranked] == [1, 2, 3]


class TestFullTieDeterminism:
    """The test that actually proves finding 0.3 is fixed — see module
    docstring."""

    def test_identical_score_price_duration_still_deterministic_across_runs(self) -> None:
        tied = [
            _scored(
                itinerary_id=f"itin_tied_{i:03d}",
                adjusted_score=Decimal("800"),
                price_eur=Decimal("800"),
                duration_hours=10,
            )
            for i in range(8)
        ]
        original_ids = [item.itinerary.itinerary_id for item in tied]

        shuffle_one = list(tied)
        random.Random(1).shuffle(shuffle_one)
        shuffle_two = list(tied)
        random.Random(2).shuffle(shuffle_two)

        # Guard against a degenerate shuffle that would make this test
        # vacuous: the two arrival orders, and the original, must actually
        # differ from one another for "arrival order doesn't matter" to
        # mean anything below.
        ids_shuffle_one = [item.itinerary.itinerary_id for item in shuffle_one]
        ids_shuffle_two = [item.itinerary.itinerary_id for item in shuffle_two]
        assert ids_shuffle_one != original_ids
        assert ids_shuffle_two != original_ids
        assert ids_shuffle_one != ids_shuffle_two

        # (a) the SAME shuffled input, ranked twice, is byte-identical.
        run_a = rank_itineraries(shuffle_one)
        run_b = rank_itineraries(shuffle_one)
        # (b) a DIFFERENT arrival order of the exact same tied set produces
        # the exact same output too — this is the actual claim finding 0.3
        # makes: the ranking does not depend on arrival order at all.
        run_c = rank_itineraries(shuffle_two)

        result_ids_a = [item.itinerary.itinerary_id for item in run_a]
        result_ids_b = [item.itinerary.itinerary_id for item in run_b]
        result_ids_c = [item.itinerary.itinerary_id for item in run_c]

        # The only remaining key once score/price/duration tie.
        expected_order = sorted(original_ids)
        assert result_ids_a == expected_order
        assert result_ids_b == expected_order
        assert result_ids_c == expected_order


class TestPriceRankingDivergesFromAdjustedScoreRanking:
    def test_cheaper_but_slower_itinerary_ranks_differently_by_price_than_by_score(self) -> None:
        # Cheap in price, but a long elapsed_time_component makes its
        # adjusted_score the WORSE (higher) of the two.
        cheap_slow = _scored(
            itinerary_id="itin_cheap_slow",
            adjusted_score=Decimal("1000"),
            price_eur=Decimal("500"),
            duration_hours=20,
        )
        # Pricier, but its adjusted_score is the BETTER (lower) of the two.
        pricey_fast = _scored(
            itinerary_id="itin_pricey_fast",
            adjusted_score=Decimal("600"),
            price_eur=Decimal("900"),
            duration_hours=5,
        )

        ranked = rank_itineraries([cheap_slow, pricey_fast])
        by_id = {item.itinerary.itinerary_id: item for item in ranked}

        # By adjusted_score, the pricier-but-faster itinerary wins.
        assert by_id["itin_pricey_fast"].rank_by_adjusted_score == 1
        assert by_id["itin_cheap_slow"].rank_by_adjusted_score == 2

        # By price alone, the two orderings DISAGREE — exactly the
        # §2.6-vs-§5.8 contradiction D16 resolves by publishing both
        # rankings rather than picking a winner.
        assert by_id["itin_cheap_slow"].rank_by_price == 1
        assert by_id["itin_pricey_fast"].rank_by_price == 2


class TestTopNSlicing:
    def test_returns_exactly_n_when_more_are_available(self) -> None:
        items = [
            _scored(
                itinerary_id=f"itin_{i:03d}",
                adjusted_score=Decimal(1000 - i),
                price_eur=Decimal(1000 - i),
            )
            for i in range(15)
        ]

        ranked = rank_itineraries(items, top_n=10)

        assert len(ranked) == 10
        # It is the actual top 10 by adjusted_score (global rank 1..10),
        # not an arbitrary 10-item slice.
        assert [item.rank_by_adjusted_score for item in ranked] == list(range(1, 11))

    def test_returns_all_when_fewer_than_n(self) -> None:
        items = [
            _scored(itinerary_id=f"itin_{i:03d}", adjusted_score=Decimal(i), price_eur=Decimal(i))
            for i in range(3)
        ]

        ranked = rank_itineraries(items, top_n=10)

        assert len(ranked) == 3
        expected_ids = {"itin_000", "itin_001", "itin_002"}
        assert {item.itinerary.itinerary_id for item in ranked} == expected_ids

    def test_empty_input_returns_empty_list_without_error(self) -> None:
        ranked = rank_itineraries([], top_n=10)

        assert ranked == []

    def test_default_top_n_is_ten(self) -> None:
        items = [
            _scored(
                itinerary_id=f"itin_{i:03d}",
                adjusted_score=Decimal(1000 - i),
                price_eur=Decimal(1000 - i),
            )
            for i in range(15)
        ]

        ranked = rank_itineraries(items)

        assert len(ranked) == 10
