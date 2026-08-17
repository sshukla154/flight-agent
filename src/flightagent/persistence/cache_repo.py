"""Async two-layer SQLite cache repository, TTL tiering, hit/miss/write
counters (T43, master plan S5 "Caching").

``CacheRepository`` wraps a single ``sqlite3.Connection`` (see ``db.py``)
and exposes async ``get_raw``/``put_raw``/``get_normalized``/``put_normalized``
methods over the two tables ``schema.sql`` defines. Every blocking
``sqlite3`` call is delegated to ``asyncio.to_thread`` -- master plan S5:
"`sqlite3` is blocking... All access through a `CacheRepository` with
async methods delegating to `asyncio.to_thread`" -- so a caller awaiting
one of these methods from inside the orchestrator's concurrent 8-way
search fan-out never blocks the event loop on disk I/O.

TTL is genuinely date-driven (``resolve_ttl``), never a hardcoded tier:
this project's own actual departure date (2027-07-17, ~337 days from
2026-08-14 as of writing) always lands in the >180-day tier, but the
function itself is a pure ``(departure_date, as_of) -> timedelta``
computation that has to be correct for every tier, not just the one this
project happens to exercise at runtime.

Clock injection follows this codebase's own established convention rather
than inventing a second one: ``cli._deterministic_as_of`` and every
``normalize``/``reporting`` call already take a caller-supplied instant
(``fare_as_of``, ``generated_at``) instead of calling ``datetime.now()``
internally, specifically so tests can pass an arbitrary fixed value. Every
time-sensitive method here (``resolve_ttl``, ``get_raw``, ``put_raw``,
``get_normalized``, ``put_normalized``) takes ``now``/``as_of`` as an
explicit parameter for exactly that reason -- there is no
``datetime.now()`` call anywhere in this module.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from flightagent.config.models import CacheSettings
from flightagent.observability.events import EventName
from flightagent.observability.logging import log_event
from flightagent.persistence.db import connect


def _parse_expires_at(expires_at_raw: str) -> datetime | None:
    """Best-effort ISO-8601 parse of a stored ``expires_at`` value -- never
    raises.

    Guards against a corrupted or hand-tampered row (direct SQL against
    the on-disk file, disk-level bit rot, a manual edit) reaching
    ``datetime.fromisoformat`` with a value it cannot parse. A row that
    fails this parse can never be proven live, so ``get_raw``/
    ``get_normalized`` treat a ``None`` result here exactly like an
    expired entry -- a miss, never a crash -- and additionally evict the
    offending row (see ``_delete_raw``/``_delete_normalized``) so the same
    corrupted value can't repeat the failure on a later lookup.
    """
    try:
        return datetime.fromisoformat(expires_at_raw)
    except (TypeError, ValueError):
        return None


def resolve_ttl(*, departure_date: date, as_of: date, settings: CacheSettings) -> timedelta:
    """TTL tiered by days-to-departure (master plan S5,
    ``config/defaults.toml``'s ``[cache]`` table): >180d, 30-180d, 7-30d,
    <7d.

    Boundaries are LOWER-INCLUSIVE HALF-OPEN, the same convention this
    codebase already uses for the layover penalty bands (D9,
    ``config/defaults.toml``'s own ``[[layover.penalty_bands]]`` comment)
    -- chosen here for the identical reason: the master plan's prose
    ("30-180d", "7-30d") overlaps at 30 and at 180/181 if read naively, and
    a lower-inclusive half-open reading is the only one that partitions
    every non-negative day count into exactly one tier with no gap and no
    overlap:

    - ``days_to_departure > 180``                -> ``ttl_minutes_over_180_days``
    - ``30 <= days_to_departure <= 180``          -> ``ttl_minutes_30_to_180_days``
    - ``7  <= days_to_departure < 30``            -> ``ttl_minutes_7_to_30_days``
    - ``days_to_departure < 7`` (including a past/same-day departure)
                                                    -> ``ttl_minutes_under_7_days``

    ``as_of`` is the caller-supplied "today" -- never ``date.today()``
    internally, per this module's own clock-injection convention (see
    module docstring). Passing the real "today" is the caller's job.
    """
    days_to_departure = (departure_date - as_of).days
    if days_to_departure > 180:
        minutes = settings.ttl_minutes_over_180_days
    elif days_to_departure >= 30:
        minutes = settings.ttl_minutes_30_to_180_days
    elif days_to_departure >= 7:
        minutes = settings.ttl_minutes_7_to_30_days
    else:
        minutes = settings.ttl_minutes_under_7_days
    return timedelta(minutes=minutes)


@dataclass
class CacheCounters:
    """In-process hit/miss/write tallies for one ``CacheRepository`` instance.

    A plain running count alongside (not instead of) the
    ``EventName.CACHE_HIT``/``CACHE_MISS``/``CACHE_WRITE`` structured log
    lines each operation also emits -- master plan S7's ``run.metrics``
    event carries a ``cache_hit_ratio`` figure at run end, and a caller
    assembling that figure needs an in-memory tally to read rather than
    re-parsing its own just-emitted log stream. Two independent counter
    pairs, one per cache layer (raw vs. normalized) -- the two tables can
    legitimately diverge (a normalizer-version bump invalidates the
    normalized layer without touching the raw layer at all, see
    ``keys.compute_normalized_key``'s docstring), so collapsing them into
    one shared tally would hide that.
    """

    raw_hits: int = field(default=0)
    raw_misses: int = field(default=0)
    raw_writes: int = field(default=0)
    normalized_hits: int = field(default=0)
    normalized_misses: int = field(default=0)
    normalized_writes: int = field(default=0)


class CacheRepository:
    """Async wrapper over the two-layer SQLite cache.

    One ``sqlite3.Connection`` per instance. A caller needing concurrent
    access from multiple OS processes should point each at the same
    on-disk file -- WAL mode (``db.connect``) is what makes concurrent
    *readers* safe there; this class itself does no cross-instance
    coordination beyond what SQLite's own WAL journal provides.

    ``_lock`` (a plain ``threading.Lock``, not ``asyncio.Lock``) serializes
    every actual ``self._connection`` call. Each public method dispatches
    its blocking work to ``asyncio.to_thread``, whose default executor is a
    thread POOL -- once ``orchestration.executor.execute_plan`` started
    driving this class under its real concurrent (semaphore-bounded)
    fan-out, two ``put_raw`` calls landing on two different pool threads at
    the same moment produced a reproducible native access violation
    (Windows, CPython 3.12.13) inside ``_upsert_raw``. ``sqlite3.threadsafety``
    reports ``3`` ("serialized") on this build, which was expected to make
    exactly this safe without an extra lock -- it did not, in practice.
    ``asyncio.Lock`` would not have fixed this: it only orders coroutines on
    one event-loop thread, and every caller here is already a *worker*
    thread, not the event loop thread. A plain ``threading.Lock`` is the
    correct primitive and costs nothing extra -- SQLite already serializes
    disk access on one file internally, so forcing Python-level
    serialization here removes a real crash, not a real amount of
    concurrency.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._lock = threading.Lock()
        self.counters = CacheCounters()

    @classmethod
    def open(cls, db_path: Path | str) -> CacheRepository:
        """Open a fresh connection (WAL + schema applied, see ``db.connect``)
        and wrap it. Convenience constructor for real (non-test) callers;
        tests are free to construct ``CacheRepository(connect(":memory:"))``
        directly when they need the raw connection too.
        """
        return cls(connect(db_path))

    def close(self) -> None:
        self._connection.close()

    # -- raw layer --------------------------------------------------------

    async def get_raw(
        self,
        raw_key: str,
        *,
        now: datetime,
        provider: str,
        origin: str,
        destination: str,
    ) -> str | None:
        """Look up ``raw_key`` in ``raw_payloads``.

        Returns the stored payload string on a live hit, ``None`` on a
        miss -- either because no row exists, or because the row exists
        but ``now`` is at or past its stored ``expires_at`` (an expired
        entry is treated identically to an absent one, never served: T43's
        own instruction and master plan S8.6, "never persist a stale
        cached fare as live"). Emits exactly one
        ``EventName.CACHE_HIT``/``CACHE_MISS`` log line and increments the
        matching ``counters`` field either way.
        """
        row = await asyncio.to_thread(self._select_raw, raw_key)
        if row is not None:
            payload, expires_at_raw = row
            expires_at = _parse_expires_at(expires_at_raw)
            if expires_at is not None and now < expires_at:
                self.counters.raw_hits += 1
                log_event(
                    EventName.CACHE_HIT,
                    provider=provider,
                    origin=origin,
                    destination=destination,
                    cache_key=raw_key,
                    layer="raw",
                )
                return payload
            if expires_at is None:
                # Corrupted row: expires_at could not be parsed, so this
                # row can never be proven live -- evict it rather than
                # leaving it to fail the same way on every future lookup.
                await asyncio.to_thread(self._delete_raw, raw_key)

        self.counters.raw_misses += 1
        log_event(
            EventName.CACHE_MISS,
            provider=provider,
            origin=origin,
            destination=destination,
            cache_key=raw_key,
            layer="raw",
        )
        return None

    def _select_raw(self, raw_key: str) -> tuple[str, str] | None:
        with self._lock:
            cursor = self._connection.execute(
                "SELECT payload, expires_at FROM raw_payloads WHERE raw_key = ?",
                (raw_key,),
            )
            row = cursor.fetchone()
        return None if row is None else (row[0], row[1])

    def _delete_raw(self, raw_key: str) -> None:
        with self._lock:
            self._connection.execute("DELETE FROM raw_payloads WHERE raw_key = ?", (raw_key,))
            self._connection.commit()

    async def put_raw(
        self,
        raw_key: str,
        payload: str,
        *,
        provider: str,
        api_version: str,
        origin: str,
        destination: str,
        departure_date: date,
        now: datetime,
        ttl: timedelta,
    ) -> None:
        """Upsert ``raw_key`` in ``raw_payloads`` with the given ``payload``,
        stamping ``retrieved_at=now`` and ``expires_at=now + ttl``.

        ``ttl`` is supplied by the caller (typically ``resolve_ttl``'s
        result) rather than computed here -- this method only persists,
        it does not decide tiering, keeping the two concerns independently
        testable.
        """
        expires_at = now + ttl
        await asyncio.to_thread(
            self._upsert_raw,
            raw_key,
            payload,
            provider,
            api_version,
            origin,
            destination,
            departure_date.isoformat(),
            now.isoformat(),
            expires_at.isoformat(),
        )
        self.counters.raw_writes += 1
        log_event(
            EventName.CACHE_WRITE,
            provider=provider,
            origin=origin,
            destination=destination,
            cache_key=raw_key,
            ttl_seconds=int(ttl.total_seconds()),
            layer="raw",
        )

    def _upsert_raw(
        self,
        raw_key: str,
        payload: str,
        provider: str,
        api_version: str,
        origin: str,
        destination: str,
        departure_date_iso: str,
        retrieved_at_iso: str,
        expires_at_iso: str,
    ) -> None:
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO raw_payloads
                    (raw_key, provider, api_version, origin, destination,
                     departure_date, payload, retrieved_at, expires_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(raw_key) DO UPDATE SET
                    payload = excluded.payload,
                    retrieved_at = excluded.retrieved_at,
                    expires_at = excluded.expires_at
                """,
                (
                    raw_key,
                    provider,
                    api_version,
                    origin,
                    destination,
                    departure_date_iso,
                    payload,
                    retrieved_at_iso,
                    expires_at_iso,
                    retrieved_at_iso,
                ),
            )
            self._connection.commit()

    # -- normalized layer ---------------------------------------------------

    async def get_normalized(
        self,
        normalized_key: str,
        *,
        now: datetime,
        provider: str,
        origin: str,
        destination: str,
    ) -> str | None:
        """``get_raw``'s exact counterpart over ``normalized_offers`` --
        see that method's docstring for the hit/miss/expiry contract,
        identical here except for the table and the ``normalized_*``
        counters incremented.
        """
        row = await asyncio.to_thread(self._select_normalized, normalized_key)
        if row is not None:
            payload, expires_at_raw = row
            expires_at = _parse_expires_at(expires_at_raw)
            if expires_at is not None and now < expires_at:
                self.counters.normalized_hits += 1
                log_event(
                    EventName.CACHE_HIT,
                    provider=provider,
                    origin=origin,
                    destination=destination,
                    cache_key=normalized_key,
                    layer="normalized",
                )
                return payload
            if expires_at is None:
                # Corrupted row -- see get_raw's identical guard above.
                await asyncio.to_thread(self._delete_normalized, normalized_key)

        self.counters.normalized_misses += 1
        log_event(
            EventName.CACHE_MISS,
            provider=provider,
            origin=origin,
            destination=destination,
            cache_key=normalized_key,
            layer="normalized",
        )
        return None

    def _select_normalized(self, normalized_key: str) -> tuple[str, str] | None:
        with self._lock:
            cursor = self._connection.execute(
                "SELECT payload, expires_at FROM normalized_offers WHERE normalized_key = ?",
                (normalized_key,),
            )
            row = cursor.fetchone()
        return None if row is None else (row[0], row[1])

    def _delete_normalized(self, normalized_key: str) -> None:
        with self._lock:
            self._connection.execute(
                "DELETE FROM normalized_offers WHERE normalized_key = ?", (normalized_key,)
            )
            self._connection.commit()

    async def put_normalized(
        self,
        normalized_key: str,
        raw_key: str,
        payload: str,
        *,
        normalizer_version: str,
        fx_source_id: str,
        provider: str,
        origin: str,
        destination: str,
        now: datetime,
        ttl: timedelta,
    ) -> None:
        """``put_raw``'s exact counterpart over ``normalized_offers``."""
        expires_at = now + ttl
        await asyncio.to_thread(
            self._upsert_normalized,
            normalized_key,
            raw_key,
            payload,
            normalizer_version,
            fx_source_id,
            now.isoformat(),
            expires_at.isoformat(),
        )
        self.counters.normalized_writes += 1
        log_event(
            EventName.CACHE_WRITE,
            provider=provider,
            origin=origin,
            destination=destination,
            cache_key=normalized_key,
            ttl_seconds=int(ttl.total_seconds()),
            layer="normalized",
        )

    def _upsert_normalized(
        self,
        normalized_key: str,
        raw_key: str,
        payload: str,
        normalizer_version: str,
        fx_source_id: str,
        retrieved_at_iso: str,
        expires_at_iso: str,
    ) -> None:
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO normalized_offers
                    (normalized_key, raw_key, normalizer_version, fx_source_id,
                     payload, retrieved_at, expires_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(normalized_key) DO UPDATE SET
                    payload = excluded.payload,
                    retrieved_at = excluded.retrieved_at,
                    expires_at = excluded.expires_at
                """,
                (
                    normalized_key,
                    raw_key,
                    normalizer_version,
                    fx_source_id,
                    payload,
                    retrieved_at_iso,
                    expires_at_iso,
                    retrieved_at_iso,
                ),
            )
            self._connection.commit()


__all__ = ["CacheCounters", "CacheRepository", "resolve_ttl"]
