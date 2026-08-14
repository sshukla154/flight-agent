"""Constrained string types for IATA airport codes and IATA carrier codes."""

from __future__ import annotations

from typing import Annotated

from pydantic import StringConstraints

IataCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
"""Exactly 3 uppercase letters, e.g. "AMS", "DEL". IATA airport code."""

CarrierCode = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9]{2}$")]
"""Exactly 2 uppercase-alphanumeric characters, e.g. "AI", "6E".

IATA carrier codes are alphanumeric, not letters-only (mapping_sketch.md
S1.1) — a letters-only pattern would reject real carriers like IndiGo
("6E") or Yakutia ("98").
"""
