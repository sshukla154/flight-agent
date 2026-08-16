"""D10's direct-vs-one-stop tier ladder (Phase 5, T31).

Builds one ``domain.policy.DestinationAnalysis`` per destination from two
already-D13-pool-separated inputs (``cli.py``'s ``_build_direct_vs_stop_pools``,
Phase 5 T29): the direct pool (validated ``max_stops=0`` itineraries) and the
one-stop pool (validated ``max_stops=1`` itineraries, already filtered to
``stop_count >= 1`` by the caller). Neither pool needs to arrive pre-sorted —
``_cheapest`` picks the minimum by ``(price_eur, itinerary_id)``, the same
deterministic tie-break finding 0.3 uses elsewhere, so caller ordering can
never leak into which itinerary is "cheapest".

Two things this module deliberately does NOT do:

- Re-derive the D10 threshold numbers. They come from ``DirectTierSettings``
  (``config/defaults.toml``'s ``[direct_tier]`` table) exclusively — D10's
  own mitigation is that a threshold retune is a YAML edit, never a code
  change here.
- Decide ranked-list ordering. ``adjusted_score`` (``flightagent.scoring``)
  governs that; this module's tier is a separate, per-destination narrative
  judgement that can legitimately disagree with it above roughly a EUR755
  one-stop fare (finding 0.1) — see ``_compute_divergence``.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from decimal import Decimal

from flightagent.config.models import DirectTierSettings, LayoverSettings, ScoringSettings
from flightagent.domain.airport import IataCode
from flightagent.domain.enums import DirectTier
from flightagent.domain.itinerary import NormalizedItinerary
from flightagent.domain.money import Money
from flightagent.domain.policy import DestinationAnalysis
from flightagent.observability.events import EventName
from flightagent.observability.logging import log_event
from flightagent.scoring.score import score_itinerary

_EUR = "EUR"

_NO_ONE_STOP_ALTERNATIVE_REASON_TEMPLATE = (
    "RECOMMENDED: no valid one-stop alternative exists for {destination} -- the cheapest "
    "direct itinerary (EUR{direct_price}) has nothing priced to compare against, so no D10 "
    "threshold applies. Defaulting to RECOMMENDED rather than penalizing a direct-only "
    "destination for lacking a one-stop option to be compared against."
)
"""The degenerate case (direct exists, no valid one-stop alternative at all).

