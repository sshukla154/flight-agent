"""Integration tests for the SQLite two-layer cache (T44), covering
end-to-end behavior beyond ``tests/unit/test_cache.py``'s own unit-level
schema/key/TTL/counter/logging coverage (T43).

As of this phase, nothing in ``orchestration/executor.py`` wires
``CacheRepository`` to a live provider call yet -- that module's own
docstring says so explicitly: "no cache layer exists yet ... that is
Phase 7". ``cli.py`` has zero mentions of caching either. So a genuine
end-to-end proof through the real CLI (the style ``tests/integration/
test_retry.py`` uses) is not yet possible: the CLI never consults the
cache at all today. This file instead builds the "real (or
InstrumentedProvider-backed) end-to-end path" at the layer that DOES
exist: a small test-local cache-aside helper (``_search_with_cache``)
that composes the REAL ``CacheRepository``, the REAL key-derivation
functions, and the REAL ``InstrumentedProvider`` double (never a second
throwaway fake) the way master plan S5 describes. Wiring this into
``executor.py`` for real is a future task's job, not this one's.

Async calls are driven with plain ``asyncio.run(...)`` from ordinary
synchronous test functions, matching ``tests/unit/test_cache.py``'s own
documented convention (this codebase has no ``pytest-asyncio``
dependency). Every time-sensitive call passes an explicit ``now``,
never the real wall clock, matching this codebase's clock-injection
convention (see ``cache_repo``'s own module docstring).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest

from flightagent.config.models import CacheSettings
from flightagent.domain.enums import CabinClass
from flightagent.domain.run import SearchRequest
from flightagent.persistence.cache_repo import CacheRepository, resolve_ttl
from flightagent.persistence.db import connect
from flightagent.persistence.keys import compute_raw_key
from flightagent.providers.base import CallBudget, FlightProvider, ProviderCapabilities
from tests.support.instrumented_provider import InstrumentedProvider, Succeed

# ---------------------------------------------------------------------------
# Shared fixtures / builders (deliberately local, not imported from
# tests/unit/test_cache.py -- matching this codebase's own convention of
# each test module owning its builders rather than cross-importing them).
# ---------------------------------------------------------------------------

_ORIGIN = "AMS"
_DESTINATION = "DEL"
_DEPARTURE_DATE = date(2027, 7, 17)


def _make_request(
    *,
    origin: str = _ORIGIN,
    destination: str = _DESTINATION,
    departure_date: date = _DEPARTURE_DATE,
    cabin: CabinClass = CabinClass.ECONOMY,
    max_stops: int = 1,
    adults: int = 1,
    currency: str = "EUR",
) -> SearchRequest:
    return SearchRequest(
        origin=origin,
        destination=destination,
        departure_date=departure_date,
        cabin=cabin,
        max_stops=max_stops,  # type: ignore[arg-type]
        adults=adults,
        currency=currency,
        layover_min=timedelta(minutes=180),
        layover_max=timedelta(minutes=360),
    )


def _make_capabilities(
    *, provider_name: str = "instrumented", api_version: str = "instrumented-v1"
) -> ProviderCapabilities:
    return ProviderCapabilities(
        provider_name=provider_name,
        api_version=api_version,
        auth_style="none",
        paginated=False,
        native_currency_forceable=True,
        returns_booking_url=False,
        stop_filter_style="nonstop_boolean",
    )


def _make_cache_settings(
    *,
    over_180: int = 1000,
    thirty_to_180: int = 500,
    seven_to_30: int = 200,
    under_7: int = 50,
) -> CacheSettings:
    """Deliberately distinct, non-production-default minute values -- same
    reasoning as ``tests/unit/test_cache.py``'s own identical builder: a
    boundary failure can only mean the tiering logic itself is wrong,
    never a coincidence of two tiers sharing a value.
    """
    return CacheSettings(
        ttl_minutes_over_180_days=over_180,
        ttl_minutes_30_to_180_days=thirty_to_180,
        ttl_minutes_7_to_30_days=seven_to_30,
        ttl_minutes_under_7_days=under_7,
        max_pages_per_task=3,
        db_path="cache/flightagent.sqlite3",
    )


def _open_repo() -> CacheRepository:
    return CacheRepository(connect(":memory:"))


async def _search_with_cache(
    provider: FlightProvider,
    request: SearchRequest,
    repo: CacheRepository,
    *,
    now: datetime,
    cache_settings: CacheSettings,
) -> str:
    """Test-local cache-aside glue over the raw layer: consult the cache
    first, and only call the provider on a genuine miss. Returns the raw
    payload STRING, not a reconstructed ``ProviderSearchResult``.

    Deliberately lives HERE, not in ``src/flightagent`` -- see this
    module's own docstring. Every piece this composes is real:
    ``compute_raw_key``, ``CacheRepository.get_raw``/``put_raw``,
    ``resolve_ttl``, and whatever ``FlightProvider`` the caller passes in
    (an ``InstrumentedProvider`` in every test below). The ONLY thing not
    yet real is the orchestration gluing them together, because that
    wiring does not exist in ``src/flightagent`` yet.

    Returning the opaque payload string rather than
    ``ProviderSearchResult.model_validate_json(cached)`` is deliberate,
    not a shortcut: ``schema.sql`` itself documents ``raw_payloads.payload``
    as "raw provider response bytes/JSON, verbatim" -- an opaque string
    ``CacheRepository`` stores and serves, never something it parses back
    into a domain object. ``ProviderSearchResult`` (and nested ``Leg``/
    ``Segment``) mix ``computed_field``s with ``extra="forbid"``, which
    means ``model_dump_json()`` output can never round-trip back through
    ``model_validate_json()`` on the same model -- a genuine, separate
    pydantic-modeling wrinkle in ``domain/itinerary.py`` that is out of
    this test file's scope to fix, and orthogonal to what this glue
    function needs to prove about caching.
    """
    capabilities = provider.capabilities
    raw_key = compute_raw_key(request, capabilities)
    cached = await repo.get_raw(
        raw_key,
        now=now,
        provider=capabilities.provider_name,
        origin=request.origin,
        destination=request.destination,
    )
    if cached is not None:
        return cached

    result = await provider.search(request, CallBudget())
    payload = result.model_dump_json()
    ttl = resolve_ttl(
        departure_date=request.departure_date, as_of=now.date(), settings=cache_settings
    )
    await repo.put_raw(
        raw_key,
        payload,
        provider=capabilities.provider_name,
        api_version=capabilities.api_version,
        origin=request.origin,
        destination=request.destination,
        departure_date=request.departure_date,
        now=now,
        ttl=ttl,
    )
    return payload


# ---------------------------------------------------------------------------
# First run misses, second identical run hits, zero provider calls
# ---------------------------------------------------------------------------


class TestFirstRunMissesSecondIdenticalRunHits:
    def test_second_identical_run_makes_zero_provider_calls(self) -> None:
        provider = InstrumentedProvider(scripts={_DESTINATION: [Succeed(offer_count=3)]})
        repo = _open_repo()
        request = _make_request()
        settings = _make_cache_settings()
        now = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)

        async def _scenario() -> tuple[str, str]:
            first = await _search_with_cache(
                provider, request, repo, now=now, cache_settings=settings
            )
            second = await _search_with_cache(
                provider,
                request,
                repo,
                now=now + timedelta(seconds=1),
                cache_settings=settings,
            )
            return first, second

        first_payload, second_payload = asyncio.run(_scenario())

        # Exactly one real provider call, ever -- the second identical
        # request is served entirely from the cache.
        assert provider.call_count(_DESTINATION) == 1
        assert repo.counters.raw_misses == 1
        assert repo.counters.raw_hits == 1
        assert repo.counters.raw_writes == 1
        assert first_payload == second_payload
        assert len(json.loads(second_payload)["offers"]) == 3

    def test_a_different_request_after_a_cached_one_still_calls_the_provider(self) -> None:
        """Sanity check on the harness itself: caching must not become a
        blanket "never call the provider again" -- a genuinely different
        request (different destination) still gets a real call.
        """
        provider = InstrumentedProvider(
            scripts={_DESTINATION: [Succeed(1)], "BOM": [Succeed(1)]}
        )
        repo = _open_repo()
        settings = _make_cache_settings()
        now = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)

        async def _scenario() -> None:
            await _search_with_cache(
                provider,
                _make_request(destination=_DESTINATION),
                repo,
                now=now,
                cache_settings=settings,
            )
            await _search_with_cache(
                provider,
                _make_request(destination="BOM"),
                repo,
                now=now,
                cache_settings=settings,
            )

        asyncio.run(_scenario())

        assert provider.call_count(_DESTINATION) == 1
        assert provider.call_count("BOM") == 1
        assert repo.counters.raw_misses == 2
        assert repo.counters.raw_hits == 0


# ---------------------------------------------------------------------------
# Cache key genuinely differs per field (max_stops called out explicitly,
# then every other raw_key field, parameterized)
# ---------------------------------------------------------------------------


class TestCacheKeyDiffersByMaxStops:
    def test_a_max_stops_0_entry_never_serves_a_max_stops_1_request(self) -> None:
        capabilities = _make_capabilities()
        repo = _open_repo()
        now = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)

        direct_request = _make_request(max_stops=0)
        one_stop_request = _make_request(max_stops=1)
        direct_key = compute_raw_key(direct_request, capabilities)
        one_stop_key = compute_raw_key(one_stop_request, capabilities)
        assert direct_key != one_stop_key  # keys must differ for the miss below to mean anything

        async def _scenario() -> str | None:
            await repo.put_raw(
                direct_key,
                "direct-only-payload",
                provider=capabilities.provider_name,
                api_version=capabilities.api_version,
                origin=direct_request.origin,
                destination=direct_request.destination,
                departure_date=direct_request.departure_date,
                now=now,
                ttl=timedelta(hours=1),
            )
            return await repo.get_raw(
                one_stop_key,
                now=now,
                provider=capabilities.provider_name,
                origin=one_stop_request.origin,
                destination=one_stop_request.destination,
            )

        assert asyncio.run(_scenario()) is None
        assert repo.counters.raw_misses == 1
        assert repo.counters.raw_hits == 0


class TestCacheKeyDiffersPerField:
    """Parameterized over every field the raw key is built from (master
    plan S5): origin, destination, departure_date, cabin, max_stops,
    adults, currency. Unlike ``tests/unit/test_cache.py``'s
    ``TestRawKeyDiffersPerField`` (which only asserts the two HASHES
    differ), this exercises the real ``CacheRepository``: an entry
    written for the baseline request must never be served to a request
    that differs in exactly one field.
    """

    _BASE_KWARGS: dict[str, object] = {
        "origin": _ORIGIN,
        "destination": _DESTINATION,
        "departure_date": _DEPARTURE_DATE,
        "cabin": CabinClass.ECONOMY,
        "max_stops": 1,
        "adults": 1,
        "currency": "EUR",
    }

    @pytest.mark.parametrize(
        ("field_name", "changed_value"),
        [
            ("origin", "RTM"),
            ("destination", "BOM"),
            ("departure_date", date(2027, 7, 18)),
            ("cabin", CabinClass.BUSINESS),
            ("max_stops", 0),
            ("adults", 2),
            ("currency", "USD"),
        ],
    )
    def test_an_entry_for_the_baseline_request_never_serves_a_changed_request(
        self, field_name: str, changed_value: object
    ) -> None:
        capabilities = _make_capabilities()
        repo = _open_repo()
        now = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)

        baseline_request = _make_request(**self._BASE_KWARGS)  # type: ignore[arg-type]
        changed_kwargs = dict(self._BASE_KWARGS)
        changed_kwargs[field_name] = changed_value
        changed_request = _make_request(**changed_kwargs)  # type: ignore[arg-type]

        baseline_key = compute_raw_key(baseline_request, capabilities)
        changed_key = compute_raw_key(changed_request, capabilities)
        assert baseline_key != changed_key

        async def _scenario() -> str | None:
            await repo.put_raw(
                baseline_key,
                "baseline-payload",
                provider=capabilities.provider_name,
                api_version=capabilities.api_version,
                origin=baseline_request.origin,
                destination=baseline_request.destination,
                departure_date=baseline_request.departure_date,
                now=now,
                ttl=timedelta(hours=1),
            )
            return await repo.get_raw(
                changed_key,
                now=now,
                provider=capabilities.provider_name,
                origin=changed_request.origin,
                destination=changed_request.destination,
            )

        assert asyncio.run(_scenario()) is None


# ---------------------------------------------------------------------------
# TTL expiry (injected clock) forces a real refetch
# ---------------------------------------------------------------------------


class TestTtlExpiryForcesARefetch:
    def test_advancing_past_the_ttl_boundary_causes_a_second_real_provider_call(self) -> None:
        provider = InstrumentedProvider(
            scripts={_DESTINATION: [Succeed(1), Succeed(1)]}
        )
        repo = _open_repo()
        request = _make_request()  # departure_date=2027-07-17, far over-180-day tier
        settings = _make_cache_settings(over_180=15)  # 15-minute TTL for this tier
        written_at = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
        just_before_expiry = written_at + timedelta(minutes=14, seconds=59)
        after_expiry = written_at + timedelta(minutes=15)

        async def _scenario() -> None:
            await _search_with_cache(
                provider, request, repo, now=written_at, cache_settings=settings
            )
            await _search_with_cache(
                provider, request, repo, now=just_before_expiry, cache_settings=settings
            )
            await _search_with_cache(
                provider, request, repo, now=after_expiry, cache_settings=settings
            )

        asyncio.run(_scenario())

        # Call 1: cold miss, real fetch. Call 2: within TTL, served from
        # cache. Call 3: past the TTL boundary, a genuine second fetch.
        assert provider.call_count(_DESTINATION) == 2
        assert repo.counters.raw_hits == 1
        assert repo.counters.raw_misses == 2
        assert repo.counters.raw_writes == 2

    def test_a_frozen_clock_within_ttl_never_triggers_a_refetch(self) -> None:
        """Complement of the above: repeated lookups at a FIXED clock
        value (never advancing) stay hits indefinitely -- expiry is driven
        purely by the injected ``now``, never by wall-clock time actually
        elapsing during the test.
        """
        provider = InstrumentedProvider(scripts={_DESTINATION: [Succeed(1)]})
        repo = _open_repo()
        request = _make_request()
        settings = _make_cache_settings(over_180=15)
        frozen_now = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)

        async def _scenario() -> None:
            for _ in range(5):
                await _search_with_cache(
                    provider, request, repo, now=frozen_now, cache_settings=settings
                )

        asyncio.run(_scenario())

        assert provider.call_count(_DESTINATION) == 1
        assert repo.counters.raw_hits == 4
        assert repo.counters.raw_misses == 1


# ---------------------------------------------------------------------------
# Corrupted / malformed cache rows: evicted, treated as a miss, never a crash
# ---------------------------------------------------------------------------


def _insert_raw_row_directly(
    connection: sqlite3.Connection, *, raw_key: str, expires_at: str
) -> None:
    """Bypasses ``CacheRepository.put_raw`` entirely -- writes a row
    straight through the connection so a malformed ``expires_at`` (the
    only field ``get_raw`` actually parses/interprets) can be injected the
    way real-world corruption would arrive: a row already sitting in the
    table that ``CacheRepository`` itself never wrote in that shape.
    """
    connection.execute(
        """
        INSERT INTO raw_payloads
            (raw_key, provider, api_version, origin, destination,
             departure_date, payload, retrieved_at, expires_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            raw_key,
            "instrumented",
            "instrumented-v1",
            _ORIGIN,
            _DESTINATION,
            _DEPARTURE_DATE.isoformat(),
            "stale-payload",
            "2026-08-16T12:00:00+00:00",
            expires_at,
            "2026-08-16T12:00:00+00:00",
        ),
    )
    connection.commit()


