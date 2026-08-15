"""Provider error taxonomy.

Master plan S5 lists a layered set of per-task controls — semaphore,
``asyncio.timeout``, circuit breaker, retry loop, token bucket — and S1
requires that error handling be an auditable table lookup ("Known error
codes go through a deterministic table"), never a probabilistic guess.
This module is only the taxonomy that table will consult: a closed
``Retryability`` classification plus a small hierarchy of concrete error
types. The retry loop, backoff schedule, circuit breaker and token bucket
that actually *act* on ``Retryability`` are Phase 4 — deliberately not
built here.

D6: the real Amadeus/Duffel adapters (Phase 7) ship interface-complete but
raise ``ProviderNotConfigured`` at runtime when credentials are absent —
that is the exact identifier D6 names, kept as its own subclass of
``ProviderConfigError`` so callers can catch the specific "no credentials"
case without a string comparison.
"""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from typing import ClassVar


class Retryability(StrEnum):
    """Whether a caller should retry an error like this.

    Two values only — deliberately not finer-grained (e.g. no
    "retry-with-backoff" vs "retry-immediately" distinction here). That
    policy detail belongs to the Phase 4 retry loop that reads this
    classification, not to the taxonomy itself.
    """

    TRANSIENT = "transient"
    """Retrying, possibly after a delay, may succeed — the failure was a
    property of one attempt (a timeout, a rate limit, a 5xx), not of the
    request itself."""

    PERMANENT = "permanent"
    """Retrying cannot succeed without something external changing first
    (fixing configuration, correcting a request) — the failure is a
    property of the request or the deployment, not of one attempt."""


class ProviderError(Exception):
    """Base of the provider error hierarchy.

    Every concrete subclass MUST set the class-level ``retryability``.
    There is deliberately no default on this base class: an unclassified
    error silently defaulting to either value would be a guess wearing the
    costume of a fact, and master plan S1 requires retry decisions to stay
    auditable table lookups. Accessing ``ProviderError.retryability``
    directly (or on a subclass that forgot to set it) raises
    ``AttributeError`` rather than returning a plausible-looking default.
    """

    retryability: ClassVar[Retryability]

    def __init__(self, message: str, *, provider: str | None = None) -> None:
        super().__init__(message)
        self.provider = provider


class ProviderTimeoutError(ProviderError):
    """The provider did not respond within the call's budget.

    Transient: master plan S5's HTTP timeout tuple
    (``Timeout(connect=5, read=15, write=5, pool=5)``) and the per-task
    ``asyncio.timeout(20)`` both exist because a slow attempt is not
    evidence the next attempt will also be slow.
    """

    retryability = Retryability.TRANSIENT


class ProviderRateLimitedError(ProviderError):
    """The provider refused the call as rate-limited (HTTP 429 or the
    provider's equivalent).

    Transient, but master plan S5 says to "honour ``Retry-After``" rather
    than retry immediately — ``retry_after`` carries that value when the
    provider supplied one. This class only carries the fact; the token
    bucket / backoff schedule that acts on it is Phase 4.
    """

    retryability = Retryability.TRANSIENT

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        retry_after: timedelta | None = None,
    ) -> None:
        super().__init__(message, provider=provider)
        self.retry_after = retry_after


class ProviderConfigError(ProviderError):
    """The provider is misconfigured — a deployment/code problem, not a
    network condition.

    Permanent: no amount of retrying fixes a missing setting or a bad
    credential; a human has to change something first.
    """

    retryability = Retryability.PERMANENT


class ProviderNotConfigured(ProviderConfigError):
    """Required credentials are absent for this provider.

    D6's exact scenario and exact identifier, for the real Amadeus/Duffel
    adapters landing in Phase 7: mock-only ships now, and those adapters
    are interface-complete but raise this at runtime rather than attempt a
    call with no credentials configured.
    """
