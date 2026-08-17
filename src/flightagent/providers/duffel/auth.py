"""Static-bearer-token auth stub for Duffel.

Duffel's real API authenticates with a single long-lived API key
(``Authorization: Bearer duffel_test_...`` / ``duffel_live_...``) plus a
mandatory ``Duffel-Version`` header on every call -- ``static_bearer``
(``ProviderCapabilities.auth_style``), not Amadeus's OAuth2
client-credentials grant. There is no token exchange, refresh, or expiry to
model, which makes this stub deliberately thinner than ``amadeus/auth.py``'s.

``DuffelProvider.search()`` (``provider.py``) raises ``ProviderNotConfigured``
before ever constructing a ``DuffelAuthClient`` (D6) -- this module exists so
the shape is visible and importable, not so it works.
"""

from __future__ import annotations

from dataclasses import dataclass

DUFFEL_VERSION_HEADER = "v2"
"""The mandatory ``Duffel-Version`` header value this adapter targets --
feeds ``ProviderCapabilities.api_version`` (``provider.py``), per that
field's own docstring ("for Duffel it is the mandatory `Duffel-Version`
header value")."""


@dataclass(frozen=True)
class DuffelCredentials:
    """The one secret Duffel's static-bearer auth requires.

    Never logged -- see ``amadeus/auth.py``'s identical note on
    ``observability.logging.redact``.
    """

    api_key: str


class DuffelAuthClient:
    """Stub static-bearer-token client -- shape only, no HTTP calls.

    A real implementation would attach ``Authorization: Bearer {api_key}``
    and ``Duffel-Version: {DUFFEL_VERSION_HEADER}`` to every request.
    Neither header is ever actually built here: ``auth_header`` raises
    ``NotImplementedError`` on purpose -- D6 ships this interface-complete
    but non-functional in Phase 7, and ``DuffelProvider.search()`` never
    reaches this call.
    """

    def __init__(self, credentials: DuffelCredentials) -> None:
        self._credentials = credentials

    def auth_header(self) -> dict[str, str]:
        """Would return
        ``{"Authorization": "Bearer ...", "Duffel-Version": ...}``. Not
        implemented (D6, Phase 7)."""
        raise NotImplementedError(
            "DuffelAuthClient.auth_header is a Phase 7 interface stub -- no real "
            "credential is ever attached to a request yet (D6). DuffelProvider.search() "
            "never reaches this call."
        )
