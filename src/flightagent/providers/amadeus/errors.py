"""Amadeus error-code-to-``Retryability`` table (STUB).

``providers.errors`` (T9) already establishes the rule this table has to
follow: "known error codes go through a deterministic table" (master plan
S1), never a probabilistic guess, and an unclassified error must fail loudly
rather than default to a plausible-looking retryability.

This table is deliberately coarse and keyed on HTTP status code, not
Amadeus's own numeric ``errors[].code`` values (e.g. the four/five-digit
codes documented per endpoint). No fixture in this codebase captures an
Amadeus *error* response -- ``tests/fixtures/providers/README.md``'s entire
"needs verification" section is about offer *shape*, and it exists precisely
because nothing here has ever been checked against a live call. Inventing a
numeric-code table now would be exactly the kind of unverified guess that
document's own honesty policy exists to flag, so this stays at the one
signal every HTTP-based API guarantees regardless of provider-specific
error-code conventions. Wiring this into an actual retry decision is Phase 4
(``providers.errors``'s own docstring: "the retry loop ... is Phase 4 --
deliberately not built here"); this module only supplies the lookup table a
future retry loop would consult once ``AmadeusProvider`` makes real HTTP
calls.
"""

from __future__ import annotations

from flightagent.providers.errors import Retryability

AMADEUS_STATUS_RETRYABILITY: dict[int, Retryability] = {
    400: Retryability.PERMANENT,  # INVALID FORMAT / malformed request
    401: Retryability.PERMANENT,  # invalid or expired OAuth2 access token
    403: Retryability.PERMANENT,  # forbidden -- app not entitled to this resource
    404: Retryability.PERMANENT,  # NOT FOUND
    422: Retryability.PERMANENT,  # UNABLE TO PROCESS (e.g. unsatisfiable search)
    429: Retryability.TRANSIENT,  # rate limited -- honour Retry-After (Phase 4)
    500: Retryability.TRANSIENT,  # SYSTEM ERROR HAS OCCURRED
    502: Retryability.TRANSIENT,
    503: Retryability.TRANSIENT,  # temporarily unavailable
    504: Retryability.TRANSIENT,
}


def classify_amadeus_error(http_status: int) -> Retryability | None:
    """Look up ``http_status`` in the stub table above.

    Returns ``None`` for a status this table does not (yet) classify --
    the caller decides what an unclassified status means; this function
    never guesses one (master plan S1).
    """
    return AMADEUS_STATUS_RETRYABILITY.get(http_status)
