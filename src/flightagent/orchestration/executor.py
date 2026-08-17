"""Bounded-concurrency task execution with retry and caching (T24 + T25 + T43 wiring).

Master plan S5's layered per-task controls, outermost in: semaphore ->
``asyncio.timeout`` -> circuit breaker -> retry loop -> token bucket -> HTTP.
T24 built the outermost layer — the semaphore-bounded fan-out. This module
now also builds the retry loop: up to ``settings.retry.max_attempts``
attempts per task, full-jitter exponential backoff (honouring
``ProviderRateLimitedError.retry_after`` when present), a run-wide retry
budget shared across the whole fan-out, and the ``SEARCH_RETRY`` event
before each retried attempt. The circuit breaker and token bucket layers
remain out of scope here.

**Cache wiring (closing a real gap a dual verify pass found in T43/T44):**
``persistence.cache_repo.CacheRepository`` was built and unit/integration
tested in T43/T44, but nothing in this module ever called it — a real
second identical CLI run still made 160 live provider calls, not the
master plan's own literal Phase 7 completion bar ("second identical run
issues 0 provider calls, logs cache_hit for all keys"). ``_search_and_record``
now composes the REAL ``CacheRepository``/``compute_raw_key``/``resolve_ttl``
the exact way ``tests/integration/test_cache.py``'s own ``_search_with_cache``
test-local helper already proved works — cache-aside over the RAW layer
only (a live provider call on a miss, a served ``payload`` string on a
hit), never the normalized layer (that belongs to a later, per-itinerary
concern this module does not own). ``cache`` is an OPTIONAL parameter
threaded through ``execute_plan`` -> ``_run_one_task`` -> ``_search_and_record``
(``None`` means "no cache configured", the exact behaviour every existing
caller/test already exercises — this wiring is additive, not a breaking
change to the function signatures).

``asyncio.TaskGroup``, not ``asyncio.gather`` (master plan S5, verbatim):
proper cancellation semantics, and it does not silently swallow a second
exception the way ``gather`` does when one task raises while others are
still in flight.

Master plan S5: "Each task records exactly one terminal ``TaskOutcome``; an
exception escaping is a bug, not an outcome." That is why ``_run_one_task``
only catches ``ProviderError`` (the closed, expected taxonomy from
``providers/errors.py``) — anything else escaping a task is a genuine bug
in this codebase and is deliberately left to propagate through
``TaskGroup`` as an ``ExceptionGroup``, not converted into a plausible-
looking outcome that would hide it.

Neither ``TaskOutcome.accepted_count``/``rejection_counts`` nor
``TaskState.ALL_REJECTED`` are decided here — those require running
normalize+validate, a later layer this module does not call. Every
outcome this module produces is provisional in exactly that one respect;
whichever later step assembles the ``RunEnvelope`` finalizes them.
"""

from __future__ import annotations

import asyncio
import random
import time
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from flightagent.config.loader import load_config
from flightagent.config.models import CacheSettings, FlightAgentSettings, RetrySettings
from flightagent.domain.enums import TaskState
from flightagent.domain.itinerary import RawOffer
from flightagent.domain.run import SearchTask, TaskOutcome
from flightagent.observability.context import task_context
from flightagent.observability.events import EventName
from flightagent.observability.logging import log_event
from flightagent.persistence.cache_repo import CacheRepository, resolve_ttl
from flightagent.persistence.keys import compute_raw_key
from flightagent.providers.base import (
    CallBudget,
    FlightProvider,
    ProviderSearchResult,
    deserialize_cached_search_result,
)
from flightagent.providers.errors import (
    ProviderError,
    ProviderRateLimitedError,
    ProviderTimeoutError,
    Retryability,
)

_BYPASS_CACHE_STATE = "bypass"
"""No ``CacheRepository`` was supplied to this call at all -- caching is
entirely optional (``cache=None`` is every existing caller's/test's
behaviour, unchanged) rather than something every run is forced to set
up. Distinct from ``"miss"`` (a cache WAS consulted and found nothing),
which would overstate what happened when no cache was configured."""

_PLACEHOLDER_HTTP_STATUS = 200
"""``FlightProvider.search`` is deliberately coarse (master plan S5) and
never surfaces a real HTTP status code to its caller. ``200`` stands in for
"the call completed without a ``ProviderError``" — the closest honest
reading of ``SearchResponseFields.http_status`` available at this layer."""

_PLACEHOLDER_RESPONSE_BYTES = 0
"""Same reasoning as ``_PLACEHOLDER_HTTP_STATUS``: ``ProviderSearchResult``
carries no response-size field, so there is nothing real to report here."""


