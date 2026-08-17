"""Two-layer cache key derivation (T43, master plan S5 "Caching").

    raw_key        = sha256(provider | provider_api_version | origin |
                             destination | departure_date | cabin |
                             max_stops | adults | currency)
    normalized_key = sha256(raw_key | normalizer_version | fx_source_id)

Both functions build their canonical string from a FIXED-ORDER TUPLE of
explicitly-named fields, never by hashing a dict (``SearchRequest.model_dump()``
or any hand-built mapping). Hashing a dict's serialization is exactly the
trap this module has to avoid: two logically-identical dicts are not
guaranteed to serialize to the same bytes if their key order differs (a
hand-rolled ``"|".join(f"{k}={v}" for k, v in some_dict.items())`` would
silently depend on insertion order), which would turn a harmless
construction-order difference into a spurious cache miss. Reading each
field off ``request``/``capabilities`` by name below sidesteps that
entirely -- the tuple order is fixed by this function's own source code,
never by how a caller happened to build the object being hashed.

Matches every other content-hash convention already established in this
codebase (``domain.ids``: sha256 over a ``"|".join(...)`` of explicitly
canonicalized fields, e.g. ``compute_itinerary_id``'s sorted-carriers
join) -- this module does not invent a second hashing convention.
"""

from __future__ import annotations

import hashlib

from flightagent.domain.run import SearchRequest
from flightagent.providers.base import ProviderCapabilities


def _sha256_of(*fields: str) -> str:
    """Hash a fixed-order sequence of string fields, joined with ``|``.

    The one hashing primitive both key functions below share -- neither
    calls ``hashlib.sha256`` directly, so there is exactly one place that
    ever decides the join character and encoding.
    """
    canonical = "|".join(fields)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_raw_key(request: SearchRequest, capabilities: ProviderCapabilities) -> str:
    """Master plan S5's ``raw_key``, over the request's search-shape fields
    plus the provider identity that answered it.

    ``capabilities.api_version`` is exactly what feeds this per that
    class's own docstring in ``providers.base``. Deliberately excludes
    ``request.adults``' upstream passenger-detail fields, ``trip_type``,
    and the layover-window fields -- master plan S5 names precisely these
    nine components and no others; layover bounds are a validation-time
    concern (D8/D9), not part of what makes two provider searches the
    "same search" for caching purposes.
    """
    return _sha256_of(
        capabilities.provider_name,
        capabilities.api_version,
        request.origin,
        request.destination,
        request.departure_date.isoformat(),
        str(request.cabin),
        str(request.max_stops),
        str(request.adults),
        request.currency,
    )


def compute_normalized_key(raw_key: str, *, normalizer_version: str, fx_source_id: str) -> str:
    """Master plan S5's ``normalized_key`` -- folds the raw key together
    with the two things that can make an identical raw payload normalize
    to different output: the normalizer's own version, and which FX source
    supplied the conversion rate (D14). Bumping either without a new API
    call produces a new ``normalized_key`` while ``raw_payloads`` is
    untouched -- the whole reason for the two-table split (see
    ``schema.sql``'s own docstring comment).
    """
    return _sha256_of(raw_key, normalizer_version, fx_source_id)
