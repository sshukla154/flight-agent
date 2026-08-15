"""D9 layover penalty band lookup — the one function this task exists for.

Master plan finding 0.8 / DECISIONS.md D9: penalty bands are LOWER-INCLUSIVE
HALF-OPEN — ``[180,240) -> 0``, ``[240,300) -> +10``, ``[300,360] -> +20`` —
except the top band, whose upper edge is closed at D8's ``[180, 360]``
validity ceiling. The spec's own worked example (a 4-hour = 240-minute
layover) lands exactly on the first boundary, which is exactly why this is
named as a risk: under this reading it scores +10, not 0.

The band table itself is never hardcoded here. ``config/defaults.toml``
already carries it as ``[[layover.penalty_bands]]`` (D9's own rationale:
"a threshold change is a config edit, never a code change") — this
function only implements the lookup rule over whatever band table it is
given, so a future band-table edit needs no change here.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from flightagent.config.models import PenaltyBand


def layover_penalty_for_minutes(elapsed_minutes: int, bands: Sequence[PenaltyBand]) -> Decimal:
    """Return the D9 penalty for a layover of ``elapsed_minutes``.

    Each band is treated as ``[min_minutes, max_minutes)`` — lower-inclusive,
    upper-exclusive — EXCEPT the band whose ``max_minutes`` equals the
    highest ``max_minutes`` across the whole table, which is closed at the
    top (D8's ``[180, 360]`` validity window has a closed upper end, so its
    penalty band must too, or exactly 360 would fall through every band).

    Raises ``ValueError`` if ``bands`` is empty or ``elapsed_minutes`` does
    not fall inside any configured band — this is a config/caller error
    (e.g. a layover that was never validated against D8's window), never a
    silent default to 0.
    """
    if not bands:
        raise ValueError("no penalty bands configured — cannot look up a layover penalty")

    top_max_minutes = max(band.max_minutes for band in bands)
    for band in bands:
        if band.min_minutes <= elapsed_minutes < band.max_minutes:
            return band.penalty_eur
        if band.max_minutes == top_max_minutes and elapsed_minutes == band.max_minutes:
            return band.penalty_eur

    raise ValueError(
        f"{elapsed_minutes} minutes does not fall within any configured penalty band "
        f"({[(b.min_minutes, b.max_minutes) for b in bands]!r})"
    )
