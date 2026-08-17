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
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from flightagent.domain.enums import CabinClass
from flightagent.domain.ground import GroundLeg
from flightagent.domain.itinerary import Leg, NormalizedItinerary
from flightagent.domain.money import Money
from flightagent.domain.scoring import ScoreComponents, ScoredItinerary
from flightagent.domain.segment import Segment
from flightagent.scoring.origin_summary import summarize_by_origin
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


_TEN_ORIGINS = ("AMS", "EIN", "RTM", "DUS", "BRU", "NRN", "CGN", "CRL", "MST", "GRQ")
"""The real Phase 6 origin set (config/ground_access.yaml priority order) --
used below instead of an arbitrary shorter stand-in so the "exactly 10
rows" test actually exercises all 10 configured origins, not a fraction of
them."""

_EIGHT_DESTINATIONS = ("DEL", "BOM", "BLR", "HYD", "MAA", "CCU", "LKO", "VNS")
"""The real 8 registry destinations (config/airports.yaml) -- see
_TEN_ORIGINS above for why the real set is used rather than a shorter one."""


def _ground_leg(*, origin: str, minutes: int) -> GroundLeg:
    """A minimal, valid GroundLeg for test fixtures that need a ScoredItinerary
    carrying ground data -- values other than `duration` are arbitrary but
    valid; only `duration` (via `minutes`) is ever asserted on by the tests
    that use this."""
    return GroundLeg(
        from_location="Nieuwegein, Utrecht, NL",
        to_airport=origin,
        mode="car",
        duration=timedelta(minutes=minutes),
        distance_km=Decimal("50"),
        cost=Money(amount=Decimal("10.00"), currency="EUR"),
        source="estimate",
        as_of=date(2026, 8, 14),
    )


