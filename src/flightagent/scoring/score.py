"""Assembles ``domain.scoring.ScoreComponents`` from a ``NormalizedItinerary``
(T13, scorer v1; Phase 5 T30 adds the real ``direct_bonus``).

``direct_bonus`` is always ``Decimal("0")`` for any itinerary with
``stop_count >= 1`` — the bonus only ever applies to a direct
(``stop_count == 0``) itinerary. For a direct itinerary,
``scoring_settings.direct_bonus_mode`` picks the formula (finding 0.1):

- ``"fixed"``: ``scoring_settings.direct_bonus_eur`` exactly, unchanged
  regardless of fare (the spec-compliant default, a flat -120.0).
- ``"proportional"``: ``-0.20 * cheapest_valid_stop_price_eur`` — finding
  0.1's own proposed fix for ``score_policy_divergence``, scaling the bonus
  to the destination's cheapest valid one-stop fare instead of a flat EUR
  figure. This is the caller's responsibility to supply (the per-destination
  policy comparison, Phase 5 T31, is what actually has that figure); this
  module raises rather than silently falling back to a fixed amount that was
  not asked for.

Ground-travel score components (``ground_cost_component`` /
``ground_time_component``, D7) are Phase 6 (T38) — not touched by this
module.

ALL ARITHMETIC IN DECIMAL (finding 0.3) — never ``float``. The two
``timedelta`` -> ``Decimal`` conversions below deliberately avoid
``timedelta.total_seconds()``, which always returns a ``float``, and instead
build the ``Decimal`` from ``timedelta``'s own integer ``.days``/``.seconds``/
``.microseconds`` fields (hours), or use exact ``timedelta // timedelta``
integer floor-division (minutes) — both are exact, reproducible integer/
Decimal arithmetic with no float ever in the path.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from flightagent.config.models import LayoverSettings, ScoringSettings
from flightagent.domain.itinerary import NormalizedItinerary
from flightagent.domain.scoring import ScoreComponents
from flightagent.scoring.components import layover_penalty_for_minutes

_SECONDS_PER_HOUR = Decimal(3600)
_ONE_MINUTE = timedelta(minutes=1)

_NO_DIRECT_BONUS_MULTI_STOP = Decimal("0")
"""direct_bonus for any itinerary with stop_count >= 1 — always exactly
this, regardless of direct_bonus_mode. Named, not inlined, so it reads as
a deliberate constant rather than a literal that might drift."""

_PROPORTIONAL_BONUS_RATIO = Decimal("0.20")
"""Finding 0.1's own proposed resolution for score_policy_divergence:
direct_bonus_mode="proportional" scales the bonus to
``-0.20 * cheapest_valid_stop_price_eur`` instead of a flat EUR figure,
eliminating the divergence between the -120 fixed bonus and the 150/20%
direct-tier policy ladder above roughly a EUR755 one-stop fare. A literal
here, not read from the ``[direct_tier]`` config table (Phase 5 T31, a
separate parallel task) — that table does not exist in this module's scope,
and this ratio is finding 0.1's own proposed constant, not a re-use of the
policy ladder's threshold."""


def _direct_bonus(
    itinerary: NormalizedItinerary,
    *,
    scoring_settings: ScoringSettings,
    cheapest_valid_stop_price_eur: Decimal | None,
) -> Decimal:
    """T30 (Phase 5): the direct-flight score bonus — see module docstring
    for the two ``direct_bonus_mode`` formulas."""
    if itinerary.stop_count >= 1:
        return _NO_DIRECT_BONUS_MULTI_STOP

    if scoring_settings.direct_bonus_mode == "fixed":
        return scoring_settings.direct_bonus_eur

    # "proportional" — the only other Literal value of direct_bonus_mode.
    if cheapest_valid_stop_price_eur is None:
        raise ValueError(
            "direct_bonus_mode is 'proportional' but no cheapest_valid_stop_price_eur "
            "was supplied for a direct (stop_count == 0) itinerary — proportional mode "
            "cannot compute a bonus without the destination's cheapest valid one-stop fare"
        )
    return -(_PROPORTIONAL_BONUS_RATIO * cheapest_valid_stop_price_eur)


def _duration_to_decimal_hours(duration: timedelta) -> Decimal:
    """Exact ``Decimal`` hours from a ``timedelta``, never via
    ``total_seconds()`` (which returns ``float`` — finding 0.3)."""
    total_seconds = (
        Decimal(duration.days) * 86400
        + Decimal(duration.seconds)
        + Decimal(duration.microseconds) / Decimal(1_000_000)
    )
    return total_seconds / _SECONDS_PER_HOUR


def _duration_to_whole_minutes(duration: timedelta) -> int:
    """Exact integer minutes from a ``timedelta``. ``timedelta // timedelta``
    is integer floor-division over the two values' internal microsecond
    counts — exact, and never routes through ``float``."""
    return duration // _ONE_MINUTE


def score_itinerary(
    itinerary: NormalizedItinerary,
    *,
    scoring_settings: ScoringSettings,
    layover_settings: LayoverSettings,
    cheapest_valid_stop_price_eur: Decimal | None = None,
) -> ScoreComponents:
    """Populate a ``ScoreComponents`` for ``itinerary`` — scorer v1 (T13),
    ``direct_bonus`` per Phase 5 (T30).

    - ``fare_eur``: the itinerary's already-converted EUR price (D14 — never
      re-derived or re-converted here).
    - ``elapsed_time_component``: ``total_duration`` in hours times
      ``scoring_settings.time_value_eur_per_hour`` (finding 0.8 — the weight
      is config, loaded here, never a hardcoded ``3.0``).
    - ``layover_penalty``: the D9 band penalty summed across every layover
      in the itinerary — zero layovers (a direct itinerary) sums to
      ``Decimal("0")`` with no special-casing needed. Does NOT assume
      exactly one layover; a future multi-leg itinerary with several
      layovers sums all of them.
    - ``direct_bonus``: ``Decimal("0")`` for ``stop_count >= 1``; for a
      direct itinerary, ``scoring_settings.direct_bonus_mode`` picks
      "fixed" (``direct_bonus_eur`` flat) or "proportional" (scaled off
      ``cheapest_valid_stop_price_eur``, which the caller must then supply)
      — see module docstring for the exact formulas.
    """
    layover_penalty_total = sum(
        (
            layover_penalty_for_minutes(
                _duration_to_whole_minutes(layover.duration),
                layover_settings.penalty_bands,
            )
            for leg in itinerary.legs
            for layover in leg.layovers
        ),
        Decimal("0"),
    )

    elapsed_hours = _duration_to_decimal_hours(itinerary.total_duration)
    elapsed_time_component = elapsed_hours * scoring_settings.time_value_eur_per_hour

    direct_bonus = _direct_bonus(
        itinerary,
        scoring_settings=scoring_settings,
        cheapest_valid_stop_price_eur=cheapest_valid_stop_price_eur,
    )

    return ScoreComponents(
        fare_eur=itinerary.price_eur.amount,
        elapsed_time_component=elapsed_time_component,
        layover_penalty=layover_penalty_total,
        direct_bonus=direct_bonus,
    )
