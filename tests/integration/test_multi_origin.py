"""Integration: the full 160-cell multi-origin fan-out through the real CLI
(T42) -- Phase 6's closing proof that T37 (planning/waves), T38 (ground
filter), T39 (early-stop annotation) and T40/T41 (Origin Comparison) all
compose correctly at full scale through ``flightagent run --all-origins
--all-destinations``, not merely in unit-level isolation.

Two things discovered by reading the actual code (not just the master plan)
before writing anything here, both documented at their point of use below
rather than only in this docstring:

1. ``cli.py``'s ``--all-origins`` path (``_run_all_destinations`` ->
   ``orchestration.plan.build_multi_origin_plan``) reads
   ``airports.registry.origins()`` directly and UNFILTERED -- it never
   calls ``orchestration.ground_filter.plannable_origins``/
   ``filter_origins_by_ground_limit`` at all. T38's 150-minute filter exists
   and is unit-tested (``tests/unit/test_ground.py``), but today's shipped
   CLI does not wire it in, so an over-limit 11th origin added to the
   registry would still be searched by the real CLI as of this commit. This
   file's ground-filter test therefore exercises the filter composed
   directly with the real planner (``orchestration.plan``), not through the
   CLI -- see ``TestGroundFilterExcludesSyntheticEleventhOriginIntegration``
   below.

2. ``policy.early_stop._MIN_PRIOR_ORIGINS = 2`` (that module's own
   docstring, verbatim: "the 1st and 2nd origins (by priority) considered
   for a destination can never trigger, regardless of price") makes it
   structurally impossible for EIN -- priority 2, with only ONE origin
   (AMS) ever able to precede it in priority order -- to itself ever be
   ``triggering_origin``, for any fixture whatsoever: the rule cannot even
   attempt a comparison until 2 EARLIER origins already carry a valid fare,
   and EIN never has more than one in front of it. RTM (priority 3) is the
   earliest origin the rule can ever evaluate. See
   ``TestEarlyStopAnnotationReplaysCorrectly``'s own class docstring below
   for how this file's fixture is built to respect that invariant while
   still proving EIN's cheaper fare correctly becomes the comparison
   baseline a later origin's still-cheaper fare triggers against.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flightagent.airports import registry
from flightagent.airports.registry import Airport
from flightagent.cli import app
from flightagent.config.loader import load_config
from flightagent.domain.enums import RejectionCode
from flightagent.domain.ground import GroundLeg
from flightagent.domain.money import Money
from flightagent.domain.run import SearchRequest, SearchTask
from flightagent.orchestration.ground_filter import filter_origins_by_ground_limit
from flightagent.orchestration.plan import build_dual_mode_plan_for_origin
from flightagent.providers.base import CallBudget, ProviderSearchResult
from tests.support.instrumented_provider import InstrumentedProvider, Succeed

runner = CliRunner()

_DEPARTURE_DATE = "2027-07-17"

_ALL_ORIGINS_ALL_DESTINATIONS_ARGS = [
    "run",
    "--origin",
    "AMS",  # ignored once --all-origins is set (cli.py's own --help text)
    "--date",
    _DEPARTURE_DATE,
    "--max-stops",
    "1",
    "--all-destinations",
    "--all-origins",
]

_EXPECTED_ORIGIN_ORDER = ("AMS", "EIN", "RTM", "DUS", "BRU", "NRN", "CGN", "CRL", "MST", "GRQ")
"""Priority order (DECISIONS.md Addendum 2 / ``airports.registry.origins()``)
-- T37's wave-1 group is exactly the first three of these."""

_EXPECTED_DESTINATIONS = tuple(airport.iata for airport in registry.destinations())


