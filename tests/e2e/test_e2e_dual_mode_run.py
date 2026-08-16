"""End-to-end golden test (Phase 5, T36) -- the literal Phase 5 exit
criterion, codified as an executed test rather than asserted in prose.

Master plan §9's Phase 5 row and this task's brief both name the same five
things, verified here as real, executed assertions against the FULL CLI
pipeline (Typer's ``CliRunner``, exactly like ``test_e2e_mock_run.py``'s
Phase 2 smoke test and ``test_cli.py``'s ``TestAllDestinationsSucceeds``),
never at the unit level alone:

1. A full dual-mode 8-destination run for one origin issues **exactly 16**
   provider calls -- asserted against ``InstrumentedProvider``'s own call
   log (``tests/support/instrumented_provider.py``, Phase 4), not inferred.
2. The Markdown report's "## Direct Flight Analysis" table has Addendum 1's
   exact 5 columns, one row per destination (8 rows), including >=1
   "Not available" row.
3. DECISIONS.md D10's three CONFIRMED worked cases (620/710 -> RECOMMENDED,
   580/690 -> GOOD_VALUE, 560/860 -> NOT_RECOMMENDED) each produce their
   documented tier -- through the REAL CLI end to end, not
   ``policy.direct_vs_stop.analyze_destination`` called directly the way
   ``tests/unit/test_decision_policy.py`` already does.
4. The Final Summary sentence's EUR figure and hours-saved figure agree
   with the underlying ranked/policy data (parsed from the sentence AND
   read from the JSON side by side, not merely both present).
5. Running the identical dual-mode command TWICE produces byte-identical
   Markdown and JSON -- the same guarantee ``test_e2e_mock_run.py`` proved
   for the single-destination, 0-policy path, re-proven here with 16 calls
   and a real D10 policy computation in the loop.

**Why real ``RawOffer``s with hand-picked fares, not ``MockProvider``'s own
seeded generator.** ``providers.mock.generator.generate_offers`` (T10)
draws prices from ``rng.randint(55000, 95000)`` -- deterministic per
request, but not a number this test file can predict without reimplementing
the generator's own RNG draw sequence by hand. Reproducing D10's three
EXACT worked cases (and controlling which destination's itinerary ends up
globally rank 1, which the Final Summary section is keyed on) requires
fares this test controls precisely. So this module builds real
``RawOffer``/``Leg``/``Segment``/``Layover`` domain objects directly --
the same domain types the real generator builds, just with fixed prices
instead of RNG-drawn ones -- and monkeypatches
``tests.support.instrumented_provider.generate_offers`` (NOT
``flightagent.providers.mock.provider.generate_offers``, a different
import site) to return them. ``InstrumentedProvider`` itself is untouched
and still records every call exactly as T25 built it; only the offers it
hands back for these 8 destinations are fixed rather than seeded-random.
Every offer still goes through the REAL normalize -> validate -> dedup ->
score -> rank -> policy -> report pipeline (T11-T15, T20, T31, T33, T34)
unmodified -- nothing in this file bypasses or stubs any of those layers.

**Fare assignment, and why BOM ends up the report's primary destination.**
DEL/BOM/BLR carry the three worked cases as their direct/one-stop pair
(D10's own numbers, unchanged). HYD/MAA/CCU/LKO carry deliberately higher
fares (all direct/one-stop pairs priced so their ``adjusted_score`` sits
well above every worked-case itinerary) purely so they cannot accidentally
undercut the worked cases for the global top rank -- their own tier outcome
is untested and irrelevant here. VNS carries no direct offer at all (this
test's own choice, matching the real generator's own convention for VNS --
see ``providers.mock.generator._NO_DIRECT_SERVICE_DESTINATIONS``), giving
this run its required >=1 "Not available" row. Hand-computed adjusted
scores (fixed direct_bonus_eur=-120.0, time_value_eur_per_hour=3.0, the
packaged defaults -- see ``config/defaults.toml``'s ``[scoring]`` table):
BOM's direct itinerary (EUR690, 8h) scores 690 + 24 - 120 = 594, the lowest
of all 15 generated itineraries (every other candidate is verified by hand
in this module's own comments to score higher) -- so ``ranked[0]`` is
BOM's direct itinerary, and the Final Summary sentence (T34) is keyed on
BOM's ``DestinationAnalysis`` (GOOD_VALUE, diff EUR110.00, saves ~5.5h ->
rounds to 6 whole hours per ``reporting.markdown._whole_hours_saved``'s
half-up convention).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner, Result

from flightagent.cli import app
from flightagent.domain.enums import CabinClass
from flightagent.domain.itinerary import Leg, RawOffer
from flightagent.domain.money import Money
from flightagent.domain.run import SearchRequest
from flightagent.domain.segment import Layover, Segment
from tests.support.instrumented_provider import InstrumentedProvider, Succeed

runner = CliRunner()

_REPORT_FILENAME = "flight_report_2027-07-17.md"
_RESULTS_FILENAME = "flight_results_2027-07-17.json"

# The 8 registered destinations (config/airports.yaml), matching
# tests/unit/test_cli.py's own constant.
_ALL_8_DESTINATIONS = ("DEL", "BOM", "BLR", "HYD", "MAA", "CCU", "LKO", "VNS")

_DUAL_MODE_ARGS = [
    "run",
    "--origin",
    "AMS",
    "--date",
    "2027-07-17",
    "--max-stops",
    "1",
    "--all-destinations",
]

# ---------------------------------------------------------------------------
# Fixed-fare RawOffer construction -- real domain objects, hand-picked prices.
# ---------------------------------------------------------------------------

_AMS_TZ = "Europe/Amsterdam"
_INDIA_TZ = "Asia/Kolkata"
_HUB = "DXB"
_HUB_TZ = "Asia/Dubai"
_DEPARTURE_DATE = date(2027, 7, 17)

_DIRECT_DURATION = timedelta(hours=8)
_ONE_STOP_FIRST_LEG = timedelta(hours=6)
_ONE_STOP_LAYOVER_MINUTES = 210  # [180,240) band -> +0 penalty (D9), matching the
# master plan's/test_decision_policy.py's own "worked example" one-stop shape.
_ONE_STOP_SECOND_LEG = timedelta(hours=4)
# Total one-stop duration: 6h + 3h30m + 4h = 13h30m, for every destination below.

# DECISIONS.md D10's three CONFIRMED worked cases, unchanged:
#   stop 620 / direct 710 -> diff  90, rel 14.52% -> RECOMMENDED
#   stop 580 / direct 690 -> diff 110, rel 18.97% -> GOOD_VALUE
#   stop 560 / direct 860 -> diff 300, rel 53.57% -> NOT_RECOMMENDED
_DIRECT_PRICE_EUR = {
    "DEL": "710.00",
    "BOM": "690.00",
    "BLR": "860.00",
    # Filler destinations -- deliberately priced high (see module docstring)
    # so none can undercut BOM's direct itinerary for the global rank-1 spot.
    "HYD": "950.00",
    "MAA": "930.00",
    "CCU": "910.00",
    "LKO": "890.00",
    # VNS: no direct entry at all -- this run's own NOT_AVAILABLE destination.
}
_ONE_STOP_PRICE_EUR = {
    "DEL": "620.00",
    "BOM": "580.00",
    "BLR": "560.00",
    "HYD": "850.00",
    "MAA": "830.00",
    "CCU": "810.00",
    "LKO": "790.00",
    "VNS": "700.00",
}


def _local_to_utc(local_date: date, local_time: time, zone_key: str) -> datetime:
    naive = datetime.combine(local_date, local_time)
    return naive.replace(tzinfo=ZoneInfo(zone_key)).astimezone(UTC)


def _segment(
    *,
    origin: str,
    destination: str,
    origin_tz: str,
    destination_tz: str,
    depart_utc: datetime,
    duration: timedelta,
    flight_number: str,
) -> Segment:
    arrive_utc = depart_utc + duration
    return Segment(
        segment_id=f"{origin}-{destination}-{flight_number}",
        origin=origin,
        destination=destination,
        depart_utc=depart_utc,
        arrive_utc=arrive_utc,
        depart_local=depart_utc.astimezone(ZoneInfo(origin_tz)),
        arrive_local=arrive_utc.astimezone(ZoneInfo(destination_tz)),
        origin_tz=origin_tz,
        destination_tz=destination_tz,
        marketing_carrier="AI",
        flight_number=flight_number,
        cabin=CabinClass.ECONOMY,
        duration=duration,
    )


def _direct_offer(destination: str, price_eur: str) -> RawOffer:
    """A single-segment, zero-layover ``RawOffer`` -- ``stop_count == 0``."""
    depart_utc = _local_to_utc(_DEPARTURE_DATE, time(7, 30), _AMS_TZ)
    segment = _segment(
        origin="AMS",
        destination=destination,
        origin_tz=_AMS_TZ,
        destination_tz=_INDIA_TZ,
        depart_utc=depart_utc,
        duration=_DIRECT_DURATION,
        flight_number="100",
    )
    leg = Leg(segments=(segment,), layovers=())
    offer_id = f"fixed-direct-{destination}"
    return RawOffer(
        provider="mock",
        provider_offer_id=offer_id,
        legs=(leg,),
        price=Money(amount=Decimal(price_eur), currency="EUR"),
        raw_payload_ref=f"mock://fixed/{offer_id}",
        provider_booking_url=f"https://mock-booking.example.test/{offer_id}",
    )


def _one_stop_offer(destination: str, price_eur: str) -> RawOffer:
    """A two-segment, one-layover ``RawOffer`` via ``_HUB`` --
    ``stop_count == 1``, with the layover inside D8's valid [180,360] window."""
    outbound_depart = _local_to_utc(_DEPARTURE_DATE, time(9, 0), _AMS_TZ)
    outbound = _segment(
        origin="AMS",
        destination=_HUB,
        origin_tz=_AMS_TZ,
        destination_tz=_HUB_TZ,
        depart_utc=outbound_depart,
        duration=_ONE_STOP_FIRST_LEG,
        flight_number="200",
    )
    inbound_depart = outbound.arrive_utc + timedelta(minutes=_ONE_STOP_LAYOVER_MINUTES)
    inbound = _segment(
        origin=_HUB,
        destination=destination,
        origin_tz=_HUB_TZ,
        destination_tz=_INDIA_TZ,
        depart_utc=inbound_depart,
        duration=_ONE_STOP_SECOND_LEG,
        flight_number="201",
    )
    layover = Layover(
        airport=_HUB,
        arrive_utc=outbound.arrive_utc,
        depart_utc=inbound.depart_utc,
        duration=inbound.depart_utc - outbound.arrive_utc,
        local_window=(
            outbound.arrive_utc.astimezone(ZoneInfo(_HUB_TZ)),
            inbound.depart_utc.astimezone(ZoneInfo(_HUB_TZ)),
        ),
        requires_airport_change=False,
        requires_terminal_change=False,
    )
    leg = Leg(segments=(outbound, inbound), layovers=(layover,))
    offer_id = f"fixed-stop-{destination}"
    return RawOffer(
        provider="mock",
        provider_offer_id=offer_id,
        legs=(leg,),
        price=Money(amount=Decimal(price_eur), currency="EUR"),
        raw_payload_ref=f"mock://fixed/{offer_id}",
        provider_booking_url=f"https://mock-booking.example.test/{offer_id}",
    )


