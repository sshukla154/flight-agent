"""Money type.

Master plan S4: "Money is Decimal, quantized 0.01, ROUND_HALF_UP." Finding
0.3 explains why: float addition is not associative, so different
accumulation orders under concurrent 160-way fan-out would produce
different last bits and reorder ties — a reproducibility failure that is
invisible until two "identical" runs disagree.

D14: currency is carried explicitly and is deliberately NOT restricted to
EUR. A ``NormalizedItinerary`` must hold ``price_original`` (whatever
currency the provider actually quoted) alongside ``price_eur`` — D14
forbids ever converting one into the other silently in place, so the type
itself must be able to represent a non-EUR amount.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator

CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
"""3 uppercase letters, ISO-4217-shaped ("EUR", "USD", "INR", ...). Not
restricted to EUR — see D14 above."""

_CENT = Decimal("0.01")


class Money(BaseModel):
    """An amount with its currency. Frozen/immutable — a Money value
    represents a fact about a quote at a point in time, not something
    later code should mutate in place.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    amount: Decimal
    currency: CurrencyCode

    @field_validator("amount", mode="before")
    @classmethod
    def _reject_float(cls, value: object) -> object:
        """Float is rejected outright, before pydantic's own Decimal
        coercion ever runs. A float literal like ``10.005`` is not exactly
        representable in binary, so quantizing it is not reproducible
        across machines/Python builds — exactly the class of
        nondeterminism finding 0.3 exists to eliminate from money and
        score arithmetic. Decimal, int, and numeric strings all construct
        an exact ``Decimal`` and are accepted.
        """
        if isinstance(value, float):
            raise ValueError(
                "Money.amount must be Decimal (or int/str), never float — "
                f"got float {value!r}. Construct with Decimal(\"{value}\") from the "
                "original string/decimal the provider sent, not a float literal."
            )
        return value

    @field_validator("amount", mode="after")
    @classmethod
    def _quantize(cls, value: Decimal) -> Decimal:
        """Quantize to the cent, ROUND_HALF_UP, at construction time.

        mapping_sketch.md S4.2 flags that FX-converted *intermediates*
        should not be quantized before the next multiplication — but that
        is a normalize/fx concern about *when* to wrap a value in
        ``Money``, not something ``Money`` itself can vary: once a value
        is presented as a quoted price, master plan S4 is explicit that it
        is quantized here, unconditionally.
        """
        return value.quantize(_CENT, rounding=ROUND_HALF_UP)
