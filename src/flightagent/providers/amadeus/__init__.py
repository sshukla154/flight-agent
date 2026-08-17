"""Amadeus Flight Offers Search v2 adapter (Phase 7, T49).

D6: interface-complete, uncredentialed. ``provider.py``'s ``AmadeusProvider``
satisfies ``FlightProvider`` structurally and always raises
``ProviderNotConfigured`` from ``search()`` -- no OAuth2 client-credentials
flow is wired up (``auth.py`` is a stub). ``mapper.py`` is the one real,
working piece: it maps the actual Amadeus payload shape (documented and
tested against ``tests/fixtures/providers/amadeus_offers_sample.json``) into
domain objects.
"""

from __future__ import annotations