def _build_fixed_offers() -> dict[tuple[str, int], tuple[RawOffer, ...]]:
    """``(destination, max_stops) -> offers`` for every one of the 16 tasks
    this run's plan builds -- 8 destinations x {direct, one-stop}. VNS's
    direct entry is deliberately absent (empty tuple): this run's own
    NOT_AVAILABLE destination (T29/D10), matching the real generator's own
    convention for VNS rather than inventing a new one.
    """
    offers: dict[tuple[str, int], tuple[RawOffer, ...]] = {}
    for destination, price in _DIRECT_PRICE_EUR.items():
        offers[(destination, 0)] = (_direct_offer(destination, price),)
    offers[("VNS", 0)] = ()
    for destination, price in _ONE_STOP_PRICE_EUR.items():
        offers[(destination, 1)] = (_one_stop_offer(destination, price),)
    return offers


_FIXED_OFFERS = _build_fixed_offers()


def _fake_generate_offers(request: SearchRequest) -> tuple[RawOffer, ...]:
    """Drop-in replacement for
    ``tests.support.instrumented_provider.generate_offers`` -- returns this
    module's fixed-fare offers keyed by ``(destination, max_stops)`` instead
    of the real seeded-random generator's output. See module docstring for
    why this monkeypatch target (not ``providers.mock.provider``'s own
    import site) is the correct one for ``InstrumentedProvider``.
    """
    return _FIXED_OFFERS.get((request.destination, request.max_stops), ())


