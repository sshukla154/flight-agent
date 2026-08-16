"""Retry-with-backoff tests for ``flightagent.orchestration.executor`` (T25).

Master plan S5's retry policy, proved end to end through
``execute_plan`` + ``tests.support.instrumented_provider.InstrumentedProvider``:

1. A transient failure that clears before ``max_attempts`` succeeds, with
   ``attempts`` reflecting the REAL count (not a hardcoded 1 or max).
2. A permanent (``PERMANENT`` retryability) error is never retried, even
   though the caught exception's TYPE is nothing special -- only its
   ``.retryability`` classification matters.
3. A transient failure that never clears is retried exactly up to
   ``max_attempts`` and no further.
4. The computed backoff is full-jitter (``random.uniform(0, computed)``,
   never equal-jitter's ``random.uniform(computed/2, computed)``) and
   respects the exponential base/cap from config.
5. ``ProviderRateLimitedError.retry_after``, when present, overrides the
   computed backoff entirely.
6. The run-wide retry budget (``retry_budget_fraction``) caps total
   retries across the whole fan-out, stopping a task's retry even though
   that task's own per-task attempt count has not yet been exhausted.

``asyncio.sleep`` is monkeypatched everywhere a real delay would otherwise
occur, so these tests run in well under a second regardless of the
backoff values being asserted on.
"""

from __future__ import annotations

import asyncio
import random
from datetime import date, timedelta
from typing import Any

import pytest

from flightagent.config.loader import load_config
from flightagent.config.models import FlightAgentSettings
from flightagent.domain.enums import TaskState
from flightagent.orchestration.executor import execute_plan
from flightagent.orchestration.plan import build_plan_for_origin
from flightagent.providers.errors import (
    ProviderConfigError,
    ProviderRateLimitedError,
    ProviderTimeoutError,
)
from tests.support.instrumented_provider import Fail, InstrumentedProvider, Succeed

_ORIGIN = "AMS"
_DEPARTURE_DATE = date(2027, 7, 17)

# The 8 registered destinations, per airports.registry -- any of these
# works as a scripted destination; tests just need ONE (or a handful),
# picked by position so a test reads clearly ("the task under test").
_DESTINATIONS = ("DEL", "BOM", "BLR", "HYD", "MAA", "CCU", "LKO", "VNS")


def _settings(*, retry_budget_fraction: float | None = None) -> FlightAgentSettings:
    """Default config, optionally with ``retry_budget_fraction`` overridden.

    Most tests below are about PER-TASK retry mechanics, not the run-wide
    budget (that is its own test class) -- ``retry_budget_fraction=10.0``
    guarantees the budget itself is never the reason a retry doesn't
    happen, so those tests isolate exactly the mechanic they claim to
    test. Tests of the budget itself pass an explicit small fraction.
    """
    overrides: dict[str, Any] = {}
    if retry_budget_fraction is not None:
        overrides["retry"] = {"retry_budget_fraction": retry_budget_fraction}
    return load_config(env={}, cli_overrides=overrides)


def _tasks_for(destinations: tuple[str, ...]) -> tuple[Any, ...]:
    """The subset of a full 8-destination single-origin plan matching
    ``destinations``, in the order requested -- built from the real
    planner (``build_plan_for_origin``) so every ``SearchTask``/
    ``SearchRequest`` here is exactly what the executor sees in
    production, never a hand-rolled stand-in that could drift from the
    real schema.
    """
    all_tasks = build_plan_for_origin(_ORIGIN, departure_date=_DEPARTURE_DATE, max_stops=0)
    by_destination = {task.request.destination: task for task in all_tasks}
    return tuple(by_destination[destination] for destination in destinations)


def _patch_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace ``asyncio.sleep`` with a recording no-op; returns the list
    every call's ``delay`` argument is appended to, in call order."""
    delays: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    return delays


class TestRetrySucceedsWithinMaxAttempts:
    def test_fails_twice_then_succeeds_makes_exactly_three_attempts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        delays = _patch_sleep(monkeypatch)
        destination = _DESTINATIONS[0]
        provider = InstrumentedProvider(
            scripts={
                destination: [
                    Fail(ProviderTimeoutError("timeout 1", provider="instrumented")),
                    Fail(ProviderTimeoutError("timeout 2", provider="instrumented")),
                    Succeed(offer_count=2),
                ]
            }
        )
        tasks = _tasks_for((destination,))
        settings = _settings(retry_budget_fraction=10.0)

        results = asyncio.run(execute_plan(tasks, provider, settings=settings))

        assert len(results) == 1
        outcome = results[0].outcome
        assert outcome.attempts == 3
        assert outcome.state == TaskState.OK
        assert outcome.offer_count == 2
        assert len(results[0].offers) == 2
        assert provider.call_count(destination) == 3
        # Exactly 2 retries were taken (attempts 1 and 2 both failed), so
        # exactly 2 backoff delays were recorded.
        assert len(delays) == 2


