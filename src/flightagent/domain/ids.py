"""Deterministic content-hash identifiers.

Finding 0.3: ``itinerary_id`` must be a pure function of an itinerary's
content, independent of dict/list ordering, because it is the final
tiebreak key that makes ranking reproducible under concurrent 160-way
fan-out — pre-sorting the ranked list by ``itinerary_id`` before Python's
stable ``sorted()`` is what stops provider response arrival order from
leaking into the output order.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from uuid import uuid4

from flightagent.domain.money import Money


def compute_itinerary_id(shape_key: str, carriers: Iterable[str], price: Money) -> str:
    """Content hash over the itinerary shape key, its marketing carriers,
    and its price (finding 0.3).

    Deterministic regardless of the iteration order of ``carriers`` — the
    caller may pass a ``set``, ``frozenset``, ``list``, or any order at
    all; carriers are sorted before hashing so the result never depends on
    how the caller happened to enumerate them. ``shape_key`` is expected
    to already be a canonical string (built by the normalize/dedup step,
    out of scope here, per finding 0.2's shape-key formula); this function
    only hashes it, it does not construct it.
    """
    canonical_carriers = ",".join(sorted(str(carrier) for carrier in carriers))
    canonical = "|".join((shape_key, canonical_carriers, str(price.amount), price.currency))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"itin_{digest}"


def compute_task_id(origin: str, destination: str, max_stops: int) -> str:
    """Deterministic, human-readable task id.

    Master plan S7: ``f"{origin}-{destination}-s{max_stops}"`` — this
    exact shape matters because it makes two runs' logs directly
    diffable, which is how a ranking change gets debugged.
    """
    return f"{origin}-{destination}-s{max_stops}"


def generate_run_id() -> str:
    """Fresh run correlation id.

    UUID4 — matching ``observability.context.new_run_id``'s convention
    (see that module's docstring for the full ULID-vs-UUID4 rationale).
    ``domain`` imports nothing internal (master plan S3), so this
    function does not import ``observability.context`` directly; it
    independently follows the same convention rather than inventing a
    second one, per this task's explicit instruction to reuse whatever
    T6 already chose.
    """
    return str(uuid4())
