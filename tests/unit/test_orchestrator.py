"""Smoke tests for ``flightagent.orchestration`` (T24).

Two things this file proves, per the task brief:

1. ``executor.execute_plan`` actually bounds concurrency at the configured
   semaphore size — proved by an inline fake provider that tracks its own
   concurrent-call count, run at two different semaphore sizes, asserting
   the OBSERVED peak matches each bound exactly (not just "<=", which would
   also pass for an executor that accidentally ran everything serially).
2. The task ledger is complete and correctly shaped: exactly 8 tasks planned
   for 8 destinations, exactly one ``TaskOutcome`` per task, and a
   ``ProviderTimeoutError`` maps to ``TaskState.TIMEOUT``.

The fake provider here is deliberately trivial (an inline async class
satisfying the structural ``FlightProvider`` protocol) — the richer,
instrumented double that records full call logs is T25's job, in the next
wave.
"""

from __future__ import annotations

import asyncio
from datetime import date

from flightagent.airports.registry import destinations
from flightagent.airports.registry import get as get_airport
from flightagent.config.loader import load_config
from flightagent.domain.enums import TaskState
from flightagent.domain.run import SearchRequest
from flightagent.orchestration.executor import execute_plan
from flightagent.orchestration.plan import build_plan_for_origin
from flightagent.providers.base import CallBudget, ProviderCapabilities, ProviderSearchResult
from flightagent.providers.errors import ProviderTimeoutError

_ORIGIN = "AMS"


def _departure_date() -> date:
    return date(2027, 7, 17)


def _fake_capabilities() -> ProviderCapabilities:
    return ProviderCapabilities(
        provider_name="fake",
        api_version="test-v1",
        auth_style="none",
        paginated=False,
        native_currency_forceable=True,
        returns_booking_url=False,
        stop_filter_style="nonstop_boolean",
    )


class _ConcurrencyTrackingProvider:
    """Trivial ``FlightProvider`` -- sleeps briefly, tracking how many calls
    were in flight at once (``current``) and the highest that ever reached
    (``peak``). Satisfies the protocol structurally: no inheritance, just
    the right ``capabilities`` property and ``search`` coroutine shape.
    """

    def __init__(self, *, sleep_seconds: float = 0.05) -> None:
        self._sleep_seconds = sleep_seconds
        self.current = 0
        self.peak = 0

    @property
    def capabilities(self) -> ProviderCapabilities:
        return _fake_capabilities()

    async def search(self, request: SearchRequest, budget: CallBudget) -> ProviderSearchResult:
        self.current += 1
        self.peak = max(self.peak, self.current)
        try:
            await asyncio.sleep(self._sleep_seconds)
        finally:
            self.current -= 1
        return ProviderSearchResult(offers=(), truncated=False, pages_fetched=1, http_calls=1)


class _AlwaysTimeoutProvider:
    """Trivial ``FlightProvider`` whose every call raises
    ``ProviderTimeoutError`` -- for proving the executor's error-state
    mapping, not concurrency.
    """

    @property
    def capabilities(self) -> ProviderCapabilities:
        return _fake_capabilities()

    async def search(self, request: SearchRequest, budget: CallBudget) -> ProviderSearchResult:
        raise ProviderTimeoutError("no response in budget", provider="fake")


