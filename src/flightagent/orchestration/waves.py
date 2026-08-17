"""T39: post-hoc, deterministic replay of D12's EUR-threshold early-stop
rule over a COMPLETE task/result set (master plan S5's Option B -- see
``policy.early_stop.MODE`` for why every evaluation this module produces
is ``mode="advisory"``, never ``"enforced"``).

Takes T37's already-wave-assigned ``SearchTask``s (``orchestration.plan.
build_multi_origin_plan``) plus the already-validated itineraries the CLI's
own pipeline produced per task (``valid_by_task_id``, built in cli.py's
``_run_all_destinations``) and reduces them to ONE ``EarlyStopEvaluation``
PER DESTINATION -- never per origin, never per wave -- by walking each
destination's origins in priority order and delegating the actual
EUR-threshold comparison to ``policy.early_stop.evaluate_destination_early_stop``.

This module NEVER cancels, skips, filters, or reorders any already-executed
task or its results -- see cli.py's own ``_run_all_destinations`` docstring
for the full pipeline this augments. Calling ``replay_early_stop`` is purely
additive: it reads ``tasks``/``valid_by_task_id`` and returns new data; it
never mutates either input, and the report's own ranked/full result set is
completely unaffected by what it returns. Building true sequential-priority
EXECUTION (stopping the fan-out mid-flight, master plan S5's Option A) is
explicitly out of scope here -- see ``policy.early_stop`` module docstring.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal

from flightagent.domain.airport import IataCode
from flightagent.domain.itinerary import NormalizedItinerary
from flightagent.domain.policy import EarlyStopEvaluation
from flightagent.domain.run import SearchTask
from flightagent.policy.early_stop import OriginFare, evaluate_destination_early_stop


def _cheapest_price_by_origin_destination(
    tasks: Sequence[SearchTask],
    valid_by_task_id: Mapping[str, Sequence[NormalizedItinerary]],
) -> dict[tuple[IataCode, IataCode], Decimal]:
    """The cheapest VALID ``price_eur`` for each ``(origin, destination)``
    pair, across BOTH stop modes.

    Not scoped to one search mode: D13 means a traveller from a given
    origin can take either that destination's direct or one-stop search
    result, so the early-stop comparison basis is "the cheapest valid fare
    this origin found to this destination", full stop -- not "...in the
    direct search" or "...in the one-stop search" specifically. An
    ``(origin, destination)`` pair with zero valid itineraries in EITHER
    mode is simply absent from the returned mapping, never defaulted to a
    sentinel price (``policy.early_stop.OriginFare.price_eur`` is exactly
    this absence, as ``None``).
    """
    prices: dict[tuple[IataCode, IataCode], Decimal] = {}
    for task in tasks:
        key = (task.request.origin, task.request.destination)
        for itinerary in valid_by_task_id.get(task.task_id, ()):
            price = itinerary.price_eur.amount
            current = prices.get(key)
            if current is None or price < current:
                prices[key] = price
    return prices


def _origins_in_priority_order(tasks: Sequence[SearchTask]) -> list[tuple[IataCode, int]]:
    """Every distinct origin appearing in ``tasks``, as ``(origin, wave)``
    pairs, deduplicated and sorted by ``SearchTask.origin_priority``
    ascending -- T37's own priority order, read directly off the tasks
    rather than re-derived from the airport registry a second way.

    ``wave`` is taken from whichever task happens to carry it first for a
    given origin: every task sharing an origin carries the identical
    ``wave`` (``orchestration.plan.build_multi_origin_plan`` assigns it
    once per origin, to every one of that origin's 16 tasks), so
    first-seen is safe and never ambiguous.
    """
    priority_and_wave_by_origin: dict[IataCode, tuple[int, int]] = {}
    for task in tasks:
        priority_and_wave_by_origin.setdefault(
            task.request.origin, (task.origin_priority, task.wave)
        )
    return [
        (origin, wave)
        for origin, (_priority, wave) in sorted(
            priority_and_wave_by_origin.items(), key=lambda item: item[1][0]
        )
    ]


def replay_early_stop(
    tasks: Sequence[SearchTask],
    valid_by_task_id: Mapping[str, Sequence[NormalizedItinerary]],
    *,
    destinations: Sequence[IataCode],
    threshold_eur: Decimal,
) -> dict[IataCode, EarlyStopEvaluation]:
    """The full T39 replay: one ``EarlyStopEvaluation`` per entry in
    ``destinations``, keyed by destination -- a ``dict`` (not a plain
    sequence) because ``EarlyStopEvaluation`` itself carries no "which
    destination is this" field in the non-triggered case (only
    ``triggering_destination``, which the model's own validator forbids
    from being set when ``triggered`` is ``False``); keying by destination
    here is the one place that association is recorded, rather than
    relying on callers keeping a second sequence in positional sync.
    Iteration order matches ``destinations`` (callers pass registry
    destination order -- see ``cli.py`` -- so this never reorders the
    report's own destination listing; Python dicts preserve insertion
    order).

    Deterministic over the SAME ``(tasks, valid_by_task_id)`` regardless of
    the execution order/concurrency level the original fan-out actually ran
    under -- the replay is entirely a function of ``SearchTask.origin_priority``
    (never completion order, per D12's own rationale for why the naive rule
    is unimplementable) plus each task's already-validated itineraries.

    ``threshold_eur`` is read by the caller from ``FlightAgentSettings.early_stop.
    threshold_eur`` (``config/defaults.toml``'s ``[early_stop]`` table) and
    threaded straight through to ``policy.early_stop.evaluate_destination_early_stop``
    -- never hardcoded at any layer.

    Never filters or truncates ``tasks``/``valid_by_task_id`` -- every task's
    results remain exactly as the caller already computed them; this
    function only reads them to build the annotation it returns.
    """
    price_by_origin_destination = _cheapest_price_by_origin_destination(tasks, valid_by_task_id)
    origins_ordered = _origins_in_priority_order(tasks)

    evaluations: dict[IataCode, EarlyStopEvaluation] = {}
    for destination in destinations:
        origin_fares = tuple(
            OriginFare(
                origin=origin,
                wave=wave,
                price_eur=price_by_origin_destination.get((origin, destination)),
            )
            for origin, wave in origins_ordered
        )
        evaluations[destination] = evaluate_destination_early_stop(
            destination, origin_fares, threshold_eur=threshold_eur
        )
    return evaluations


__all__ = ["replay_early_stop"]
