"""D12's EUR-threshold early-stop rule (Phase 6, T39, finding 0.7).

Mirrors ``policy/direct_vs_stop.py``'s shape: this module holds the actual
per-destination rule evaluation, given each origin's already-computed
cheapest valid fare to that destination in priority order; the caller
(``orchestration.waves``, T39) builds those per-origin fares from the full
160-task plan and the run's already-validated itineraries and calls
``evaluate_destination_early_stop`` once per destination.

Two things this module deliberately does NOT do:

- Build the per-(origin, destination) cheapest-fare lookup from raw tasks
  and outcomes. That is ``orchestration.waves``' job (T39) -- this module
  only ever sees the already-reduced ``OriginFare`` sequence.
- Build a "mode=enforced" record, or stop an in-flight fan-out. Master plan
  S5's Option A (true sequential-priority execution, `--search-mode=
  sequential-priority`) is explicitly out of scope for T39 -- see ``MODE``
  below. This module only ever produces post-hoc annotations.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Literal, NamedTuple

from flightagent.domain.airport import IataCode
from flightagent.domain.money import Money
from flightagent.domain.policy import EarlyStopEvaluation

_EUR = "EUR"

MODE: Literal["enforced", "advisory"] = "advisory"
"""T39 builds ONLY master plan Option B: full fan-out, post-hoc
deterministic replay, reported as a report annotation. True
sequential-priority EXECUTION (Option A -- actually stopping the fan-out
mid-flight, `--search-mode=sequential-priority`) is a different, later,
explicitly out-of-scope task; no code path in this codebase builds it yet.

Every ``EarlyStopEvaluation`` this module produces therefore carries
``mode="advisory"`` unconditionally. This is a property of WHICH CODE PATH
built the record (a post-hoc replay, always advisory), not a reflection of
``EarlyStopSettings.enabled`` -- that config flag toggles whether a FUTURE
sequential-priority executor would actually stop the fan-out; it has no
bearing on what this always-post-hoc module reports. A default run
(``enabled=false``) and a run with the flag flipped on both get exactly the
same ``mode="advisory"`` annotation from this module, because this module
never executes anything -- it only replays results that already exist.
"""

_MIN_PRIOR_ORIGINS = 2
"""D12's own rule 1: "the rule is evaluable only after >=2 origins have
been evaluated in priority order for a given destination." Below this
count there is nothing to compare against, so no comparison is even
attempted here -- finding 0.7's "vacuous truth on the first origin" fix.
Concretely: the 1st and 2nd origins (by priority) considered for a
destination can never trigger, regardless of price -- only the 3rd origin
onward has >=2 prior origins to compare against."""


class OriginFare(NamedTuple):
    """One origin's wave assignment and cheapest VALID fare to a single
    destination, already reduced by the caller (``orchestration.waves``)
    from that origin's raw task results.

    ``price_eur`` is ``None`` when this origin has no valid itinerary to
    the destination at all (every task errored, returned no offers, or
    every offer failed validation) -- finding 0.7's "the rule only compares
    valid fares": an absent price must never be treated as EUR 0, and must
    never itself count toward ``_MIN_PRIOR_ORIGINS`` (see
    ``evaluate_destination_early_stop``), since there is nothing there to
    compare against.
    """

    origin: IataCode
    wave: int
    price_eur: Decimal | None


def evaluate_destination_early_stop(
    destination: IataCode,
    origin_fares: Sequence[OriginFare],
    *,
    threshold_eur: Decimal,
) -> EarlyStopEvaluation:
    """Replay D12's EUR-threshold rule for ONE destination.

    ``origin_fares`` must already be in ascending priority order (T37's own
    origin-priority order -- this function does not re-sort). ``threshold_eur``
    comes from ``EarlyStopSettings.threshold_eur`` (``config/defaults.toml``'s
    ``[early_stop]`` table) -- never hardcoded here.

    Walks the origins in order, skipping any with ``price_eur is None``
    (they contribute neither a candidate price nor a comparison-basis
    entry). For every remaining origin once >=``_MIN_PRIOR_ORIGINS`` earlier
    (valid-fare) origins have already been seen, compares its price against
    the CHEAPEST of those prior origins' prices: a margin (prior cheapest
    minus this origin's price) ``>= threshold_eur`` triggers.

    Mirrors what a real sequential-priority executor would have done: the
    FIRST origin (in priority order) that would trigger stops the replay
    right there and is returned immediately -- later origins are never
    considered for this destination once one has triggered, exactly as a
    real executor would never have searched them. If no origin ever
    triggers, the LAST comparison actually performed (checking the final
    valid-fare origin against every valid-fare origin before it) is
    returned as the ``triggered=False`` record. If the rule never once
    reached its ``_MIN_PRIOR_ORIGINS`` threshold at all (fewer than
    ``_MIN_PRIOR_ORIGINS + 1`` origins ever had a valid fare to this
    destination, so no comparison ever ran), returns ``triggered=False``
    with every valid-fare origin seen as ``compared_against`` (0, 1, or 2
    of them), tagged at the last origin's wave.

    Never removes, skips, or reorders any task's own results -- this is a
    pure read over ``origin_fares``; the caller's already-executed data is
    untouched either way.
    """
    evaluated_origins: list[IataCode] = []
    evaluated_prices: dict[IataCode, Decimal] = {}
    last_check: EarlyStopEvaluation | None = None

    for origin_fare in origin_fares:
        if origin_fare.price_eur is None:
            continue

        if len(evaluated_origins) >= _MIN_PRIOR_ORIGINS:
            best_prior_price = min(evaluated_prices[origin] for origin in evaluated_origins)
            margin = best_prior_price - origin_fare.price_eur
            if margin >= threshold_eur:
                return EarlyStopEvaluation(
                    evaluated_at_wave=origin_fare.wave,
                    triggered=True,
                    triggering_origin=origin_fare.origin,
                    triggering_destination=destination,
                    margin=Money(amount=margin, currency=_EUR),
                    compared_against=tuple(evaluated_origins),
                    mode=MODE,
                )
            last_check = EarlyStopEvaluation(
                evaluated_at_wave=origin_fare.wave,
                triggered=False,
                compared_against=tuple(evaluated_origins),
                mode=MODE,
            )

        evaluated_origins.append(origin_fare.origin)
        evaluated_prices[origin_fare.origin] = origin_fare.price_eur

    if last_check is not None:
        return last_check

    final_wave = origin_fares[-1].wave if origin_fares else 0
    return EarlyStopEvaluation(
        evaluated_at_wave=final_wave,
        triggered=False,
        compared_against=tuple(evaluated_origins),
        mode=MODE,
    )


__all__ = ["MODE", "OriginFare", "evaluate_destination_early_stop"]