class TestCorruptedRawRowIsEvictedAndTreatedAsAMiss:
    def test_unparseable_expires_at_is_a_miss_not_a_crash(self) -> None:
        connection = connect(":memory:")
        repo = CacheRepository(connection)
        raw_key = "corrupt-raw-key"
        _insert_raw_row_directly(connection, raw_key=raw_key, expires_at="not-a-real-timestamp")

        # The point of this assertion is that this call returns instead of
        # raising -- a bare `asyncio.run(...)` here would propagate any
        # exception `get_raw` raised straight out of the test.
        result = asyncio.run(
            repo.get_raw(
                raw_key,
                now=datetime(2026, 8, 16, 12, 0, tzinfo=UTC),
                provider="instrumented",
                origin=_ORIGIN,
                destination=_DESTINATION,
            )
        )

        assert result is None
        assert repo.counters.raw_misses == 1
        assert repo.counters.raw_hits == 0

    def test_the_corrupted_row_is_actually_evicted_from_the_table(self) -> None:
        connection = connect(":memory:")
        repo = CacheRepository(connection)
        raw_key = "corrupt-raw-key-2"
        _insert_raw_row_directly(connection, raw_key=raw_key, expires_at="garbage")

        asyncio.run(
            repo.get_raw(
                raw_key,
                now=datetime(2026, 8, 16, 12, 0, tzinfo=UTC),
                provider="instrumented",
                origin=_ORIGIN,
                destination=_DESTINATION,
            )
        )

        row_count = connection.execute(
            "SELECT COUNT(*) FROM raw_payloads WHERE raw_key = ?", (raw_key,)
        ).fetchone()[0]
        assert row_count == 0  # evicted, not merely skipped over

    def test_the_cache_recovers_normally_after_evicting_a_corrupted_row(self) -> None:
        """Eviction must actually clear the way for normal service to
        resume -- a fresh ``put_raw`` for the same key afterward must
        succeed and roundtrip, not collide with a half-cleaned-up row.
        """
        connection = connect(":memory:")
        repo = CacheRepository(connection)
        raw_key = "corrupt-raw-key-3"
        _insert_raw_row_directly(connection, raw_key=raw_key, expires_at="also-garbage")
        now = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)

        async def _scenario() -> str | None:
            miss = await repo.get_raw(
                raw_key, now=now, provider="instrumented", origin=_ORIGIN, destination=_DESTINATION
            )
            assert miss is None
            await repo.put_raw(
                raw_key,
                "fresh-payload",
                provider="instrumented",
                api_version="instrumented-v1",
                origin=_ORIGIN,
                destination=_DESTINATION,
                departure_date=_DEPARTURE_DATE,
                now=now,
                ttl=timedelta(hours=1),
            )
            return await repo.get_raw(
                raw_key, now=now, provider="instrumented", origin=_ORIGIN, destination=_DESTINATION
            )

        assert asyncio.run(_scenario()) == "fresh-payload"


