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

from collections.abc import Iterable

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


def summarize_validation_results(
    results: Iterable[ValidationResult],
) -> tuple[int, dict[str, int]]:
    """Aggregate a batch of ``ValidationResult``s into the two fields
    ``EventName.VALIDATE_COMPLETED`` requires (master plan S7;
    ``observability/events.py::ValidateCompletedFields``).

    Returns ``(accepted_count, rejection_counts)``:

    - ``accepted_count``: the number of itineraries in the batch whose
      ``is_valid`` is ``True`` — one per itinerary, never per rejection.
    - ``rejection_counts``: tallies every individual ``Rejection`` across
      every ``ValidationResult`` in the batch, keyed by
      ``RejectionCode.value``. This engine never short-circuits on the
      first failing rule (see ``validate`` above and
      ``test_validator.py::TestEngineAccumulatesAllRejections``), so one
      itinerary failing N rules at once contributes one increment to each
      of N different counters here — this counts *rejections*, not
      *rejected itineraries*. Two different itineraries that each fail
      the same rule once tally to 2 against that one code; one itinerary
      failing two different rules tallies 1 against each of the two.
    """
    accepted_count = 0
    rejection_counts: dict[str, int] = {}
    for result in results:
        if result.is_valid:
            accepted_count += 1
        for rejection in result.rejections:
            key = rejection.code.value
            rejection_counts[key] = rejection_counts.get(key, 0) + 1
    return accepted_count, rejection_counts
