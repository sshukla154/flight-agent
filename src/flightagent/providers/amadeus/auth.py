"""OAuth2 client-credentials stub for Amadeus.

Amadeus's real Flight Offers Search v2 API authenticates via OAuth2
client-credentials: POST ``client_id``/``client_secret`` to
``/v1/security/oauth2/token``, receive a bearer ``access_token`` with an
``expires_in`` TTL, attach it as ``Authorization: Bearer <token>`` on every
subsequent call, and refresh it before it expires.

None of that is implemented here. ``AmadeusProvider.search()``
(``provider.py``) raises ``ProviderNotConfigured`` before ever constructing
an ``AmadeusAuthClient`` or calling ``fetch_access_token`` (D6) -- this
module exists purely so the shape of that flow is visible and importable,
not so it works. ``fetch_access_token`` raises ``NotImplementedError``
unconditionally; there is deliberately no half-working HTTP call underneath
it that could be accidentally invoked in a credential-absent environment and
appear to almost succeed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class AmadeusCredentials:
    """The two secrets Amadeus's client-credentials grant requires.

    Never logged: ``observability.logging.redact`` already masks any dict
    key matching ``client[_-]?secret`` case-insensitively, but this type is
    never passed through ``log_event`` in the first place -- ``search()``
    raises before any credential this class holds is ever touched.
    """

    client_id: str
    client_secret: str


@dataclass(frozen=True)
class AmadeusAccessToken:
    """One OAuth2 client-credentials grant response, trimmed to what a
    caller needs to decide whether to reuse or refresh it."""

    access_token: str
    expires_at: datetime
    token_type: str = "Bearer"

    def is_expired(self, *, now: datetime) -> bool:
        return now >= self.expires_at


class AmadeusAuthClient:
    """Stub OAuth2 client-credentials client -- shape only, no HTTP calls.

    A real implementation would POST to Amadeus's token endpoint
    (``/v1/security/oauth2/token``, ``grant_type=client_credentials``),
    cache the resulting ``AmadeusAccessToken`` for its ``expires_in`` TTL,
    and transparently refresh it on expiry. None of that exists yet: the
    one method below raises ``NotImplementedError`` on purpose -- D6 ships
    this interface-complete but non-functional in Phase 7, and
    ``AmadeusProvider.search()`` never actually reaches this class (it
    raises ``ProviderNotConfigured`` first).
    """

    def __init__(self, credentials: AmadeusCredentials) -> None:
        self._credentials = credentials

    async def fetch_access_token(self) -> AmadeusAccessToken:
        """Would POST ``client_id``/``client_secret`` to Amadeus's token
        endpoint and return the resulting bearer token. Not implemented
        (D6, Phase 7)."""
        raise NotImplementedError(
            "AmadeusAuthClient.fetch_access_token is a Phase 7 interface stub -- no "
            "OAuth2 client-credentials flow is wired up yet (D6). AmadeusProvider.search() "
            "never reaches this call."
        )
