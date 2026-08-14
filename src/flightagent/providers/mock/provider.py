"""``MockProvider`` -- the T10 ``FlightProvider`` implementation.

D6: mock-only ships as the deliverable; this is a first-class, permanent
code path, not a temporary stub. Two modes, chosen at construction time:

(a) PROGRAMMATIC (default) -- ``generator.generate_offers`` builds a
    small, deterministic set of synthetic offers seeded from the request
    itself (master plan S5's determinism trap: never a shared RNG).
(b) FIXTURE-FILE -- loads a committed JSON scenario file
    (``fixtures/ams_del_onestop.json``) and returns its offers verbatim.
    No randomness, and the request is not consulted to filter or alter
    what the fixture contains -- what's in the file is what comes back.

This module constructs ``RawOffer``/``Leg``/``Segment`` domain objects
directly (mode a) or deserializes them straight from a committed,
domain-shaped JSON file (mode b). It never touches the hand-authored
Amadeus/Duffel provider-payload fixtures under
``tests/fixtures/providers/`` -- those exist for Phase 7's real-adapter
mapper tests (T49), a different concern entirely.
"""

from __future__ import annotations

import json
from pathlib import Path

from flightagent.domain.itinerary import RawOffer
from flightagent.domain.run import SearchRequest
from flightagent.providers.base import CallBudget, ProviderCapabilities, ProviderSearchResult
from flightagent.providers.mock.generator import generate_offers

MOCK_API_VERSION = "mock-v1"
"""Feeds the raw cache key's ``provider_api_version`` component (master
plan S5) -- fixed, since the mock has no real API to version."""


class MockProvider:
    """Synthetic ``FlightProvider`` -- satisfies the protocol structurally
    (see ``providers/base.py``'s own docstring on why ``FlightProvider`` is
    a ``Protocol`` rather than an ABC): this class is not a subclass of
    anything, it just has the right ``capabilities`` property and
    ``search`` coroutine shape.
    """

    def __init__(self, *, fixture_path: Path | str | None = None) -> None:
        """``fixture_path`` selects FIXTURE-FILE mode. ``None`` (the
        default) selects PROGRAMMATIC mode."""
        self._fixture_path = Path(fixture_path) if fixture_path is not None else None

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_name="mock",
            api_version=MOCK_API_VERSION,
            auth_style="none",
            paginated=False,
            native_currency_forceable=True,
            returns_booking_url=True,
            stop_filter_style="nonstop_boolean",
        )

    async def search(self, request: SearchRequest, budget: CallBudget) -> ProviderSearchResult:
        """``budget`` is accepted to satisfy the ``FlightProvider`` shape
        but unused: the mock never makes a network call, so there is
        nothing for a timeout or a page cap to bound."""
        offers = (
            _load_fixture(self._fixture_path)
            if self._fixture_path is not None
            else generate_offers(request)
        )
        return ProviderSearchResult(
            offers=offers,
            truncated=False,
            pages_fetched=1,
            http_calls=1,
            raw_payload_refs=tuple(offer.raw_payload_ref for offer in offers),
        )


def _load_fixture(path: Path) -> tuple[RawOffer, ...]:
    """Parse a committed fixture-scenario file into ``RawOffer``s, verbatim.

    The file holds the DOMAIN model's own JSON shape
    (``{"offers": [RawOffer.model_dump(mode="json"), ...]}``), never a raw
    provider payload -- see this module's docstring and
    ``fixtures/ams_del_onestop.json``.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    return tuple(RawOffer.model_validate(item) for item in payload["offers"])
