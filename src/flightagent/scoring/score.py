"""Assembles ``domain.scoring.ScoreComponents`` from a ``NormalizedItinerary``
(T13, scorer v1).

Scope — v1 only (per this task's brief): ``direct_bonus`` is always
``Decimal("0")`` here. Addendum 1's -120 direct bonus is Phase 5 (T30), and
ground-travel score components (``ground_cost_component`` /
``ground_time_component``, D7) are Phase 6 (T38) — neither is touched by
this module.

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

_NO_DIRECT_BONUS_V1 = Decimal("0")
"""direct_bonus is always exactly this in Phase 2 — see module docstring
and the task brief's scope note. Named, not inlined, so a Phase 5 (T30)
change is a one-line diff away from an accidental silent drift."""


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
) -> ScoreComponents:
    """Populate a ``ScoreComponents`` for ``itinerary`` — scorer v1 (T13).

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
    - ``direct_bonus``: always ``Decimal("0")`` — out of scope for Phase 2,
      see module docstring.
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

    return ScoreComponents(
        fare_eur=itinerary.price_eur.amount,
        elapsed_time_component=elapsed_time_component,
        layover_penalty=layover_penalty_total,
        direct_bonus=_NO_DIRECT_BONUS_V1,
    )
