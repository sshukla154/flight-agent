"""Validation engine package (Phase 3 / T12 — v1 subset).

See ``DECISIONS.md`` D8/D11/D13 and master plan finding 0.4 for the exact
rules this package implements: stop count, the D8 layover window, origin
match, and the D11 origin-local departure date. Full hardening
(overnight/multi-day layovers, DST edge cases beyond what
``domain.segment`` already represents, self-transfer detection, the
ground-travel filter) is Phase 3 T18 and Phase 6 T38 — those tasks append
another callable to ``rules.RULES``; they do not need to touch
``engine.py``.
"""

from __future__ import annotations

from flightagent.validation.engine import validate
from flightagent.validation.rules import (
    RULES,
    Rule,
    check_departure_date,
    check_layover_window,
    check_origin_match,
    check_stop_count,
)

__all__ = [
    "RULES",
    "Rule",
    "check_departure_date",
    "check_layover_window",
    "check_origin_match",
    "check_stop_count",
    "validate",
]