class TaskExecutionResult(BaseModel):
    """One executed ``SearchTask``'s ledger entry plus its raw offers.

    Bundles ``TaskOutcome`` (always present — exactly one per input task)
    with whatever ``RawOffer``s the provider returned, so a caller gets
    both the ledger AND the offers needed for the next wave's
    normalize+validate step from a single executor call, instead of having
    to re-derive one from the other.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: TaskOutcome
    offers: tuple[RawOffer, ...] = ()


class RetryBudget:
    """Run-wide cap on total retries across one ``execute_plan`` fan-out.

    Master plan S5: "without this cap a single broken provider can turn a
    160-task run into a 480-call quota fire." ``total`` is
    ``int(task_count * settings.retry.retry_budget_fraction)`` — truncated,
    not rounded, so a run too small to earn even one retry slot (e.g. a
    single-task run at the default 0.25 fraction) gets none, rather than a
    fraction silently rounding up into a slot that was never earned.

    One instance is constructed per ``execute_plan`` call and shared BY
    REFERENCE across every concurrently-running task. ``try_consume`` is a
    plain synchronous check-then-decrement with no ``await`` inside it, so
    it is safe under asyncio's single-threaded cooperative scheduling
    without a lock: two tasks can only interleave at an ``await`` point,
    and there isn't one between the check and the decrement here.
    """

    def __init__(self, total: int) -> None:
        self._remaining = total

    @property
    def remaining(self) -> int:
        return self._remaining

    def try_consume(self) -> bool:
        """Consume one retry slot and return ``True``, or return ``False``
        (consuming nothing) if the budget is already spent."""
        if self._remaining <= 0:
            return False
        self._remaining -= 1
        return True


def _error_state_for(exc: ProviderError) -> TaskState:
    """Map a raised ``ProviderError`` to its ``TaskState`` (master plan S4's
    8-state set). ``ProviderRateLimitedError`` and ``ProviderTimeoutError``
    each get their own dedicated state; every other ``ProviderError``
    (including ``ProviderConfigError``/``ProviderNotConfigured``) is the
    generic ``PROVIDER_ERROR`` state.
    """
    if isinstance(exc, ProviderRateLimitedError):
        return TaskState.RATE_LIMITED
    if isinstance(exc, ProviderTimeoutError):
        return TaskState.TIMEOUT
    return TaskState.PROVIDER_ERROR


def _should_retry(
    exc: ProviderError,
    attempt: int,
    retry_settings: RetrySettings,
    retry_budget: RetryBudget,
) -> bool:
    """Whether the attempt that just raised ``exc`` (the ``attempt``'th
    attempt) should be followed by another one.

    Order matters: a ``PERMANENT`` error or an already-exhausted attempt
    count must short-circuit BEFORE ``retry_budget.try_consume()`` is ever
    called — retrying a ``ProviderConfigError`` cannot succeed, so it must
    never spend a shared budget slot that a genuinely transient failure
    elsewhere in the run could have used.
    """
    if exc.retryability is not Retryability.TRANSIENT:
        return False
    if attempt >= retry_settings.max_attempts:
        return False
    return retry_budget.try_consume()


def _compute_delay_seconds(
    exc: ProviderError, attempt: int, retry_settings: RetrySettings
) -> float:
    """Delay before the retry that follows the ``attempt``'th attempt.

    Master plan S5: honour ``Retry-After`` when the provider supplied one,
    overriding the computed backoff entirely — no jitter is applied to an
    explicit provider-supplied value, since jitter exists to desynchronize
    a GUESS, not to second-guess a value the provider itself chose.

    Otherwise: ``min(backoff_cap_seconds, backoff_base_seconds * 2**n)``
    with FULL jitter (``random.uniform(0, computed)``, not equal jitter —
    master plan S5 is explicit that many tasks failing simultaneously
    against a rate limit is the thundering-herd case full jitter exists
    for). ``n`` is the zero-indexed retry count: the first retry (after
    attempt 1 fails) uses ``2**0``, the second (after attempt 2) uses
    ``2**1``, and so on -- ``n = attempt - 1``.
    """
    if isinstance(exc, ProviderRateLimitedError) and exc.retry_after is not None:
        return exc.retry_after.total_seconds()
    computed = min(
        retry_settings.backoff_cap_seconds,
        retry_settings.backoff_base_seconds * (2 ** (attempt - 1)),
    )
    return random.uniform(0, computed)


async def _run_one_task(
    task: SearchTask,
    provider: FlightProvider,
    *,
    retry_settings: RetrySettings,
    retry_budget: RetryBudget,
    cache: CacheRepository | None = None,
    cache_settings: CacheSettings | None = None,
    cache_now: datetime | None = None,
) -> TaskExecutionResult:
    """Run one task's provider call, retrying per ``retry_settings`` and
    ``retry_budget``, and produce its ``TaskExecutionResult``. Binds
    ``task.task_id`` into the logging contextvar
    (``observability.context.task_context``) for the duration of the call
    (all attempts), so every event this function emits carries it
    automatically without threading it through ``log_event`` by hand.

    ``cache``/``cache_settings``/``cache_now`` are threaded through
    unchanged to ``_search_and_record`` -- see that function's docstring.
    All three default to ``None``: existing callers/tests that never pass
    them get exactly the pre-cache-wiring behaviour (``cache="bypass"``
    on every outcome), unchanged.
    """
    with task_context(task.task_id):
        return await _search_and_record(
            task,
            provider,
            retry_settings=retry_settings,
            retry_budget=retry_budget,
            cache=cache,
            cache_settings=cache_settings,
            cache_now=cache_now,
        )


async def _search_and_record(
    task: SearchTask,
    provider: FlightProvider,
    *,
    retry_settings: RetrySettings,
    retry_budget: RetryBudget,
    cache: CacheRepository | None = None,
    cache_settings: CacheSettings | None = None,
    cache_now: datetime | None = None,
) -> TaskExecutionResult:
    """Cache-aside over the RAW layer, then the existing retry loop on a
    miss (or when ``cache is None``, exactly the pre-wiring behaviour).

    ``cache_settings``/``cache_now`` are required together with ``cache``
    (asserted below) -- ``cache_now`` is deliberately a REAL wall-clock
    instant the CALLER supplies (never ``datetime.now()`` inside this
    module, matching this codebase's clock-injection convention), and
    deliberately NOT the same value as ``cli._deterministic_as_of``'s
    request-derived, fake-for-reproducibility instant: cache TTL is about
    genuine elapsed real time between two runs, and computing it from a
    deterministic stand-in would make every cache entry's expiry equally
    fake, defeating the one thing TTL exists to do.
    """
    request = task.request
    capabilities = provider.capabilities
    provider_name = capabilities.provider_name
    started_at = time.monotonic()

    raw_key: str | None = None
    if cache is not None:
        assert cache_settings is not None and cache_now is not None, (
            "cache requires cache_settings and cache_now to be supplied together"
        )
        raw_key = compute_raw_key(request, capabilities)
        cached_payload = await cache.get_raw(
            raw_key,
            now=cache_now,
            provider=provider_name,
            origin=request.origin,
            destination=request.destination,
        )
        if cached_payload is not None:
            return _record_cache_hit(task, provider_name, cached_payload, started_at)

    cache_state = "miss" if cache is not None else _BYPASS_CACHE_STATE

    attempt = 1
    while True:
        log_event(
            EventName.SEARCH_REQUESTED,
            provider=provider_name,
            origin=request.origin,
            destination=request.destination,
            max_stops=request.max_stops,
            attempt=attempt,
        )

        try:
            search_result = await provider.search(request, CallBudget())
        except ProviderError as exc:
            if _should_retry(exc, attempt, retry_settings, retry_budget):
                delay = _compute_delay_seconds(exc, attempt, retry_settings)
                log_event(
                    EventName.SEARCH_RETRY,
                    provider=provider_name,
                    origin=request.origin,
                    destination=request.destination,
                    attempt=attempt + 1,
                    reason=type(exc).__name__,
                )
                await asyncio.sleep(delay)
                attempt += 1
                continue

            duration_ms = _elapsed_ms(started_at)
            log_event(
                EventName.SEARCH_FAILED,
                provider=provider_name,
                origin=request.origin,
                destination=request.destination,
                attempts=attempt,
                error=str(exc),
            )
            outcome = TaskOutcome(
                task_id=task.task_id,
                state=_error_state_for(exc),
                attempts=attempt,
                duration_ms=duration_ms,
                offer_count=0,
                accepted_count=0,
                rejection_counts={},
                cache=cache_state,
                error_type=type(exc).__name__,
                error_detail=str(exc),
            )
            return TaskExecutionResult(outcome=outcome, offers=())

        duration_ms = _elapsed_ms(started_at)
        offer_count = len(search_result.offers)
        log_event(
            EventName.SEARCH_RESPONSE,
            provider=provider_name,
            origin=request.origin,
            destination=request.destination,
            max_stops=request.max_stops,
            attempt=attempt,
            http_status=_PLACEHOLDER_HTTP_STATUS,
            response_bytes=_PLACEHOLDER_RESPONSE_BYTES,
            duration_ms=duration_ms,
            offer_count=offer_count,
            cache=cache_state,
        )

        if cache is not None and raw_key is not None:
            assert cache_settings is not None and cache_now is not None
            ttl = resolve_ttl(
                departure_date=request.departure_date,
                as_of=cache_now.date(),
                settings=cache_settings,
            )
            await cache.put_raw(
                raw_key,
                search_result.model_dump_json(),
                provider=provider_name,
                api_version=capabilities.api_version,
                origin=request.origin,
                destination=request.destination,
                departure_date=request.departure_date,
                now=cache_now,
                ttl=ttl,
            )

        state = TaskState.OK if offer_count > 0 else TaskState.NO_OFFERS
        outcome = TaskOutcome(
            task_id=task.task_id,
            state=state,
            attempts=attempt,
            duration_ms=duration_ms,
            offer_count=offer_count,
            accepted_count=0,
            rejection_counts={},
            cache=cache_state,
        )
        return TaskExecutionResult(outcome=outcome, offers=search_result.offers)


def _record_cache_hit(
    task: SearchTask, provider_name: str, cached_payload: str, started_at: float
) -> TaskExecutionResult:
    """Build the ``TaskExecutionResult`` for a raw-layer cache hit --
    ``attempts=0`` is exactly correct, not a placeholder: zero provider
    attempts were made, the entire result came from
    ``providers.base.deserialize_cached_search_result``.
    """
    search_result: ProviderSearchResult = deserialize_cached_search_result(cached_payload)
    request = task.request
    duration_ms = _elapsed_ms(started_at)
    offer_count = len(search_result.offers)
    log_event(
        EventName.SEARCH_RESPONSE,
        provider=provider_name,
        origin=request.origin,
        destination=request.destination,
        max_stops=request.max_stops,
        attempt=0,
        http_status=_PLACEHOLDER_HTTP_STATUS,
        response_bytes=_PLACEHOLDER_RESPONSE_BYTES,
        duration_ms=duration_ms,
        offer_count=offer_count,
        cache="hit",
    )
    state = TaskState.OK if offer_count > 0 else TaskState.NO_OFFERS
    outcome = TaskOutcome(
        task_id=task.task_id,
        state=state,
        attempts=0,
        duration_ms=duration_ms,
        offer_count=offer_count,
        accepted_count=0,
        rejection_counts={},
        cache="hit",
    )
    return TaskExecutionResult(outcome=outcome, offers=search_result.offers)


def _elapsed_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)


async def execute_plan(
    tasks: tuple[SearchTask, ...],
    provider: FlightProvider,
    *,
    settings: FlightAgentSettings | None = None,
    cache: CacheRepository | None = None,
    cache_now: datetime | None = None,
) -> tuple[TaskExecutionResult, ...]:
    """Fan out ``tasks`` to ``provider.search`` concurrently, bounded by an
    ``asyncio.Semaphore`` sized from ``settings.concurrency.max_concurrent_searches``
    (``settings`` defaults to ``load_config()`` when omitted — never a
    hardcoded concurrency number).

    Uses ``asyncio.TaskGroup`` (see module docstring for why, not
    ``gather``). Returns exactly one ``TaskExecutionResult`` per input
    task, in the same order as ``tasks`` — ``asserts`` this before
    returning (master plan S5: "a missing outcome would otherwise silently
    shrink the search space").

    ``cache``/``cache_now`` default to ``None`` (no caching — every
    existing caller/test keeps its pre-cache-wiring behaviour unchanged).
    Passing ``cache`` requires ``cache_now`` too (a real caller supplies
    both together; see ``_search_and_record``'s docstring for why
    ``cache_now`` must be a genuine wall-clock instant). ``cache_settings``
    itself comes from ``resolved_settings.cache`` — never a second,
    independent parameter here — so a caller cannot pass a cache repo
    that is wired up against a different TTL policy than the rest of this
    run's config.
    """
    resolved_settings = settings if settings is not None else load_config()
    semaphore = asyncio.Semaphore(resolved_settings.concurrency.max_concurrent_searches)
    retry_budget = RetryBudget(
        int(len(tasks) * resolved_settings.retry.retry_budget_fraction)
    )

    results: list[TaskExecutionResult | None] = [None] * len(tasks)

    async def _bounded_run(index: int, task: SearchTask) -> None:
        async with semaphore:
            results[index] = await _run_one_task(
                task,
                provider,
                retry_settings=resolved_settings.retry,
                retry_budget=retry_budget,
                cache=cache,
                cache_settings=resolved_settings.cache if cache is not None else None,
                cache_now=cache_now,
            )

    async with asyncio.TaskGroup() as task_group:
        for index, task in enumerate(tasks):
            task_group.create_task(_bounded_run(index, task))

    outcomes = tuple(result for result in results if result is not None)
    assert len(outcomes) == len(tasks), (
        f"executor produced {len(outcomes)} result(s) for {len(tasks)} task(s) "
        f"-- a missing outcome would silently shrink the search space"
    )
    return outcomes


__all__ = ["RetryBudget", "TaskExecutionResult", "execute_plan"]