# ---------------------------------------------------------------------------
# Shared run helper -- one full ``--all-destinations`` invocation.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _DualModeRun:
    result: Result
    provider: InstrumentedProvider
    report_text: str
    results: dict[str, Any]


def _invoke_dual_mode(directory: Path, monkeypatch: pytest.MonkeyPatch) -> _DualModeRun:
    """Run the target dual-mode invocation with cwd pinned to ``directory``
    and ``InstrumentedProvider`` (scripted to succeed everywhere, offering
    exactly this module's fixed fares) standing in for the real mock
    provider -- the same substitution pattern
    ``test_cli.py::TestAllDestinationsSucceeds`` already uses one layer up,
    applied here with fixed rather than seeded-random fares.
    """
    monkeypatch.chdir(directory)
    monkeypatch.setattr(
        "tests.support.instrumented_provider.generate_offers", _fake_generate_offers
    )
    provider = InstrumentedProvider(
        scripts={destination: [Succeed(offer_count=1)] for destination in _ALL_8_DESTINATIONS}
    )
    monkeypatch.setattr("flightagent.cli._build_provider", lambda name: provider)

    result = runner.invoke(app, _DUAL_MODE_ARGS)

    report_text = ""
    results: dict[str, Any] = {}
    report_path = directory / "out" / _REPORT_FILENAME
    results_path = directory / "out" / _RESULTS_FILENAME
    if report_path.is_file():
        report_text = report_path.read_text(encoding="utf-8")
    if results_path.is_file():
        results = json.loads(results_path.read_text(encoding="utf-8"))

    return _DualModeRun(result=result, provider=provider, report_text=report_text, results=results)