@pytest.fixture
def isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run the CLI from an isolated directory so ``out/...`` never touches
    the real repo's ``out/`` directory -- matching every other integration
    test's own identical fixture (e.g. ``tests/integration/test_orchestrator.py``).
    """
    monkeypatch.chdir(tmp_path)
    return tmp_path


class _PriceControlledProvider(InstrumentedProvider):
    """``InstrumentedProvider`` (T25/T29) plus a per-(origin, destination)
    price override applied identically to BOTH stop modes.

    ``InstrumentedProvider.scripts`` is keyed by destination ALONE (correct
    for every earlier, single-origin phase it was built for) -- it has no
    origin axis, so it cannot express "AMS's fare to DEL is X, EIN's fare to
    the SAME destination is Y", which T39's early-stop replay fixture below
    needs. This subclass delegates every bit of call-log/peak-concurrency
    bookkeeping to ``InstrumentedProvider.search`` completely UNCHANGED,
    then -- only for the ``(origin, destination)`` pairs named in
    ``price_overrides`` -- replaces the returned offers' price with the
    exact target amount, leaving every other field (shape, layover,
    airline, segment times) exactly as the real mock generator produced it,
    so the offer stays schema-valid and still passes the real validation
    engine on its own merits.
    """

    def __init__(
        self, *, price_overrides: dict[tuple[str, str], Decimal], **kwargs: object
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._price_overrides = price_overrides

    async def search(self, request: SearchRequest, budget: CallBudget) -> ProviderSearchResult:
        result = await super().search(request, budget)
        override = self._price_overrides.get((request.origin, request.destination))
        if override is None or not result.offers:
            return result
        overridden_offers = tuple(
            offer.model_copy(update={"price": Money(amount=override, currency=request.currency)})
            for offer in result.offers
        )
        return result.model_copy(
            update={
                "offers": overridden_offers,
                "raw_payload_refs": tuple(offer.raw_payload_ref for offer in overridden_offers),
            }
        )


class TestFullMultiOriginFanOutThroughRealCli:
    """T42: the literal 160-cell fan-out -- 10 origins x 8 destinations x 2
    stop modes -- through ``flightagent run --all-origins --all-destinations``,
    proving the executor's bounded concurrency (T24/T25), T37's wave/priority
    ordering, and T40/T41's Origin Comparison table all compose correctly at
    full scale, in a single real run, not merely in unit-level isolation.
    """

    def test_160_calls_bounded_concurrency_wave_order_and_origin_comparison(
        self, isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert len(_EXPECTED_ORIGIN_ORDER) == 10
        assert len(_EXPECTED_DESTINATIONS) == 8

        settings = load_config()
        assert settings.concurrency.max_concurrent_searches == 8
        # D12 default -- no --search-mode flag exists to change this (T39's
        # own scope note), so a plain --all-origins run always has this off.
        assert settings.early_stop.enabled is False

        provider = InstrumentedProvider(
            scripts={
                destination: [Succeed(offer_count=2)] for destination in _EXPECTED_DESTINATIONS
            },
            # Long enough that 8 concurrently-dispatched calls genuinely
            # OVERLAP in wall-clock time -- a (buggy) fully serial executor
            # would also satisfy "peak_in_flight <= 8" but could never
            # reach exactly 8 (see tests/integration/test_orchestrator.py's
            # identical reasoning). Short enough that 20 batches (160 tasks
            # / 8 concurrency) still finish in well under a second.
            call_delay_seconds=0.02,
        )
        monkeypatch.setattr("flightagent.cli._build_provider", lambda name: provider)

        result = runner.invoke(app, _ALL_ORIGINS_ALL_DESTINATIONS_ARGS)
        assert result.exit_code == 0, result.output

        # -- EXACTLY 160 provider calls, verified via the instrumented
        # provider's own call log, never inferred from the report. --
        assert len(provider.call_log) == 160
        assert {record.origin for record in provider.call_log} == set(_EXPECTED_ORIGIN_ORDER)
        for origin in _EXPECTED_ORIGIN_ORDER:
            assert sum(1 for record in provider.call_log if record.origin == origin) == 16

        # -- Peak concurrency: bounded by, and genuinely REACHES, the
        # configured 8 -- proof of real overlap, not serial execution that
        # happens to also satisfy "<= 8". --
        assert provider.peak_in_flight <= settings.concurrency.max_concurrent_searches
        assert provider.peak_in_flight == settings.concurrency.max_concurrent_searches

        # -- Wave/priority order (T37): AMS+EIN+RTM (wave 1, 48 tasks) are
        # dispatched, essentially as a block, before DUS (wave 2). Semaphore
        # FIFO ordering guarantees the DISPATCH (acquisition) order of the
        # underlying tasks, but call_log append order is a race once a
        # wave-2 task acquires a freed slot while a few wave-1 stragglers it
        # was queued behind are still finishing their own call (real,
        # legitimate concurrency -- not a reordering bug) -- so a handful of
        # wave-2 entries can legitimately interleave right at the boundary.
        # Bounded by the configured concurrency itself: at most
        # max_concurrent_searches stragglers can ever be "still in flight"
        # when the first wave-2 task's slot frees.
        first_48_log = provider.call_log[:48]
        boundary_overlap = [
            record for record in first_48_log if record.origin not in {"AMS", "EIN", "RTM"}
        ]
        assert len(boundary_overlap) <= settings.concurrency.max_concurrent_searches
        assert {record.origin for record in boundary_overlap} <= {"DUS"}
        # And the reverse must never happen: no wave-1 origin call is still
        # missing by the time DUS's own calls start appearing.
        dus_first_index = next(
            index for index, record in enumerate(provider.call_log) if record.origin == "DUS"
        )
        wave_1_calls_before_dus = sum(
            1
            for record in provider.call_log[:dus_first_index]
            if record.origin in {"AMS", "EIN", "RTM"}
        )
        assert wave_1_calls_before_dus >= 48 - settings.concurrency.max_concurrent_searches

        results_path = isolated_cwd / "out" / "flight_results_2027-07-17.json"
        report_path = isolated_cwd / "out" / "flight_report_2027-07-17.md"
        assert results_path.is_file()
        assert report_path.is_file()
        results = json.loads(results_path.read_text(encoding="utf-8"))

        # -- Early stop disabled by default: the full 160-call fan-out
        # above happened UNCONDITIONALLY, regardless of what the post-hoc
        # annotation reports for any given destination. --
        assert len(results["early_stop_analysis"]) == 8
        assert all(row["mode"] == "advisory" for row in results["early_stop_analysis"])

        # -- Origin Comparison (T40/T41): all 10 rows, in priority order,
        # each with a real fare -- never silently dropped. --
        origin_rows = results["origin_comparison"]
        assert [row["origin"] for row in origin_rows] == list(_EXPECTED_ORIGIN_ORDER)
        assert all(row["cheapest_fare_eur"] is not None for row in origin_rows)

        report_text = report_path.read_text(encoding="utf-8")
        assert "## Origin Comparison" in report_text
        for origin in _EXPECTED_ORIGIN_ORDER:
            assert origin in report_text


class TestEarlyStopAnnotationReplaysCorrectly:
    """T39/D12: the post-hoc early-stop replay correctly identifies a
    trigger and reports the right margin/compared_against, while D12's
    default (``enabled=false``) still means every one of the 160 calls
    actually ran regardless.

    Fixture: AMS's fare to DEL = EUR900.00, EIN's fare to DEL = EUR640.00
    (exactly 260 EUR cheaper than AMS's), RTM's fare to DEL = EUR380.00
    (260 EUR cheaper than EIN's, the cheaper of the first two).

    Per this module's own docstring (point 2), EIN -- priority 2 -- can
    never itself be ``triggering_origin``: the rule needs 2 EARLIER
    valid-fare origins before it will even attempt a comparison, and only
    AMS precedes EIN. RTM (priority 3) is the earliest origin the rule can
    ever evaluate, and that is exactly where this fixture's trigger fires --
    comparing RTM's EUR380.00 against ``min(AMS, EIN)`` = EIN's EUR640.00,
    a EUR260.00 margin, which is what this test verifies: EIN's cheaper
    fare correctly becomes the beaten comparison baseline
    (``compared_against``), with the right margin attributed to the
    earliest origin the rule's own design allows to trigger.
    """

    _DESTINATION = "DEL"

    def test_annotation_triggers_at_rtm_against_eins_cheaper_baseline_all_origins_still_searched(
        self, isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = load_config()
        assert settings.early_stop.enabled is False
        assert settings.early_stop.threshold_eur == Decimal("250.0")

        price_overrides = {
            ("AMS", self._DESTINATION): Decimal("900.00"),
            ("EIN", self._DESTINATION): Decimal("640.00"),
            ("RTM", self._DESTINATION): Decimal("380.00"),
        }
        provider = _PriceControlledProvider(
            price_overrides=price_overrides,
            scripts={
                destination: [Succeed(offer_count=2)] for destination in _EXPECTED_DESTINATIONS
            },
        )
        monkeypatch.setattr("flightagent.cli._build_provider", lambda name: provider)

        result = runner.invoke(app, _ALL_ORIGINS_ALL_DESTINATIONS_ARGS)
        assert result.exit_code == 0, result.output

        # D12 default: every one of the 160 calls actually happened,
        # regardless of what the annotation below reports -- the replay
        # never skips, cancels, or reorders anything (orchestration.waves'
        # own docstring).
        assert len(provider.call_log) == 160
        assert {record.origin for record in provider.call_log} == set(_EXPECTED_ORIGIN_ORDER)
        del_calls = provider.calls_for(self._DESTINATION)
        assert {call.origin for call in del_calls} == set(_EXPECTED_ORIGIN_ORDER)
        assert len(del_calls) == 20  # 10 origins x 2 stop modes, all still searched

        results_path = isolated_cwd / "out" / "flight_results_2027-07-17.json"
        results = json.loads(results_path.read_text(encoding="utf-8"))

        early_stop_by_destination = {
            row["destination"]: row for row in results["early_stop_analysis"]
        }
        entry = early_stop_by_destination[self._DESTINATION]

        assert entry["triggered"] is True
        assert entry["triggering_origin"] == "RTM"
        assert entry["margin_eur"] == "260.00"
        assert entry["compared_against"] == ["AMS", "EIN"]
        assert entry["mode"] == "advisory"
        # AMS/EIN/RTM (priorities 1-3) all collapse to wave 1 (T37).
        assert entry["evaluated_at_wave"] == 1


def _synthetic_origin(iata: str, *, ground_minutes: int, priority: int = 99) -> Airport:
    """A wholly synthetic origin ``Airport`` -- master plan S6's own reason
    the ground-travel filter must exist even though it never fires against
    today's real 10-origin roster ("someone will add an eleventh airport"),
    matching ``tests/unit/test_ground.py``'s own identical helper (not
    imported from there: that module has no ``__init__.py`` and is not a
    real importable package, per this test tree's rootless layout -- see
    ``tests/conftest.py``).
    """
    return Airport(
        iata=iata,
        name=f"Synthetic {iata}",
        city="Nowhere",
        country="Testland",
        iana_tz="UTC",
        lat=0.0,
        lon=0.0,
        priority=priority,
        ground=GroundLeg(
            from_location="Nieuwegein, Utrecht, NL",
            to_airport=iata,
            mode="car",
            duration=timedelta(minutes=ground_minutes),
            distance_km=Decimal("300"),
            cost=Money(amount=Decimal("60.00"), currency="EUR"),
            source="estimate",
            as_of=date(2026, 8, 14),
        ),
    )


class TestGroundFilterExcludesSyntheticEleventhOriginIntegration:
    """T38's 150-minute ground-travel hard filter, composed with the REAL
    planner (``orchestration.plan.build_dual_mode_plan_for_origin``) --
    proving the filtered-origin set feeds a genuinely valid multi-origin
    task list, not just that the raw filter function alone rejects the
    right ``Airport`` in isolation (already covered at the unit level,
    ``tests/unit/test_ground.py``).

    Deliberately NOT run through ``flightagent run --all-origins`` -- see
    this module's own docstring, point 1: the shipped CLI's ``--all-origins``
    path never calls ``orchestration.ground_filter`` at all, so there is no
    CLI invocation that would actually exercise this filter today. This
    test instead proves the filter and planner correctly COMPOSE when
    wired together directly, exactly as a future CLI change would need to
    wire them.
    """

    def test_synthetic_eleventh_origin_excluded_ten_real_origins_plan_normally(self) -> None:
        settings = load_config()
        assert settings.ground_travel.max_ground_travel_minutes == 150

        eleventh = _synthetic_origin("ZZZ", ground_minutes=200)
        plannable, rejections = filter_origins_by_ground_limit(
            [*registry.origins(), eleventh],
            max_ground_travel_minutes=settings.ground_travel.max_ground_travel_minutes,
        )

        # All 10 real origins pass through unaffected, in priority order.
        assert [airport.iata for airport in plannable] == list(_EXPECTED_ORIGIN_ORDER)

        # The synthetic 11th is excluded, with a proper Rejection.
        assert len(rejections) == 1
        rejection = rejections[0]
        assert rejection.code == RejectionCode.GROUND_TRAVEL_EXCEEDED
        assert rejection.rule_id == "ground_travel_limit"
        assert "ZZZ" in rejection.message
        assert rejection.observed == "200"
        assert rejection.expected == "<= 150"

        # Composition proof: feed the FILTERED origin list into the real
        # planner, exactly as a CLI wiring it in would -- the resulting
        # task list is the full, untainted 160-task fan-out (10 origins x
        # 8 destinations x 2 modes), with no ZZZ task anywhere in it.
        departure_date = date(2027, 7, 17)
        tasks: list[SearchTask] = []
        for airport in plannable:
            tasks.extend(
                build_dual_mode_plan_for_origin(
                    airport.iata, departure_date=departure_date, settings=settings
                )
            )

        assert len(tasks) == 160
        task_origins = {task.request.origin for task in tasks}
        assert task_origins == set(_EXPECTED_ORIGIN_ORDER)
        assert "ZZZ" not in task_origins
