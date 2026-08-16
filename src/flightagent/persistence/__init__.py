"""SQLite-backed two-layer cache (T43, master plan S5 "Caching: SQLite,
not DuckDB").

Public surface:

- ``db.connect`` -- open a WAL-mode connection with the schema applied.
- ``keys.compute_raw_key`` / ``keys.compute_normalized_key`` -- the
  two-layer key derivation.
- ``cache_repo.resolve_ttl`` -- days-to-departure TTL tiering.
- ``cache_repo.CacheRepository`` / ``cache_repo.CacheCounters`` -- the
  async repository and its hit/miss/write tallies.
"""

from flightagent.persistence.cache_repo import CacheCounters, CacheRepository, resolve_ttl
from flightagent.persistence.db import connect
from flightagent.persistence.keys import compute_normalized_key, compute_raw_key

__all__ = [
    "CacheCounters",
    "CacheRepository",
    "compute_normalized_key",
    "compute_raw_key",
    "connect",
    "resolve_ttl",
]
