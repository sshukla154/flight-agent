"""Duffel v2 adapter (Phase 7, T49).

D6: interface-complete, uncredentialed. ``provider.py``'s ``DuffelProvider``
satisfies ``FlightProvider`` structurally and always raises
``ProviderNotConfigured`` from ``search()`` -- no static-bearer credential is
ever attached to a request (``auth.py`` is a stub). ``mapper.py`` is the one
real, working piece: it maps the actual Duffel payload shape (documented and
tested against ``tests/fixtures/providers/duffel_offers_sample.json``, whose
own two-step ``offer_request_response``/``offers_list_response`` wrapper
differs structurally from Amadeus's single-response shape -- see that
module's docstring) into domain objects.
"""

from __future__ import annotations
