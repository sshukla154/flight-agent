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


class Rejection(BaseModel):
    """One failed validation rule, with enough context to audit it without
    re-running the rule."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: RejectionCode
    message: str
    observed: str
    expected: str
    rule_id: str


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
