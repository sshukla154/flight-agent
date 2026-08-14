"""The FlightProvider protocol, and the types its search method carries.

Master plan S5 ("Provider abstraction"): ``search()`` is deliberately
coarse — one logical search in, a complete offer list out. The adapter
internally does however many round trips and pages it needs (Amadeus's one
call vs Duffel's create-then-poll two-step), and nothing above the adapter
knows an "offer request" concept exists. ``ProviderSearchResult.http_calls``
is what keeps that internal cost visible to the caller without leaking it
into the return type's shape.

``FlightProvider`` is a ``Protocol`` (structural typing), per the master
plan's own module-tree comment (S3: "base.py — FlightProvider protocol +
ProviderCapabilities") rather than an ABC — the mock, Amadeus and Duffel
adapters (Phase 7+) never need to share a common base class, only the same
callable shape.

``CallBudget`` is deliberately minimal: a timeout and a pagination cap.
Full rate-limiting/retry infrastructure — the semaphore, circuit breaker,
retry loop and token bucket of master plan S5's layered per-task controls —
is Phase 4 and is NOT built here. This type exists only so
``FlightProvider.search``'s signature is real now and does not need a
breaking change when that infrastructure lands.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from flightagent.domain.itinerary import RawOffer
from flightagent.domain.run import SearchRequest


class CallBudget(BaseModel):
    """Per-call budget for a single, non-concurrent provider search.

    ``timeout`` defaults to master plan S5's per-task
    ``asyncio.timeout(20)``. ``max_pages`` defaults to S5's stated
    pagination cap ("Cap pages/task (default 3) — a 160-task run that
    paginates deeply exhausts quota"). Both are overridable per call so a
    caller (orchestration, or a test) can tighten or loosen either without
    touching the provider.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    timeout: timedelta = Field(default=timedelta(seconds=20), gt=timedelta(0))
    max_pages: int = Field(default=3, ge=1)


class ProviderCapabilities(BaseModel):
    """Static, provider-level metadata — never per-call state.

    One field per distinguishing row of master plan S5's Amadeus/Duffel
    comparison table (Auth, Search shape/Pagination, Currency, Stop
    filters, Booking URL), kept here rather than hardcoded in the adapters
    so callers (a future cache-key builder, a future rate limiter, the
    report generator) can query "can this provider do X" without importing
    a specific adapter module.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_name: str
    api_version: str
    """Feeds the raw cache key (master plan S5: ``raw_key = sha256(provider
    | provider_api_version | ...)``). For Amadeus this is the API surface
    version; for Duffel it is the mandatory ``Duffel-Version`` header
    value; for the mock provider, a fixed literal."""
    auth_style: Literal["oauth2_client_credentials", "static_bearer", "none"]
    paginated: bool
    max_pages_default: int = Field(default=3, ge=1)
    native_currency_forceable: bool
    """True when the provider accepts a target currency on the request
    itself (Amadeus's ``currencyCode=EUR``). False when it always returns
    the price in whatever currency the airline priced it in (Duffel's
    ``total_amount``/``total_currency``) and the caller must convert
    (D14) rather than assume EUR."""
    returns_booking_url: bool
    """False for both real adapters today (finding 0.6 — neither Amadeus
    nor Duffel returns a consumer booking URL). Kept as a capability
    rather than hardcoded so the mock provider, which does return a
    synthetic URL, can truthfully declare the opposite."""
    stop_filter_style: Literal["nonstop_boolean", "max_connections_param"]


class ProviderSearchResult(BaseModel):
    """Everything one ``FlightProvider.search`` call produces.

    ``offers`` is pre-dedup, pre-validation — exactly as the provider
    returned it (mapped into ``RawOffer``, never raw JSON). ``truncated``,
    ``pages_fetched`` and ``http_calls`` make the adapter's internal
    pagination cost visible without changing the return shape between a
    one-call provider and a two-step one. ``raw_payload_refs`` lets any
    normalized row trace back to the exact bytes it came from (master plan
    S5). ``provider_warnings`` is for anomalies worth surfacing (e.g. "the
    provider validated its own stop filter incorrectly") that are not
    errors — see the anomaly-triage LLM role in master plan S1, item 4.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    offers: tuple[RawOffer, ...] = ()
    truncated: bool
    pages_fetched: int = Field(ge=0)
    http_calls: int = Field(ge=0)
    raw_payload_refs: tuple[str, ...] = ()
    provider_warnings: tuple[str, ...] = ()


@runtime_checkable
class FlightProvider(Protocol):
    """One logical flight-search provider.

    Structural (``Protocol``), not an ABC — see module docstring. Marked
    ``@runtime_checkable`` so ``isinstance(candidate, FlightProvider)`` is
    a real, usable check (e.g. for a future provider registry to validate
    an adapter at registration time), not just a static-typing fiction.

    ``search`` is intentionally coarse (master plan S5): one
    ``SearchRequest`` in, one complete ``ProviderSearchResult`` out. The
    adapter decides internally how many HTTP round trips and pages that
    takes; nothing above this call knows or cares.
    """

    @property
    def capabilities(self) -> ProviderCapabilities: ...

    async def search(self, request: SearchRequest, budget: CallBudget) -> ProviderSearchResult: ...
