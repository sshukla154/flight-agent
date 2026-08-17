"""``AmadeusProvider`` -- interface-complete, uncredentialed ``FlightProvider``.

D6: ships as importable, testable code with a real payload mapper
(``mapper.py``) and unit tests against the Phase 0 fixture, but ``search()``
always raises ``ProviderNotConfigured`` -- no OAuth2 client-credentials flow
is wired up (``auth.py`` is a stub), so there is no lawful way to make a real
call regardless of what credentials this class is constructed with.
"""

from __future__ import annotations

from flightagent.domain.run import SearchRequest
from flightagent.providers.base import CallBudget, ProviderCapabilities, ProviderSearchResult
from flightagent.providers.errors import ProviderNotConfigured

AMADEUS_API_VERSION = "v2"
"""Amadeus Flight Offers Search's own documented API surface version --
feeds the raw cache key's ``provider_api_version`` component (master plan
S5, ``ProviderCapabilities.api_version``)."""


class AmadeusProvider:
    """Satisfies ``FlightProvider`` structurally (see ``providers/base.py``'s
    own docstring on why that is a ``Protocol``, not an ABC) -- this class
    is not a subclass of anything.
    """

    def __init__(self, *, client_id: str | None = None, client_secret: str | None = None) -> None:
        """``client_id``/``client_secret`` are accepted for interface
        completeness (a future caller wiring real credentials in has
        somewhere to put them) but never consulted: ``search()`` raises
        unconditionally regardless of what is passed here (D6) -- there is
        no OAuth2 flow (``auth.py``) actually wired up yet to consume them.
        """
        self._client_id = client_id
        self._client_secret = client_secret

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_name="amadeus",
            api_version=AMADEUS_API_VERSION,
            auth_style="oauth2_client_credentials",
            paginated=True,
            native_currency_forceable=True,
            returns_booking_url=False,
            stop_filter_style="nonstop_boolean",
        )

    async def search(self, request: SearchRequest, budget: CallBudget) -> ProviderSearchResult:
        """Always raises ``ProviderNotConfigured`` (D6) -- no OAuth2
        client-credentials flow is wired up (``auth.py`` is a stub), so
        there is no lawful way to make a real Amadeus call in this phase,
        credentials supplied to ``__init__`` or not. ``request``/``budget``
        are accepted to satisfy the ``FlightProvider`` shape but never
        consulted.
        """
        raise ProviderNotConfigured(
            "AmadeusProvider has no working OAuth2 client-credentials flow wired up yet "
            "(D6, Phase 7 interface-complete stub) -- no real Amadeus call can be made "
            "regardless of credentials supplied to __init__.",
            provider="amadeus",
        )
