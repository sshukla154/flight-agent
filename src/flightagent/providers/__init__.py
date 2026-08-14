"""Flight-search provider abstraction (master plan S5).

``providers`` may import ``domain`` and ``config`` only (master plan S3's
import-linter contract) — nothing in this package reaches into
``orchestration`` or ``agent``.
"""

from __future__ import annotations

from flightagent.providers.base import (
    CallBudget,
    FlightProvider,
    ProviderCapabilities,
    ProviderSearchResult,
)
from flightagent.providers.errors import (
    ProviderConfigError,
    ProviderError,
    ProviderNotConfigured,
    ProviderRateLimitedError,
    ProviderTimeoutError,
    Retryability,
)

__all__ = [
    "CallBudget",
    "FlightProvider",
    "ProviderCapabilities",
    "ProviderConfigError",
    "ProviderError",
    "ProviderNotConfigured",
    "ProviderRateLimitedError",
    "ProviderSearchResult",
    "ProviderTimeoutError",
    "Retryability",
]
