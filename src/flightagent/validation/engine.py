"""The validation engine — runs every rule, collects every rejection.

Master plan S1/S5: "the validation engine re-checks stop count, layover,
origin, and date on every offer regardless" — providers do return offers
violating their own filters, and a client-side re-check costs nothing.
This module is the one place that owns "run all rules, never
short-circuit on the first failure"; ``rules.py``'s callables are plain
independent predicates that know nothing about each other or about this
loop.
"""

from __future__ import annotations

from flightagent.domain.itinerary import NormalizedItinerary
from flightagent.domain.run import SearchRequest
from flightagent.domain.validation import ValidationResult
from flightagent.validation.rules import RULES


def validate(itinerary: NormalizedItinerary, request: SearchRequest) -> ValidationResult:
    """Run every rule in ``RULES`` against ``itinerary``, accumulating
    every returned ``Rejection`` into one ``ValidationResult``.

    Never returns after the first hit — every rule in ``RULES`` runs
    unconditionally, regardless of whether an earlier rule already
    produced a rejection. ``ValidationResult.is_valid`` is a
    ``computed_field`` derived from ``rejections`` (domain/validation.py),
    so there is no separate flag here that could drift out of sync with
    the rejection list.
    """
    rejections = tuple(
        rejection for rule in RULES if (rejection := rule(itinerary, request)) is not None
    )
    return ValidationResult(itinerary_id=itinerary.itinerary_id, rejections=rejections)