Chosen deliberately as RECOMMENDED, not NOT_RECOMMENDED or NOT_AVAILABLE:
NOT_AVAILABLE is reserved for "no direct service" (the model's own validator
forbids it here, since ``cheapest_direct`` is present); NOT_RECOMMENDED would
imply a better alternative exists to prefer instead, which is false -- there
is nothing priced to prefer. RECOMMENDED reads as "nothing disqualifies this
direct itinerary", which is the only claim actually supported by the data.
"""

_NO_DIRECT_SERVICE_REASON_TEMPLATE = "no direct service found for {destination}"


def _cheapest(pool: Sequence[NormalizedItinerary]) -> NormalizedItinerary | None:
    """The minimum-price itinerary in ``pool``, or ``None`` if empty.

    Tie-break is ``itinerary_id`` (finding 0.3's deterministic-tiebreak
    convention) so a pool that happens to arrive in a different order never
    changes which itinerary is picked as "cheapest".
    """
    if not pool:
        return None
    return min(pool, key=lambda itinerary: (itinerary.price_eur.amount, itinerary.itinerary_id))


def evaluate_direct_tier(
    price_difference: Decimal,
    relative_difference: Decimal | None,
    thresholds: DirectTierSettings,
) -> tuple[DirectTier, str]:
    """Apply D10's config-driven band table to one (diff, rel) pair.

    Predicate per tier is OR, not AND — a tier fires if EITHER its absolute
    (EUR) arm or its relative (%) arm passes; ``relative_difference`` may be
    ``None`` (the zero-stop-price guard), in which case only the absolute
    arm is ever evaluated, never a crash.

    No ``abs()`` anywhere: ``price_difference`` is signed on purpose, so a
    direct itinerary that is actually CHEAPER than the one-stop (a negative
    ``price_difference``) trivially satisfies every "<=" arm and comes out
    RECOMMENDED, exactly as it should, with no special-casing needed.

    Returns ``(tier, tier_reason)`` where ``tier_reason`` names which
    threshold fired and with what numbers (D10's own requirement) — or, for
    NOT_RECOMMENDED, that neither arm of either band fired.
    """
    diff_recommended = price_difference <= thresholds.recommended_max_diff_eur
    rel_recommended = relative_difference is not None and (
        relative_difference <= thresholds.recommended_max_relative
    )
    if diff_recommended or rel_recommended:
        if diff_recommended and rel_recommended:
            reason = (
                f"RECOMMENDED: both rules fired -- price difference EUR{price_difference} <= "
                f"recommended_max_diff_eur EUR{thresholds.recommended_max_diff_eur} AND relative "
                f"difference {relative_difference} <= recommended_max_relative "
                f"{thresholds.recommended_max_relative}"
            )
        elif diff_recommended:
            reason = (
                f"RECOMMENDED: absolute-difference rule fired -- price difference "
                f"EUR{price_difference} <= recommended_max_diff_eur "
                f"EUR{thresholds.recommended_max_diff_eur}"
            )
        else:
            reason = (
                f"RECOMMENDED: relative-difference rule fired -- relative difference "
                f"{relative_difference} <= recommended_max_relative "
                f"{thresholds.recommended_max_relative}"
            )
        return DirectTier.RECOMMENDED, reason

    diff_good_value = price_difference <= thresholds.good_value_max_diff_eur
    rel_good_value = relative_difference is not None and (
        relative_difference <= thresholds.good_value_max_relative
    )
    if diff_good_value or rel_good_value:
        if diff_good_value and rel_good_value:
            reason = (
                f"GOOD_VALUE: both rules fired -- price difference EUR{price_difference} <= "
                f"good_value_max_diff_eur EUR{thresholds.good_value_max_diff_eur} AND relative "
                f"difference {relative_difference} <= good_value_max_relative "
                f"{thresholds.good_value_max_relative}"
            )
        elif diff_good_value:
            reason = (
                f"GOOD_VALUE: absolute-difference rule fired -- price difference "
                f"EUR{price_difference} <= good_value_max_diff_eur "
                f"EUR{thresholds.good_value_max_diff_eur}"
            )
        else:
            reason = (
                f"GOOD_VALUE: relative-difference rule fired -- relative difference "
                f"{relative_difference} <= good_value_max_relative "
                f"{thresholds.good_value_max_relative}"
            )
        return DirectTier.GOOD_VALUE, reason

    rel_text = (
        "undefined (cheapest_valid_stop price is zero)"
        if relative_difference is None
        else str(relative_difference)
    )
    reason = (
        f"NOT_RECOMMENDED: neither rule fired -- price difference EUR{price_difference} > "
        f"good_value_max_diff_eur EUR{thresholds.good_value_max_diff_eur}, and relative "
        f"difference {rel_text} is not <= good_value_max_relative "
        f"{thresholds.good_value_max_relative}"
    )
    return DirectTier.NOT_RECOMMENDED, reason


def _compute_divergence(
    *,
    tier: DirectTier,
    cheapest_direct: NormalizedItinerary,
    cheapest_valid_stop: NormalizedItinerary,
    price_difference: Decimal,
    scoring_settings: ScoringSettings,
    layover_settings: LayoverSettings,
) -> tuple[bool, str | None]:
    """Finding 0.1's ``score_policy_divergence``: does the tier's implied
    recommendation ("direct is a good choice" for RECOMMENDED/GOOD_VALUE)
    agree with what ``adjusted_score``-based ranking would actually show?

    Computed independently of the tier ladder above -- this asks a different
    question (score-based ranking) using the real scorer
    (``flightagent.scoring.score.score_itinerary``), not a re-derivation of
    the 100/150/0.10/0.20 numbers.
    """
    direct_components = score_itinerary(
        cheapest_direct,
        scoring_settings=scoring_settings,
        layover_settings=layover_settings,
        cheapest_valid_stop_price_eur=cheapest_valid_stop.price_eur.amount,
    )
    stop_components = score_itinerary(
        cheapest_valid_stop,
        scoring_settings=scoring_settings,
        layover_settings=layover_settings,
    )
    direct_adjusted = direct_components.adjusted_score
    stop_adjusted = stop_components.adjusted_score

    policy_favors_direct = tier in (DirectTier.RECOMMENDED, DirectTier.GOOD_VALUE)
    score_favors_direct = direct_adjusted < stop_adjusted

    if policy_favors_direct == score_favors_direct:
        return False, None

    if policy_favors_direct and not score_favors_direct:
        explanation = (
            f"Direct-tier policy recommends the direct itinerary ({tier.value}, price "
            f"difference EUR{price_difference}), but adjusted_score ranks the one-stop "
            f"itinerary ahead instead (one-stop adjusted_score {stop_adjusted} vs direct's "
            f"{direct_adjusted}) -- the fixed direct bonus and the direct-tier rule disagree "
            f"above roughly a EUR755 one-stop fare (finding 0.1)."
        )
    else:
        explanation = (
            f"Direct-tier policy does not recommend the direct itinerary ({tier.value}, price "
            f"difference EUR{price_difference}), but adjusted_score ranks the direct itinerary "
            f"ahead anyway (direct adjusted_score {direct_adjusted} vs one-stop's "
            f"{stop_adjusted}) -- the fixed direct bonus and the direct-tier rule disagree "
            f"(finding 0.1)."
        )
    return True, explanation


def analyze_destination(
    destination: IataCode,
    *,
    direct_pool: Sequence[NormalizedItinerary],
    one_stop_pool: Sequence[NormalizedItinerary],
    direct_tier_settings: DirectTierSettings,
    scoring_settings: ScoringSettings,
    layover_settings: LayoverSettings,
) -> DestinationAnalysis:
    """Build one destination's ``DestinationAnalysis`` (D10, finding 0.1).

    ``direct_pool`` / ``one_stop_pool`` are the D13 pool-separated per-
    destination pools ``cli.py``'s ``_build_direct_vs_stop_pools`` already
    builds (T29) -- ``one_stop_pool`` must already be filtered to
    ``stop_count >= 1`` by the caller; this function does not re-filter it.

    Four cases, in order:

    1. ``direct_pool`` empty -> ``NOT_AVAILABLE``, no price comparison.
    2. ``direct_pool`` non-empty, ``one_stop_pool`` empty -> the degenerate
       "direct-only destination" case, see
       ``_NO_ONE_STOP_ALTERNATIVE_REASON_TEMPLATE`` above for the reasoning.
    3. Both non-empty, cheapest one-stop price is exactly zero -> the
       relative-difference rule is skipped (division by zero is never
       attempted), tier decided by the absolute-difference rule alone, and a
       WARNING-level ``policy.direct_decision`` event is logged.
    4. Both non-empty, ordinary case -> full D10 ladder plus
       ``score_policy_divergence``.
    """
    cheapest_direct = _cheapest(direct_pool)
    cheapest_valid_stop = _cheapest(one_stop_pool)

    if cheapest_direct is None:
        tier = DirectTier.NOT_AVAILABLE
        tier_reason = _NO_DIRECT_SERVICE_REASON_TEMPLATE.format(destination=destination)
        log_event(
            EventName.POLICY_DIRECT_DECISION,
            destination=str(destination),
            tier=tier.value,
            tier_reason=tier_reason,
        )
        return DestinationAnalysis(
            destination=destination,
            cheapest_direct=None,
            cheapest_valid_stop=cheapest_valid_stop,
            price_difference=None,
            relative_difference=None,
            tier=tier,
            tier_reason=tier_reason,
        )

    if cheapest_valid_stop is None:
        tier = DirectTier.RECOMMENDED
        tier_reason = _NO_ONE_STOP_ALTERNATIVE_REASON_TEMPLATE.format(
            destination=destination, direct_price=cheapest_direct.price_eur.amount
        )
        log_event(
            EventName.POLICY_DIRECT_DECISION,
            destination=str(destination),
            tier=tier.value,
            tier_reason=tier_reason,
        )
        return DestinationAnalysis(
            destination=destination,
            cheapest_direct=cheapest_direct,
            cheapest_valid_stop=None,
            price_difference=None,
            relative_difference=None,
            tier=tier,
            tier_reason=tier_reason,
        )

    price_difference = cheapest_direct.price_eur.amount - cheapest_valid_stop.price_eur.amount
    stop_price = cheapest_valid_stop.price_eur.amount

    relative_difference: Decimal | None
    zero_stop_price = stop_price == 0
    if zero_stop_price:
        relative_difference = None
    else:
        relative_difference = price_difference / stop_price

    tier, tier_reason = evaluate_direct_tier(
        price_difference, relative_difference, direct_tier_settings
    )

    if zero_stop_price:
        log_event(
            EventName.POLICY_DIRECT_DECISION,
            level=logging.WARNING,
            destination=str(destination),
            tier=tier.value,
            tier_reason=tier_reason,
            msg=(
                f"cheapest_valid_stop price for {destination} is exactly zero EUR -- "
                "relative-difference rule skipped, tier decided by the absolute-difference "
                "rule alone"
            ),
        )
    else:
        log_event(
            EventName.POLICY_DIRECT_DECISION,
            destination=str(destination),
            tier=tier.value,
            tier_reason=tier_reason,
        )

    divergence, divergence_explanation = _compute_divergence(
        tier=tier,
        cheapest_direct=cheapest_direct,
        cheapest_valid_stop=cheapest_valid_stop,
        price_difference=price_difference,
        scoring_settings=scoring_settings,
        layover_settings=layover_settings,
    )

    return DestinationAnalysis(
        destination=destination,
        cheapest_direct=cheapest_direct,
        cheapest_valid_stop=cheapest_valid_stop,
        price_difference=Money(amount=price_difference, currency=_EUR),
        relative_difference=relative_difference,
        tier=tier,
        tier_reason=tier_reason,
        score_policy_divergence=divergence,
        divergence_explanation=divergence_explanation,
    )