class TestPermanentErrorIsNeverRetried:
    def test_config_error_stops_after_one_attempt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        delays = _patch_sleep(monkeypatch)
        destination = _DESTINATIONS[1]
        provider = InstrumentedProvider(
            scripts={
                destination: [
                    Fail(ProviderConfigError("bad credentials", provider="instrumented")),
                ]
            }
        )
        tasks = _tasks_for((destination,))
        settings = _settings(retry_budget_fraction=10.0)

        results = asyncio.run(execute_plan(tasks, provider, settings=settings))

        outcome = results[0].outcome
        assert outcome.attempts == 1
        assert outcome.state == TaskState.PROVIDER_ERROR
        assert outcome.error_type == "ProviderConfigError"
        assert provider.call_count(destination) == 1
        # A PERMANENT error must never even reach the backoff/sleep step.
        assert delays == []


class TestTransientErrorExhaustsMaxAttempts:
    def test_always_fails_stops_at_max_attempts_not_beyond(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_sleep(monkeypatch)
        destination = _DESTINATIONS[2]
        provider = InstrumentedProvider(
            scripts={
                destination: [
                    Fail(ProviderTimeoutError("always times out", provider="instrumented")),
                ]
            }
        )
        tasks = _tasks_for((destination,))
        settings = _settings(retry_budget_fraction=10.0)

        results = asyncio.run(execute_plan(tasks, provider, settings=settings))

        outcome = results[0].outcome
        assert outcome.attempts == settings.retry.max_attempts
        assert outcome.attempts == 3
        assert outcome.state == TaskState.TIMEOUT
        assert outcome.state != TaskState.OK
        assert provider.call_count(destination) == settings.retry.max_attempts


class TestBackoffIsFullJitterWithinExponentialBounds:
    def test_recorded_delays_fall_within_full_jitter_bounds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        delays = _patch_sleep(monkeypatch)
        destination = _DESTINATIONS[3]
        provider = InstrumentedProvider(
            scripts={
                destination: [
                    Fail(ProviderTimeoutError("timeout 1", provider="instrumented")),
                    Fail(ProviderTimeoutError("timeout 2", provider="instrumented")),
                    Succeed(offer_count=1),
                ]
            }
        )
        tasks = _tasks_for((destination,))
        settings = _settings(retry_budget_fraction=10.0)
        base = float(settings.retry.backoff_base_seconds)
        cap = float(settings.retry.backoff_cap_seconds)

        asyncio.run(execute_plan(tasks, provider, settings=settings))

        assert len(delays) == 2
        # Retry before attempt 2 (n=0): computed = min(cap, base * 2**0).
        expected_first_ceiling = min(cap, base * (2**0))
        # Retry before attempt 3 (n=1): computed = min(cap, base * 2**1).
        expected_second_ceiling = min(cap, base * (2**1))
        assert 0.0 <= delays[0] <= expected_first_ceiling
        assert 0.0 <= delays[1] <= expected_second_ceiling

    def test_full_jitter_draws_from_zero_not_half_of_computed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Full jitter is ``random.uniform(0, computed)``. Equal jitter --
        the thing master plan S5 explicitly says NOT to use -- would be
        ``random.uniform(computed / 2, computed)``. Patching ``random.uniform``
        itself (rather than only inspecting the delay it returns) proves
        the LOWER bound passed really is 0, not half of the computed
        ceiling, which a delay-range assertion alone could not distinguish
        from a narrower equal-jitter implementation that happened to draw
        a low value.
        """
        _patch_sleep(monkeypatch)

        captured_bounds: list[tuple[float, float]] = []
        real_uniform = random.uniform

        def _recording_uniform(low: float, high: float) -> float:
            captured_bounds.append((low, high))
            return real_uniform(low, high)

        monkeypatch.setattr("flightagent.orchestration.executor.random.uniform", _recording_uniform)

        destination = _DESTINATIONS[4]
        provider = InstrumentedProvider(
            scripts={
                destination: [
                    Fail(ProviderTimeoutError("timeout 1", provider="instrumented")),
                    Succeed(offer_count=1),
                ]
            }
        )
        tasks = _tasks_for((destination,))
        settings = _settings(retry_budget_fraction=10.0)
        base = float(settings.retry.backoff_base_seconds)

        asyncio.run(execute_plan(tasks, provider, settings=settings))

        assert len(captured_bounds) == 1
        low, high = captured_bounds[0]
        assert low == 0.0
        assert high == pytest.approx(base)


class TestRetryAfterOverridesComputedBackoff:
    def test_rate_limited_retry_after_is_used_verbatim(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        delays = _patch_sleep(monkeypatch)
        destination = _DESTINATIONS[5]
        provider = InstrumentedProvider(
            scripts={
                destination: [
                    Fail(
                        ProviderRateLimitedError(
                            "429 from provider",
                            provider="instrumented",
                            retry_after=timedelta(seconds=5),
                        )
                    ),
                    Succeed(offer_count=1),
                ]
            }
        )
        tasks = _tasks_for((destination,))
        # backoff_base_seconds/backoff_cap_seconds default to 0.5/8.0 --
        # retry_after=5s would not even be reachable by the computed
        # formula's ceiling at attempt 1 (0.5s), so a delay of exactly 5.0
        # is unambiguous proof retry_after was used, not the computed value.
        settings = _settings(retry_budget_fraction=10.0)

        results = asyncio.run(execute_plan(tasks, provider, settings=settings))

        assert len(delays) == 1
        assert delays[0] == 5.0
        outcome = results[0].outcome
        assert outcome.attempts == 2
        assert outcome.state == TaskState.OK


class TestRunWideRetryBudget:
    def test_budget_exhaustion_stops_retries_before_per_task_limit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """8 tasks, every destination scripted to fail TRANSIENT forever.
        ``retry_budget_fraction=0.25`` over 8 tasks gives a budget of
        exactly ``int(8 * 0.25) == 2`` total retries for the WHOLE run.

        Concurrency is forced to 1 so execution is strictly serial and
        deterministic (task 0 runs to completion before task 1 starts --
        see the executor's ``asyncio.Semaphore`` with a permit count of
        1, and neither the patched ``asyncio.sleep`` nor
        ``InstrumentedProvider.search`` ever actually suspends, so nothing
        interleaves). Task 0 alone can therefore spend the entire 2-retry
        budget reaching its own ``max_attempts`` ceiling (3 attempts), and
        every task after it gets 0 retries -- exactly 1 attempt each --
        even though each of THOSE tasks' own per-task attempt budget
        (``max_attempts=3``) was nowhere near exhausted. That is the run-
        wide cap actually capping, not each task's own limit.
        """
        _patch_sleep(monkeypatch)
        provider = InstrumentedProvider(
            scripts={
                destination: [
                    Fail(ProviderTimeoutError(f"{destination} always times out", provider="x"))
                ]
                for destination in _DESTINATIONS
            }
        )
        tasks = _tasks_for(_DESTINATIONS)
        assert len(tasks) == 8
        settings = _settings(retry_budget_fraction=0.25)
        expected_budget = int(len(tasks) * settings.retry.retry_budget_fraction)
        assert expected_budget == 2

        cli_overrides = {"concurrency": {"max_concurrent_searches": 1}}
        serial_settings = load_config(
            env={}, cli_overrides={**cli_overrides, "retry": {"retry_budget_fraction": 0.25}}
        )

        results = asyncio.run(execute_plan(tasks, provider, settings=serial_settings))

        assert len(results) == 8
        attempts = [result.outcome.attempts for result in results]

        # Total attempts made = 8 baseline (1 per task) + however many
        # retries the shared budget actually paid for.
        assert sum(attempts) == len(tasks) + expected_budget

        # No task ever exceeds its own per-task ceiling.
        assert all(a <= serial_settings.retry.max_attempts for a in attempts)

        # At least one task was cut off at 1 attempt purely by the spent
        # budget, despite max_attempts (3) allowing more for that task in
        # isolation -- proof the run-wide cap, not the per-task cap, is
        # what stopped it.
        assert attempts.count(1) >= 1
        assert any(a > 1 for a in attempts), "expected the budget to have funded at least one retry"

        # None of the results reached OK -- every task in this test is
        # scripted to fail on every call.
        assert all(result.outcome.state == TaskState.TIMEOUT for result in results)