@pytest.fixture
def dual_mode_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _DualModeRun:
    """One successful dual-mode run, isolated to its own ``tmp_path`` --
    fresh per test function (fixtures re-run per test), so no two test
    methods ever share a provider's call log or an ``out/`` directory."""
    run = _invoke_dual_mode(tmp_path, monkeypatch)
    assert run.result.exit_code == 0, run.result.output
    return run


def _direct_flight_analysis_row(report_text: str, destination: str) -> str:
    """The single Markdown table row beginning ``"| {destination} |"`` --
    raises (not returns ``None``) if the destination has no row at all,
    since every one of the 8 registry destinations must have exactly one
    (D15's amendment: this table is the one place a destination can never
    be silently absent)."""
    marker = f"| {destination} |"
    for line in report_text.splitlines():
        if line.startswith(marker):
            return line
    raise AssertionError(
        f"no Direct Flight Analysis row found for destination {destination!r} in:\n{report_text}"
    )


def _destination_analysis_json(results: dict[str, Any], destination: str) -> dict[str, Any]:
    for entry in results["destination_analyses"]:
        if entry["destination"] == destination:
            return entry
    raise AssertionError(f"no destination_analyses JSON entry for {destination!r}")


# ---------------------------------------------------------------------------
# 1. Exactly 16 provider calls.
# ---------------------------------------------------------------------------


class TestExactlySixteenProviderCalls:
    """The phase's own literal exit criterion: a full dual-mode 8-destination
    run for one origin issues exactly 16 provider calls -- asserted against
    ``InstrumentedProvider``'s own call log, never inferred from the
    destination count or the report's contents."""

    def test_issues_exactly_16_calls_two_modes_per_destination(
        self, dual_mode_run: _DualModeRun
    ) -> None:
        call_log = dual_mode_run.provider.call_log
        assert len(call_log) == 16
        assert {record.destination for record in call_log} == set(_ALL_8_DESTINATIONS)
        assert {record.origin for record in call_log} == {"AMS"}
        assert {record.max_stops for record in call_log} == {0, 1}
        for destination in _ALL_8_DESTINATIONS:
            modes = sorted(
                record.max_stops for record in call_log if record.destination == destination
            )
            assert modes == [0, 1], f"{destination} must be searched at both modes exactly once"


