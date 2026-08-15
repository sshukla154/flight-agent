"""flightagent.scoring — scorer v1 (T13).

Populates ``domain.scoring.ScoreComponents`` from a ``NormalizedItinerary``.
See ``score.py`` for the scope note (direct_bonus and ground components are
out of scope for Phase 2) and ``components.py`` for the D9 penalty-band
lookup.
"""

from __future__ import annotations

from flightagent.scoring.components import layover_penalty_for_minutes
from flightagent.scoring.score import score_itinerary

__all__ = [
    "layover_penalty_for_minutes",
    "score_itinerary",
]
