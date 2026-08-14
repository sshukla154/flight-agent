"""flightagent.tools — MCP-exposed tool functions (master plan §1,
"MCP tool granularity"). ``airport_info`` is the first of these (T8);
the rest of the coarse tool surface (e.g. ``search_flights``) lands in
later tasks and will live in this same package.
"""

from __future__ import annotations

from flightagent.tools.airport_info import airport_info

__all__ = ["airport_info"]
