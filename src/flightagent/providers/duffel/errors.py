"""Duffel error-type-to-``Retryability`` table (STUB) -- see
``amadeus/errors.py``'s own docstring for why this is deliberately coarse and
unverified rather than a fully fleshed-out table.

Duffel's documented error envelope is
``{"errors": [{"type": ..., "code": ..., "title": ..., "message": ...}]}``,
and ``type`` (not ``code``) is Duffel's own top-level classification -- this
table is keyed on ``type``, matching Duffel's documented error-handling
categories, rather than the far larger and more provider-specific ``code``
enum. As with ``amadeus/errors.py``, no fixture in this codebase captures a
real Duffel error response, so this is a stub for a future retry loop
(Phase 4) to consult, not an exhaustive, verified table.
"""

from __future__ import annotations

from flightagent.providers.errors import Retryability

DUFFEL_ERROR_TYPE_RETRYABILITY: dict[str, Retryability] = {
    "authentication_error": Retryability.PERMANENT,
    "invalid_request_error": Retryability.PERMANENT,
    "validation_error": Retryability.PERMANENT,
    "invalid_state_error": Retryability.PERMANENT,
    "rate_limit_error": Retryability.TRANSIENT,  # honour Retry-After (Phase 4)
    "airline_error": Retryability.TRANSIENT,
    "internal_server_error": Retryability.TRANSIENT,
}


def classify_duffel_error(error_type: str) -> Retryability | None:
    """Look up ``error_type`` (Duffel's ``errors[].type``) in the stub table
    above. Returns ``None`` for a type this table does not (yet) classify --
    never guesses (master plan S1).
    """
    return DUFFEL_ERROR_TYPE_RETRYABILITY.get(error_type)
