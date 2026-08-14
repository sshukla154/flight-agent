"""Mock ``FlightProvider`` (master plan S3, D6).

The permanent, zero-credential, zero-network data source this project
ships with -- not a temporary stub for when real credentials are missing,
but the actual demo path (D6, master plan S8.5).
"""

from __future__ import annotations

from flightagent.providers.mock.generator import compute_seed, generate_offers
from flightagent.providers.mock.provider import MOCK_API_VERSION, MockProvider

__all__ = ["MOCK_API_VERSION", "MockProvider", "compute_seed", "generate_offers"]