# ---------------------------------------------------------------------------
# 2. Direct Flight Analysis table: 5 columns, 8 rows, >=1 "Not available".
# ---------------------------------------------------------------------------


class TestDirectFlightAnalysisTable:
    """Addendum 1's exact 5-column table, one row per registry destination
    (8 rows total), including at least one "Not available" row (VNS --
    D10's NOT_AVAILABLE tier, no direct service)."""

    def test_table_present_with_exact_columns_and_all_eight_rows(
        self, dual_mode_run: _DualModeRun
    ) -> None:
        report_text = dual_mode_run.report_text
        assert "## Direct Flight Analysis" in report_text
        assert (
            "| Destination | Airline | Price | Difference vs Cheapest Stop | "
            "Recommendation |" in report_text
        )
        for destination in _ALL_8_DESTINATIONS:
            _direct_flight_analysis_row(report_text, destination)  # raises if absent

    def test_vns_row_and_json_entry_are_not_available(self, dual_mode_run: _DualModeRun) -> None:
        row = _direct_flight_analysis_row(dual_mode_run.report_text, "VNS")
        assert "Not available" in row

        analysis = _destination_analysis_json(dual_mode_run.results, "VNS")
        assert analysis["tier"] == "not_available"
        assert analysis["recommendation"] == "Not available"
        assert analysis["airline"] is None
        assert analysis["price_eur"] is None
        assert analysis["price_difference_eur"] is None


# ---------------------------------------------------------------------------
# 3. The three D10 worked cases, through the FULL pipeline.
# ---------------------------------------------------------------------------


class TestThreeWorkedCasesThroughFullPipeline:
    """DECISIONS.md D10's three CONFIRMED worked cases, reproduced end to
    end with real ``RawOffer``s carrying these exact fares, run through the
    real CLI -- never ``policy.direct_vs_stop.analyze_destination`` called
    directly (that unit-level proof already exists in
    ``tests/unit/test_decision_policy.py::TestThreeWorkedSpecCases``)."""

    def test_case_1_diff_90_rel_14_5_percent_is_recommended(
        self, dual_mode_run: _DualModeRun
    ) -> None:
        row = _direct_flight_analysis_row(dual_mode_run.report_text, "DEL")
        assert "★ Recommended" in row

        analysis = _destination_analysis_json(dual_mode_run.results, "DEL")
        assert analysis["tier"] == "recommended"
        assert analysis["recommendation"] == "Recommended"
        assert analysis["price_difference_eur"] == "90.00"
        assert analysis["relative_difference"] == str(Decimal("90.00") / Decimal("620.00"))

    def test_case_2_diff_110_rel_18_97_percent_is_good_value(
        self, dual_mode_run: _DualModeRun
    ) -> None:
        row = _direct_flight_analysis_row(dual_mode_run.report_text, "BOM")
        assert "Recommended (good value)" in row

        analysis = _destination_analysis_json(dual_mode_run.results, "BOM")
        assert analysis["tier"] == "good_value"
        assert analysis["recommendation"] == "Recommended (good value)"
        assert analysis["price_difference_eur"] == "110.00"
        assert analysis["relative_difference"] == str(Decimal("110.00") / Decimal("580.00"))

    def test_case_3_diff_300_rel_53_6_percent_is_not_recommended(
        self, dual_mode_run: _DualModeRun
    ) -> None:
        row = _direct_flight_analysis_row(dual_mode_run.report_text, "BLR")
        assert "Optional" in row
        # Never the star / good-value forms for this row.
        assert "★" not in row
        assert "good value" not in row

        analysis = _destination_analysis_json(dual_mode_run.results, "BLR")
        assert analysis["tier"] == "not_recommended"
        assert analysis["recommendation"] == "Optional"
        assert analysis["price_difference_eur"] == "300.00"
        assert analysis["relative_difference"] == str(Decimal("300.00") / Decimal("560.00"))


# ---------------------------------------------------------------------------
# 4. Final Summary sentence: EUR + hours-saved match the ranked/policy data.
# ---------------------------------------------------------------------------

