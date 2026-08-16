"""``DuffelProvider`` -- interface-complete, uncredentialed ``FlightProvider``.

D6: mirrors ``amadeus/provider.py``'s own contract exactly, but declares
Duffel's actual capabilities (static-bearer auth, opaque cursor pagination,
no native-currency forcing, ``max_connections`` stop filtering -- master
plan S5's own Amadeus/Duffel comparison table) rather than Amadeus's.
"""

from __future__ import annotations

from flightagent.domain.run import SearchRequest
from flightagent.providers.base import CallBudget, ProviderCapabilities, ProviderSearchResult
from flightagent.providers.duffel.auth import DUFFEL_VERSION_HEADER
from flightagent.providers.errors import ProviderNotConfigured


class DuffelProvider:
    """Satisfies ``FlightProvider`` structurally -- see
    ``amadeus/provider.py``'s identical note on why this is not a subclass
    of anything.
    """

    def __init__(self, *, api_key: str | None = None) -> None:
        """``api_key`` is accepted for interface completeness but never
        consulted -- see ``amadeus/provider.py``'s identical note.
        ``search()`` raises unconditionally regardless of what is passed
        here (D6): there is no ``DuffelAuthClient`` call actually wired up
        to consume it.
        """
        self._api_key = api_key

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_name="duffel",
            api_version=DUFFEL_VERSION_HEADER,
            auth_style="static_bearer",
            paginated=True,
            native_currency_forceable=False,
            returns_booking_url=False,
            stop_filter_style="max_connections_param",
        )

    async def search(self, request: SearchRequest, budget: CallBudget) -> ProviderSearchResult:
        """Always raises ``ProviderNotConfigured`` (D6) -- no static-bearer
        credential is ever attached to a request (``auth.py`` is a stub),
        so there is no lawful way to make a real Duffel call in this phase.
        ``request``/``budget`` are accepted to satisfy the
        ``FlightProvider`` shape but never consulted.
        """
        raise ProviderNotConfigured(
            "DuffelProvider has no API key wired up (D6, Phase 7 interface-complete stub) "
            "-- no real Duffel call can be made regardless of credentials supplied to "
            "__init__.",
            provider="duffel",
        )
