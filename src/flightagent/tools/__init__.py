"""flightagent.tools — MCP-exposed tool functions (master plan §1,
"MCP tool granularity"). ``airport_info`` (T8) and ``save_json`` (T45,
spec §3.3) are two of the exactly three tools this registry will ever hold
-- ``search_flights`` (the coarse provider-facing search) is the third and
lands in a later task, in this same package.
"""

from __future__ import annotations

from flightagent.tools.airport_info import airport_info
from flightagent.tools.save_json import save_json

__all__ = ["airport_info", "save_json"]