_FINAL_SUMMARY_RE = re.compile(
    r"\*\*Final Summary:\*\* The direct .*? flight is only €(?P<eur>\d+\.\d{2}) more expensive "
    r"than the cheapest valid one-stop option"
    r"(?: and saves approximately (?P<hours>\d+) hours? of travel time)?"
    r"\. I recommend choosing the direct flight\."
)


class TestFinalSummarySentenceMatchesUnderlyingData:
    """A1-8's restated acceptance criterion (DECISIONS.md): the Final
    Summary sentence's EUR delta and hours-saved integer must both equal
    the values computed from the ranked/policy data -- parsed from the
    rendered sentence and read from the JSON side by side, not merely
    "some number is present"."""

    def test_top_ranked_itinerary_is_boms_direct_flight(self, dual_mode_run: _DualModeRun) -> None:
        # Ground truth this test's fares were hand-picked to produce (see
        # module docstring): BOM's direct itinerary (EUR690, 8h) has the
        # lowest adjusted_score (594) of all 15 generated itineraries.
        top = dual_mode_run.results["top_itineraries"][0]
        assert top["rank_by_adjusted_score"] == 1
        assert top["destination"] == "BOM"
        assert top["stop_count"] == 0
        assert top["price_eur"] == "690.00"
        assert top["score"]["adjusted_score"] == "594.00"

    def test_final_summary_eur_and_hours_match_the_ranked_and_policy_json(
        self, dual_mode_run: _DualModeRun
    ) -> None:
        match = _FINAL_SUMMARY_RE.search(dual_mode_run.report_text)
        assert match is not None, dual_mode_run.report_text

        analysis = _destination_analysis_json(dual_mode_run.results, "BOM")
        assert analysis["tier"] == "good_value"  # a "recommend direct" tier -> hours clause present

        # The EUR figure in the sentence must equal the JSON's own
        # price_difference_eur for the SAME (primary) destination.
        assert match.group("eur") == analysis["price_difference_eur"] == "110.00"

        # The hours-saved figure must equal time_saved_minutes rounded
        # half-up to the nearest whole hour (reporting.markdown's own
        # convention) -- computed here independently from the JSON minutes,
        # not merely copied from the expected constant.
        assert match.group("hours") is not None
        time_saved_minutes = analysis["time_saved_minutes"]
        assert time_saved_minutes == 330  # 13h30m one-stop - 8h direct = 5h30m
        expected_hours = (time_saved_minutes + 30) // 60
        assert expected_hours == 6
        assert int(match.group("hours")) == expected_hours


# ---------------------------------------------------------------------------
# 5. Byte-identical determinism, now with 16 calls and a real policy pass.
# ---------------------------------------------------------------------------


class TestByteIdenticalDualModeDeterminism:
    """Master plan §5's determinism guarantee ("run the full ... pipeline,
    diff both artifacts excluding run_meta, expect byte equality"),
    re-proven here now that 16 provider calls and the D10 policy
    computation (Phase 5) are in the loop, not just 8 calls and no policy
    (Phase 4's own byte-identical test, ``test_cli.py``'s
    ``TestTargetInvocation``-adjacent single-destination case). Each run
    gets its OWN directory and its OWN ``InstrumentedProvider`` instance,
    proving the equality is not an artifact of one run's files simply
    never having been touched by a second invocation."""

    def test_two_separate_runs_produce_byte_identical_artifacts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        first_dir = tmp_path / "run1"
        second_dir = tmp_path / "run2"
        first_dir.mkdir()
        second_dir.mkdir()

        first = _invoke_dual_mode(first_dir, monkeypatch)
        assert first.result.exit_code == 0, first.result.output

        second = _invoke_dual_mode(second_dir, monkeypatch)
        assert second.result.exit_code == 0, second.result.output

        assert len(first.provider.call_log) == 16
        assert len(second.provider.call_log) == 16

        first_report_bytes = (first_dir / "out" / _REPORT_FILENAME).read_bytes()
        second_report_bytes = (second_dir / "out" / _REPORT_FILENAME).read_bytes()
        first_results_bytes = (first_dir / "out" / _RESULTS_FILENAME).read_bytes()
        second_results_bytes = (second_dir / "out" / _RESULTS_FILENAME).read_bytes()

        assert first_report_bytes == second_report_bytes
        assert first_results_bytes == second_results_bytes
