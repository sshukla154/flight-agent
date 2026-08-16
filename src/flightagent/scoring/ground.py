"""Ground-travel score overlay — D7 / master plan S6's PARALLEL
``total_journey_score`` metric, never the spec's own ``score``/
``adjusted_score`` formula (see ``domain.ground.GroundLeg``'s own module
docstring and ``domain.scoring.ScoreComponents``'s docstring for why the
two must stay separate).

``domain.scoring.ScoredItinerary`` already carries ``ground_cost_component``,
``ground_time_component``, and the computed ``total_journey_score`` property
(Phase 1 built these as placeholders for exactly this task) — this module
supplies the missing INPUTS:

- ``ground_cost_component`` / ``ground_time_component``: the two weighted
  Decimal figures, computed from one origin's ``GroundLeg`` plus the
  ``[ground_travel]`` config weights (``config/defaults.toml``:
  ``ground_cost_weight=1.0``, ``ground_time_weight=8.0``).
- ``apply_ground_overlay``: populates a ``ScoredItinerary`` with those two
  components (plus the ``ground`` leg itself). ``total_journey_score``
  itself needs no separate call — it is a ``computed_field`` on
  ``ScoredItinerary`` that reads exactly the two fields this function
  sets, so setting them IS setting it.
- ``door_to_door_hours``: a display/report-facing derived value (T41,
  later task) — flight ``total_duration`` plus ``GroundLeg.duration``, in
  hours. Entirely separate from the two weighted score components above
  and NOT itself an input to ``total_journey_score``.

ALL ARITHMETIC IN DECIMAL (finding 0.3), never ``float`` — this is not new
guidance, every prior phase's scorer work (``scoring.score``) already
follows it, and this module follows the identical pattern: durations are
converted to exact Decimal hours via ``timedelta``'s own integer
``.days``/``.seconds``/``.microseconds`` fields, never via
``timedelta.total_seconds()`` (which always returns ``float``).
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from flightagent.config.models import GroundTravelSettings
from flightagent.domain.ground import GroundLeg
from flightagent.domain.scoring import ScoredItinerary

_SECONDS_PER_HOUR = Decimal(3600)


def _duration_to_decimal_hours(duration: timedelta) -> Decimal:
    """Exact ``Decimal`` hours from a ``timedelta`` — the identical
    technique ``scoring.score`` already uses under the same name
    (duplicated here, not imported: that helper is module-private, and
    every scoring submodule in this codebase owns its own copy of this
    small exact-arithmetic conversion rather than sharing one across
    ``scoring.score``, ``scoring.components``, and this module).
    """
    total_seconds = (
        Decimal(duration.days) * 86400
        + Decimal(duration.seconds)
        + Decimal(duration.microseconds) / Decimal(1_000_000)
    )
    return total_seconds / _SECONDS_PER_HOUR


def ground_cost_component(ground_leg: GroundLeg, *, ground_cost_weight: Decimal) -> Decimal:
    """``ground_cost_weight * ground_leg.cost.amount`` (D7, master plan S6).

    ``ground_leg.cost`` is already a quantized ``Money`` (cent precision,
    ROUND_HALF_UP) — this multiplies its ``.amount`` by the configured
    weight and does not re-quantize the product. ``total_journey_score`` is
    a comparison metric, not a currency amount presented to a user, so
    carrying its full Decimal precision through is correct, not sloppy.
    """
    return ground_cost_weight * ground_leg.cost.amount


def ground_time_component(ground_leg: GroundLeg, *, ground_time_weight: Decimal) -> Decimal:
    """``ground_time_weight * ground_leg.duration`` (converted to Decimal
    hours) — D7, master plan S6."""
    return ground_time_weight * _duration_to_decimal_hours(ground_leg.duration)


def apply_ground_overlay(
    scored: ScoredItinerary,
    *,
    ground_leg: GroundLeg,
    settings: GroundTravelSettings,
) -> ScoredItinerary:
    """Return a NEW ``ScoredItinerary`` (frozen model) with ``ground``,
    ``ground_cost_component`` and ``ground_time_component`` populated from
    ``ground_leg`` and ``settings``'s weights.

    ``total_journey_score`` needs no separate call here — it is a
    ``computed_field`` on ``ScoredItinerary`` (domain/scoring.py) that
    reads exactly these two fields, so setting them IS setting it.

    Uses ``model_copy(update=...)`` — the same idiom
    ``scoring.ranking.rank_itineraries`` already uses to attach computed
    rank positions to a frozen ``ScoredItinerary`` without mutating the
    input — this never mutates ``scored`` in place. As with that existing
    use, ``model_copy(update=...)`` does not re-run
    ``ScoredItinerary``'s own validators; the three fields set here are
    always mutually consistent (a real ``ground_leg`` plus its own
    components), so ``_validate_ground_consistency`` would not have
    rejected the result anyway.
    """
    return scored.model_copy(
        update={
            "ground": ground_leg,
            "ground_cost_component": ground_cost_component(
                ground_leg, ground_cost_weight=settings.ground_cost_weight
            ),
            "ground_time_component": ground_time_component(
                ground_leg, ground_time_weight=settings.ground_time_weight
            ),
        }
    )


def door_to_door_hours(flight_total_duration: timedelta, ground_leg: GroundLeg) -> Decimal:
    """Flight ``total_duration`` plus ``ground_leg.duration``, in Decimal
    hours — the door-to-door travel time T41's origin comparison table
    surfaces per origin. Entirely separate from ``ground_cost_component``/
    ``ground_time_component`` above: this is a display value, never an
    input to ``total_journey_score``.
    """
    return _duration_to_decimal_hours(flight_total_duration + ground_leg.duration)