class TestCorruptedNormalizedRowIsEvictedAndTreatedAsAMiss:
    """``get_normalized``'s exact counterpart to the raw-layer tests
    above -- same guard, same table shape, different table.
    """

    def test_unparseable_expires_at_is_a_miss_not_a_crash(self) -> None:
        connection = connect(":memory:")
        repo = CacheRepository(connection)
        normalized_key = "corrupt-normalized-key"
        connection.execute(
            """
            INSERT INTO normalized_offers
                (normalized_key, raw_key, normalizer_version, fx_source_id,
                 payload, retrieved_at, expires_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalized_key,
                "some-raw-key",
                "n1",
                "ecb-2026",
                "stale-payload",
                "2026-08-16T12:00:00+00:00",
                "not-a-real-timestamp",
                "2026-08-16T12:00:00+00:00",
            ),
        )
        connection.commit()

        result = asyncio.run(
            repo.get_normalized(
                normalized_key,
                now=datetime(2026, 8, 16, 12, 0, tzinfo=UTC),
                provider="instrumented",
                origin=_ORIGIN,
                destination=_DESTINATION,
            )
        )

        assert result is None
        assert repo.counters.normalized_misses == 1
        assert repo.counters.normalized_hits == 0

        row_count = connection.execute(
            "SELECT COUNT(*) FROM normalized_offers WHERE normalized_key = ?", (normalized_key,)
        ).fetchone()[0]
        assert row_count == 0  # evicted here too


# ---------------------------------------------------------------------------
# Cache-disabled mode: T43 built no such toggle -- documented, not faked
# ---------------------------------------------------------------------------


class TestCacheDisabledMode:
    """T44 asks: does a cache-disabled mode bypass both reads and writes?
    Checked all three places such a toggle could plausibly live:

    - ``CacheSettings`` (``config/models.py``): only the four TTL tiers
      plus ``max_pages_per_task``, ``extra="forbid"`` -- no enabled flag.
    - ``cli.py``: zero mentions of "cache" anywhere (grepped case-
      insensitively) -- no ``--no-cache``/``--cache`` flag exists.
    - ``CacheRepository``: no constructor or method takes an
      enabled/disabled flag; there is also no call site anywhere in
      ``orchestration/executor.py`` that would otherwise consult the
      cache unconditionally (see this module's own docstring) for a
      toggle to bypass.

    T43 genuinely did not build this. Skipped, not faked with a toggle
    invented for the test's own sake -- per T44's own instruction ("if
    T43 built one, check").
    """

    @pytest.mark.skip(
        reason=(
            "No cache-enabled/disabled setting exists on CacheSettings, cli.py, "
            "or CacheRepository as of T43 -- nothing to bypass yet."
        )
    )
    def test_cache_disabled_mode_bypasses_reads_and_writes(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Concurrent identical requests: real behavior is NOT single-flighted
# ---------------------------------------------------------------------------


class TestConcurrentIdenticalRequestsAreNotSingleFlighted:
    """``CacheRepository`` (T43) implements no in-flight de-duplication
    anywhere -- no ``asyncio.Lock``, no in-flight-key tracking dict, no
    coordination of any kind beyond what SQLite's own connection mutex
    provides for data-safety (see ``persistence/db.py``'s own docstring on
    ``check_same_thread=False``). Per T44's own instruction ("check what
    T43 actually built ... do not assume single-flight if it was not
    actually implemented"), this documents and proves the REAL, current
    behavior: two concurrent identical requests against a cold cache both
    independently miss and both independently call the provider. Duplicate
    concurrent fetches on a cold cache are the accepted current behavior,
    not a regression this test is trying to catch -- a future task that
    adds single-flighting would need to update this test alongside it.
    """

    def test_two_concurrent_identical_cold_requests_both_call_the_provider(self) -> None:
        # call_delay_seconds > 0 is required to observe real overlap here
        # -- see InstrumentedProvider's own module docstring (T28 note):
        # without an await point inside the call, one task runs to
        # completion atomically before the event loop ever switches to
        # its sibling, and the "concurrency" being tested wouldn't exist.
        provider = InstrumentedProvider(
            scripts={_DESTINATION: [Succeed(1), Succeed(1)]}, call_delay_seconds=0.05
        )
        repo = _open_repo()
        request = _make_request()
        settings = _make_cache_settings()
        now = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)

        async def _scenario() -> None:
            await asyncio.gather(
                _search_with_cache(provider, request, repo, now=now, cache_settings=settings),
                _search_with_cache(provider, request, repo, now=now, cache_settings=settings),
            )

        asyncio.run(_scenario())

        # Real, current behavior: BOTH concurrent lookups miss (neither
        # sees the other's still-in-flight write), so both genuinely call
        # the provider -- no single-flighting exists to collapse them.
        assert provider.call_count(_DESTINATION) == 2
        assert repo.counters.raw_misses == 2
        assert repo.counters.raw_writes == 2
        assert provider.peak_in_flight == 2  # the two calls actually overlapped