def _itinerary_for_origin(
    *,
    itinerary_id: str,
    origin: str,
    destination: str,
    price_eur: Decimal,
    duration_hours: int = 10,
) -> NormalizedItinerary:
    """Like module-level `_itinerary`, but with an origin/destination the
    caller controls -- `_itinerary` itself hardcodes AMS->DEL, which is not
    enough for the per-origin grouping tests below, which need itineraries
    departing from several different origins."""
    depart_utc = datetime(2027, 7, 17, 8, 0, tzinfo=UTC)
    arrive_utc = depart_utc + timedelta(hours=duration_hours)
    segment = _segment(
        origin=origin, destination=destination, depart_utc=depart_utc, arrive_utc=arrive_utc
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


def _scored_for_origin(
    *,
    itinerary_id: str,
    origin: str,
    destination: str = "DEL",
    adjusted_score: Decimal,
    price_eur: Decimal,
    duration_hours: int = 10,
    ground_minutes: int | None = None,
) -> ScoredItinerary:
    """Like module-level `_scored`, but for an itinerary departing from
    `origin` (any airport, not just AMS) and optionally carrying a
    `GroundLeg` (`ground_minutes`, D7) -- both needed by the per-origin
    grouping tests below, neither offered by `_scored` itself."""
    itinerary = _itinerary_for_origin(
        itinerary_id=itinerary_id,
        origin=origin,
        destination=destination,
        price_eur=price_eur,
        duration_hours=duration_hours,
    )
    components = ScoreComponents(
        fare_eur=adjusted_score,
        elapsed_time_component=Decimal("0"),
        layover_penalty=Decimal("0"),
        direct_bonus=Decimal("0"),
    )
    ground = (
        _ground_leg(origin=origin, minutes=ground_minutes) if ground_minutes is not None else None
    )
    return ScoredItinerary(
        itinerary=itinerary,
        components=components,
        ground=ground,
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


class TestSummarizeByOriginAtScale:
    """T40: a 160-itinerary batch (10 origins x 8 destinations x 2 fares --
    the literal Phase 6 full fan-out shape, master plan S5) must group into
    EXACTLY 10 OriginSummary rows, one per configured origin, each carrying
    that origin's own genuine cheapest itinerary."""

    def test_160_itinerary_batch_groups_into_exactly_ten_origin_rows(self) -> None:
        items = []
        for origin_index, origin in enumerate(_TEN_ORIGINS):
            for destination_index, destination in enumerate(_EIGHT_DESTINATIONS):
                for fare_variant in range(2):
                    price = Decimal(500 + origin_index * 10 + destination_index + fare_variant * 50)
                    itinerary_id = f"{origin}-{destination}-{fare_variant}"
                    items.append(
                        _scored_for_origin(
                            itinerary_id=itinerary_id,
                            origin=origin,
                            destination=destination,
                            adjusted_score=price,
                            price_eur=price,
                            ground_minutes=30 + origin_index,
                        )
                    )
        assert len(items) == 160

        summaries = summarize_by_origin(items, origins=_TEN_ORIGINS)

        assert len(summaries) == 10
        assert [summary.origin for summary in summaries] == list(_TEN_ORIGINS)
        assert all(summary.best is not None for summary in summaries)

        # Each origin's "best" really is the minimum tiebreak_key among
        # its own 16 itineraries -- not just "some" itinerary from that
        # origin, and not the global minimum leaking across origins.
        for origin_index, origin in enumerate(_TEN_ORIGINS):
            own_items = [
                item for item in items if item.itinerary.itinerary_id.startswith(f"{origin}-")
            ]
            assert len(own_items) == 16
            expected_best = min(own_items, key=lambda item: item.tiebreak_key)
            actual_best = summaries[origin_index].best
            assert actual_best is not None
            assert actual_best.itinerary.itinerary_id == expected_best.itinerary.itinerary_id
            assert summaries[origin_index].ground_minutes == 30 + origin_index


class TestSummarizeByOriginNeverDropsAnEmptyOrigin:
    """Acceptance criterion A2-5c: every configured origin gets a row "or
    an explicit reason for absence" -- an origin with zero itineraries
    must still appear, with best=None/ground_minutes=None, not vanish from
    the returned list."""

    def test_origin_with_zero_itineraries_still_gets_a_row_with_best_none(self) -> None:
        items = [
            _scored_for_origin(
                itinerary_id="ams-1",
                origin="AMS",
                adjusted_score=Decimal("500"),
                price_eur=Decimal("500"),
            )
        ]

        summaries = summarize_by_origin(items, origins=["AMS", "EIN"])

        assert len(summaries) == 2
        ams_summary, ein_summary = summaries
        assert ams_summary.origin == "AMS"
        assert ams_summary.best is not None
        assert ein_summary.origin == "EIN"
        assert ein_summary.best is None
        assert ein_summary.ground_minutes is None

    def test_empty_input_still_produces_a_row_per_configured_origin(self) -> None:
        summaries = summarize_by_origin([], origins=_TEN_ORIGINS)

        assert len(summaries) == 10
        assert all(summary.best is None for summary in summaries)
        assert all(summary.ground_minutes is None for summary in summaries)


class TestOriginSummaryGroundMinutes:
    def test_ground_minutes_derived_from_best_itinerarys_ground_leg(self) -> None:
        item = _scored_for_origin(
            itinerary_id="x",
            origin="AMS",
            adjusted_score=Decimal("500"),
            price_eur=Decimal("500"),
            ground_minutes=45,
        )

        summaries = summarize_by_origin([item], origins=["AMS"])

        assert summaries[0].ground_minutes == 45

    def test_ground_minutes_is_none_when_best_itinerary_has_no_ground_leg(self) -> None:
        item = _scored_for_origin(
            itinerary_id="x",
            origin="AMS",
            adjusted_score=Decimal("500"),
            price_eur=Decimal("500"),
        )

        summaries = summarize_by_origin([item], origins=["AMS"])

        assert summaries[0].best is not None
        assert summaries[0].ground_minutes is None


class TestGlobalAndPerOriginViewsAgree:
    """The global top-10 ranking (`rank_itineraries`, unchanged Phase 2/4
    behavior) and the per-origin view (`summarize_by_origin`, new T40 work)
    must both be computable over the SAME underlying `ScoredItinerary` set
    and never disagree about what a given itinerary's fare is."""

    def test_global_ranking_output_is_identical_whether_or_not_per_origin_view_is_also_built(
        self,
    ) -> None:
        items = [
            _scored_for_origin(
                itinerary_id=f"{origin}-item",
                origin=origin,
                adjusted_score=Decimal(1000 - index),
                price_eur=Decimal(1000 - index),
            )
            for index, origin in enumerate(("AMS", "EIN", "RTM"))
        ]

        ranked_alone = rank_itineraries(items, top_n=10)
        # Building the per-origin view from the SAME list must not mutate
        # it or leave any state that changes a subsequent ranking call --
        # every model involved is frozen, but this is the behavioural
        # property that frozen-ness is supposed to guarantee here.
        summarize_by_origin(items, origins=("AMS", "EIN", "RTM"))
        ranked_after = rank_itineraries(items, top_n=10)

        assert [item.itinerary.itinerary_id for item in ranked_alone] == [
            item.itinerary.itinerary_id for item in ranked_after
        ]
        assert [item.rank_by_adjusted_score for item in ranked_alone] == [
            item.rank_by_adjusted_score for item in ranked_after
        ]

    def test_global_top_10_winner_is_correctly_attributed_to_its_origin_in_the_per_origin_view(
        self,
    ) -> None:
        items = [
            _scored_for_origin(
                itinerary_id="ams-cheap",
                origin="AMS",
                adjusted_score=Decimal("400"),
                price_eur=Decimal("400"),
            ),
            _scored_for_origin(
                itinerary_id="ams-pricier",
                origin="AMS",
                adjusted_score=Decimal("600"),
                price_eur=Decimal("600"),
            ),
            _scored_for_origin(
                itinerary_id="ein-mid",
                origin="EIN",
                adjusted_score=Decimal("500"),
                price_eur=Decimal("500"),
            ),
        ]

        global_ranked = rank_itineraries(items, top_n=10)
        origin_summaries = summarize_by_origin(items, origins=("AMS", "EIN"))

        winner = global_ranked[0]
        assert winner.itinerary.itinerary_id == "ams-cheap"

        by_origin = {summary.origin: summary for summary in origin_summaries}
        ams_best = by_origin["AMS"].best
        assert ams_best is not None
        assert ams_best.itinerary.itinerary_id == winner.itinerary.itinerary_id
        assert ams_best.itinerary.price_eur == winner.itinerary.price_eur

    def test_an_origin_truncated_out_of_the_global_top_n_still_gets_its_own_best_here(
        self,
    ) -> None:
        """Proves the module docstring's caveat: `summarize_by_origin` must
        be called with the FULL, untruncated set to stay meaningful. GRQ's
        one itinerary here is real and valid, but every AMS fare beats it,
        so a `top_n=10` global cut with 12 cheaper AMS itineraries pushes
        GRQ's fare out of the displayed global table entirely -- it must
        still show up as GRQ's own best in the per-origin view."""
        cheap_ams_items = [
            _scored_for_origin(
                itinerary_id=f"ams-{i}",
                origin="AMS",
                adjusted_score=Decimal(100 + i),
                price_eur=Decimal(100 + i),
            )
            for i in range(12)
        ]
        grq_item = _scored_for_origin(
            itinerary_id="grq-1",
            origin="GRQ",
            adjusted_score=Decimal("9999"),
            price_eur=Decimal("9999"),
        )
        items = [*cheap_ams_items, grq_item]

        global_top10 = rank_itineraries(items, top_n=10)
        assert "grq-1" not in {item.itinerary.itinerary_id for item in global_top10}

        origin_summaries = summarize_by_origin(items, origins=("AMS", "GRQ"))
        by_origin = {summary.origin: summary for summary in origin_summaries}
        grq_summary = by_origin["GRQ"]
        assert grq_summary.best is not None
        assert grq_summary.best.itinerary.itinerary_id == "grq-1"
