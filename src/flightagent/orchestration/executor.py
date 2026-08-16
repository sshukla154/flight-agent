"""Bounded-concurrency task execution (T24).

Master plan S5's layered per-task controls, outermost in: semaphore ->
``asyncio.timeout`` -> circuit breaker -> retry loop -> token bucket -> HTTP.
This module builds only the outermost layer — the semaphore-bounded fan-out
— plus the one-attempt-only, no-retry provider call each task makes today.
The retry loop (with its own ``SEARCH_RETRY`` events and backoff schedule)
is T25, in the next wave; everything here calls the provider exactly once
per task and reports whatever happened.

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
import time

from pydantic import BaseModel, ConfigDict

from flightagent.config.loader import load_config
from flightagent.config.models import FlightAgentSettings
from flightagent.domain.enums import TaskState
from flightagent.domain.itinerary import RawOffer
from flightagent.domain.run import SearchTask, TaskOutcome
from flightagent.observability.context import task_context
from flightagent.observability.events import EventName
from flightagent.observability.logging import log_event
from flightagent.providers.base import CallBudget, FlightProvider
from flightagent.providers.errors import (
    ProviderError,
    ProviderRateLimitedError,
    ProviderTimeoutError,
)

_PLACEHOLDER_CACHE_STATE = "bypass"
"""Every call this module makes is a live provider call — no cache layer
exists yet (that is Phase 7, ``persistence``/``cache`` in the module tree).
``"bypass"`` is the honest value for "no cache was consulted", as opposed
to ``"miss"`` (a cache WAS consulted and found nothing) which would
overstate what this module actually does."""

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


async def _run_one_task(task: SearchTask, provider: FlightProvider) -> TaskExecutionResult:
    """Run one task's single (no-retry) provider call and produce its
    ``TaskExecutionResult``. Binds ``task.task_id`` into the logging
    contextvar (``observability.context.task_context``) for the duration
    of the call, so every event this function emits carries it
    automatically without threading it through ``log_event`` by hand.
    """
    with task_context(task.task_id):
        return await _search_and_record(task, provider)


async def _search_and_record(task: SearchTask, provider: FlightProvider) -> TaskExecutionResult:
    request = task.request
    provider_name = provider.capabilities.provider_name

    log_event(
        EventName.SEARCH_REQUESTED,
        provider=provider_name,
        origin=request.origin,
        destination=request.destination,
        max_stops=request.max_stops,
        attempt=1,
    )

    started_at = time.monotonic()
    try:
        search_result = await provider.search(request, CallBudget())
    except ProviderError as exc:
        duration_ms = _elapsed_ms(started_at)
        log_event(
            EventName.SEARCH_FAILED,
            provider=provider_name,
            origin=request.origin,
            destination=request.destination,
            attempts=1,
            error=str(exc),
        )
        outcome = TaskOutcome(
            task_id=task.task_id,
            state=_error_state_for(exc),
            attempts=1,
            duration_ms=duration_ms,
            offer_count=0,
            accepted_count=0,
            rejection_counts={},
            cache=_PLACEHOLDER_CACHE_STATE,
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
        attempt=1,
        http_status=_PLACEHOLDER_HTTP_STATUS,
        response_bytes=_PLACEHOLDER_RESPONSE_BYTES,
        duration_ms=duration_ms,
        offer_count=offer_count,
        cache=_PLACEHOLDER_CACHE_STATE,
    )
    state = TaskState.OK if offer_count > 0 else TaskState.NO_OFFERS
    outcome = TaskOutcome(
        task_id=task.task_id,
        state=state,
        attempts=1,
        duration_ms=duration_ms,
        offer_count=offer_count,
        accepted_count=0,
        rejection_counts={},
        cache=_PLACEHOLDER_CACHE_STATE,
    )
    return TaskExecutionResult(outcome=outcome, offers=search_result.offers)


def _elapsed_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)


async def execute_plan(
    tasks: tuple[SearchTask, ...],
    provider: FlightProvider,
    *,
    settings: FlightAgentSettings | None = None,
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
    """
    resolved_settings = settings if settings is not None else load_config()
    semaphore = asyncio.Semaphore(resolved_settings.concurrency.max_concurrent_searches)

    results: list[TaskExecutionResult | None] = [None] * len(tasks)

    async def _bounded_run(index: int, task: SearchTask) -> None:
        async with semaphore:
            results[index] = await _run_one_task(task, provider)

    async with asyncio.TaskGroup() as task_group:
        for index, task in enumerate(tasks):
            task_group.create_task(_bounded_run(index, task))

    outcomes = tuple(result for result in results if result is not None)
    assert len(outcomes) == len(tasks), (
        f"executor produced {len(outcomes)} result(s) for {len(tasks)} task(s) "
        f"-- a missing outcome would silently shrink the search space"
    )
    return outcomes


__all__ = ["TaskExecutionResult", "execute_plan"]
