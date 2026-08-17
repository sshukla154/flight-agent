"""Validation outcome records.

Phase 3 (out of scope here) builds the rule engine that PRODUCES these;
this module only defines the shape it must never short-circuit into.
Master plan S5: "the validation engine re-checks stop count, layover,
origin, and date on every offer regardless, because providers do return
offers violating their own filters and a client-side re-check costs
nothing" — which is exactly why ``ValidationResult.rejections`` is a
collection of every rule that failed, not a single optional ``Rejection``
for whichever one failed first.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, computed_field

from flightagent.domain.enums import RejectionCode
from flightagent.domain.itinerary import NormalizedItinerary


class Rejection(BaseModel):
    """One failed validation rule, with enough context to audit it without
    re-running the rule."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: RejectionCode
    message: str
    observed: str
    expected: str
    rule_id: str


class RejectedItinerary(BaseModel):
    """One itinerary paired with the ``Rejection`` that excluded it (T41).

    ``ValidationResult`` alone cannot serve a report appendix: it carries
    only ``itinerary_id`` and ``rejections``, never the ``NormalizedItinerary``
    object itself (see that class's own docstring), so there is nothing to
    read a route/price/airline off of. This model exists specifically for
    call sites (``cli.py``) that still hold the itinerary in scope at the
    moment ``validate()`` returns a rejection and want to preserve it for
    later, itinerary-level reporting -- D5's "Self-transfer, not protected"
    appendix (``reporting.markdown``/``reporting.json_report``, T41) is the
    first consumer, and is not required to be the only one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    itinerary: NormalizedItinerary
    rejection: Rejection


class ValidationResult(BaseModel):
    """The full outcome of validating one itinerary against every rule."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    itinerary_id: str
    rejections: tuple[Rejection, ...] = ()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_valid(self) -> bool:
        """Derived from ``rejections``, never independently settable — an
        itinerary with any recorded rejection cannot also claim to be
        valid, by construction rather than by convention."""
        return len(self.rejections) == 0