class TestBuildPlanForOrigin:
    def test_plans_exactly_one_task_per_destination(self) -> None:
        tasks = build_plan_for_origin(_ORIGIN, departure_date=_departure_date(), max_stops=1)
        assert len(tasks) == 8
        assert len(tasks) == len(destinations())

    def test_each_task_carries_the_origin_registry_priority_and_wave_zero(self) -> None:
        tasks = build_plan_for_origin(_ORIGIN, departure_date=_departure_date(), max_stops=1)
        expected_priority = get_airport(_ORIGIN).priority
        assert expected_priority is not None
        for task in tasks:
            assert task.origin_priority == expected_priority
            assert task.wave == 0
            assert task.request.origin == _ORIGIN

    def test_task_ids_follow_the_deterministic_convention(self) -> None:
        tasks = build_plan_for_origin(_ORIGIN, departure_date=_departure_date(), max_stops=0)
        for task in tasks:
            assert task.task_id == f"{_ORIGIN}-{task.request.destination}-s0"

    def test_covers_every_destination_exactly_once(self) -> None:
        tasks = build_plan_for_origin(_ORIGIN, departure_date=_departure_date(), max_stops=1)
        planned_destinations = {task.request.destination for task in tasks}
        registry_destinations = {airport.iata for airport in destinations()}
        assert planned_destinations == registry_destinations


class TestExecutePlanConcurrencyBound:
    def test_peak_concurrency_matches_a_bound_of_two(self) -> None:
        tasks = build_plan_for_origin(_ORIGIN, departure_date=_departure_date(), max_stops=1)
        provider = _ConcurrencyTrackingProvider(sleep_seconds=0.05)
        settings = load_config(
            env={}, cli_overrides={"concurrency": {"max_concurrent_searches": 2}}
        )

        results = asyncio.run(execute_plan(tasks, provider, settings=settings))

        assert provider.peak == 2
        assert len(results) == 8

    def test_peak_concurrency_matches_a_bound_of_one(self) -> None:
        """A different (tighter) bound must produce a different observed
        peak -- proof the semaphore size is actually read from
        ``settings``, not a coincidence of an ``<=`` assertion that would
        also pass for an executor that never bounded anything at all.
        """
        tasks = build_plan_for_origin(_ORIGIN, departure_date=_departure_date(), max_stops=1)
        provider = _ConcurrencyTrackingProvider(sleep_seconds=0.05)
        settings = load_config(
            env={}, cli_overrides={"concurrency": {"max_concurrent_searches": 1}}
        )

        asyncio.run(execute_plan(tasks, provider, settings=settings))

        assert provider.peak == 1


class TestExecutePlanLedgerCompleteness:
    def test_exactly_one_outcome_per_task(self) -> None:
        tasks = build_plan_for_origin(_ORIGIN, departure_date=_departure_date(), max_stops=0)
        provider = _ConcurrencyTrackingProvider(sleep_seconds=0.01)

        results = asyncio.run(execute_plan(tasks, provider))

        assert len(results) == len(tasks)
        result_task_ids = {result.outcome.task_id for result in results}
        planned_task_ids = {task.task_id for task in tasks}
        assert result_task_ids == planned_task_ids

    def test_successful_no_offers_call_produces_no_offers_state(self) -> None:
        tasks = build_plan_for_origin(_ORIGIN, departure_date=_departure_date(), max_stops=0)
        provider = _ConcurrencyTrackingProvider(sleep_seconds=0.0)

        results = asyncio.run(execute_plan(tasks, provider))

        for result in results:
            assert result.outcome.state == TaskState.NO_OFFERS
            assert result.outcome.offer_count == 0
            assert result.outcome.accepted_count == 0
            assert result.outcome.attempts == 1
            assert result.offers == ()


class TestExecutePlanErrorHandling:
    def test_provider_timeout_error_produces_timeout_outcome(self) -> None:
        tasks = build_plan_for_origin(_ORIGIN, departure_date=_departure_date(), max_stops=1)[:1]
        provider = _AlwaysTimeoutProvider()

        results = asyncio.run(execute_plan(tasks, provider))

        assert len(results) == 1
        outcome = results[0].outcome
        assert outcome.state == TaskState.TIMEOUT
        assert outcome.attempts == 1
        assert outcome.error_type == "ProviderTimeoutError"
        assert outcome.error_detail == "no response in budget"
        assert outcome.offer_count == 0
        assert outcome.accepted_count == 0
        assert results[0].offers == ()
