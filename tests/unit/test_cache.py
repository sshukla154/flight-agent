"""Tests for the SQLite two-layer cache (T43): schema, key derivation,
TTL tiering, and hit/miss/write counters (master plan S5 "Caching:
SQLite, not DuckDB").

Async calls are driven with plain ``asyncio.run(...)`` from ordinary
synchronous test functions -- this codebase has no ``pytest-asyncio``
dependency (see ``tests/unit/test_retry.py``'s own
``asyncio.run(execute_plan(...))`` calls), so this file follows that same
convention rather than introducing a second one.

Clock injection follows this codebase's own convention (see
``cache_repo``'s own docstring): every time-sensitive call here passes an
explicit ``now``/``as_of`` value, never relies on the real wall clock, so
every test is exactly as deterministic as the pipeline it is testing.
"""

from __future__ import annotations

import asyncio
import io
import json
from datetime import UTC, date, datetime, timedelta

import pytest

from flightagent.config.models import CacheSettings
from flightagent.domain.enums import CabinClass
from flightagent.domain.run import SearchRequest
from flightagent.observability.context import run_context
from flightagent.observability.logging import setup_logging
from flightagent.persistence.cache_repo import CacheRepository, resolve_ttl
from flightagent.persistence.db import connect
from flightagent.persistence.keys import compute_normalized_key, compute_raw_key
from flightagent.providers.base import ProviderCapabilities

# ---------------------------------------------------------------------------
# Shared fixtures / builders
# ---------------------------------------------------------------------------


def _make_request(
    *,
    origin: str = "AMS",
    destination: str = "DEL",
    departure_date: date = date(2027, 7, 17),
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
    *, provider_name: str = "mock", api_version: str = "v1"
) -> ProviderCapabilities:
    return ProviderCapabilities(
        provider_name=provider_name,
        api_version=api_version,
        auth_style="none",
        paginated=False,
        native_currency_forceable=True,
        returns_booking_url=True,
        stop_filter_style="nonstop_boolean",
    )


def _make_cache_settings(
    *,
    over_180: int = 1000,
    thirty_to_180: int = 500,
    seven_to_30: int = 200,
    under_7: int = 50,
) -> CacheSettings:
    """Deliberately distinct, non-production-default minute values (not
    ``config/defaults.toml``'s real 1440/360/60/15) so a boundary test
    failure can only mean the tiering LOGIC picked the wrong tier, never a
    coincidence of two tiers sharing a value.
    """
    return CacheSettings(
        ttl_minutes_over_180_days=over_180,
        ttl_minutes_30_to_180_days=thirty_to_180,
        ttl_minutes_7_to_30_days=seven_to_30,
        ttl_minutes_under_7_days=under_7,
        max_pages_per_task=3,
        db_path="cache/flightagent.sqlite3",
    )


@pytest.fixture
def repo() -> CacheRepository:
    connection = connect(":memory:")
    return CacheRepository(connection)


_DEFAULT_RAW_WRITE_KWARGS: dict[str, object] = {
    "provider": "mock",
    "api_version": "v1",
    "origin": "AMS",
    "destination": "DEL",
    "departure_date": date(2027, 7, 17),
}


# ---------------------------------------------------------------------------
# db.connect: schema application
# ---------------------------------------------------------------------------


class TestSchemaApplication:
    def test_connect_creates_both_tables(self) -> None:
        connection = connect(":memory:")
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"raw_payloads", "normalized_offers"} <= tables

    def test_connect_is_idempotent_on_an_already_migrated_file(self, tmp_path: object) -> None:
        db_path = f"{tmp_path}/cache.sqlite3"
        first = connect(db_path)
        first.close()
        second = connect(db_path)  # must not raise on re-applying schema.sql
        tables = {
            row[0]
            for row in second.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"raw_payloads", "normalized_offers"} <= tables
        second.close()

    def test_normalized_offers_raw_key_foreign_key_column_exists(self) -> None:
        connection = connect(":memory:")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(normalized_offers)")}
        assert "raw_key" in columns


# ---------------------------------------------------------------------------
# Key derivation: stability
# ---------------------------------------------------------------------------


class TestRawKeyStability:
    def test_stable_across_identical_logical_inputs(self) -> None:
        capabilities = _make_capabilities()
        request_a = _make_request()
        request_b = SearchRequest(
            # Same values, deliberately constructed with kwargs in a
            # different order than `_make_request`'s own literal order --
            # the resulting SearchRequest is logically identical either
            # way, and the key must not care how it was built.
            currency="EUR",
            adults=1,
            max_stops=1,
            cabin=CabinClass.ECONOMY,
            departure_date=date(2027, 7, 17),
            destination="DEL",
            origin="AMS",
            layover_max=timedelta(minutes=360),
            layover_min=timedelta(minutes=180),
        )
        assert compute_raw_key(request_a, capabilities) == compute_raw_key(request_b, capabilities)

    def test_stable_across_repeated_calls(self) -> None:
        capabilities = _make_capabilities()
        request = _make_request()
        first = compute_raw_key(request, capabilities)
        second = compute_raw_key(request, capabilities)
        assert first == second


class TestRawKeyDiffersPerField:
    """Parametrized over master plan S5's own raw_key field list: origin,
    destination, departure_date, cabin, max_stops, adults, currency.
    Changing exactly one field must change the key -- a collision here
    would mean two logically different searches share a cache entry.
    """

    _BASE_KWARGS: dict[str, object] = {
        "origin": "AMS",
        "destination": "DEL",
        "departure_date": date(2027, 7, 17),
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
    def test_changing_one_field_changes_the_key(
        self, field_name: str, changed_value: object
    ) -> None:
        capabilities = _make_capabilities()
        baseline = _make_request(**self._BASE_KWARGS)  # type: ignore[arg-type]
        changed_kwargs = dict(self._BASE_KWARGS)
        changed_kwargs[field_name] = changed_value
        changed = _make_request(**changed_kwargs)  # type: ignore[arg-type]

        baseline_key = compute_raw_key(baseline, capabilities)
        changed_key = compute_raw_key(changed, capabilities)

        assert baseline_key != changed_key

    def test_changing_provider_identity_changes_the_key(self) -> None:
        request = _make_request()
        key_mock = compute_raw_key(request, _make_capabilities(provider_name="mock"))
        key_amadeus = compute_raw_key(request, _make_capabilities(provider_name="amadeus"))
        assert key_mock != key_amadeus

    def test_changing_api_version_changes_the_key(self) -> None:
        request = _make_request()
        key_v1 = compute_raw_key(request, _make_capabilities(api_version="v1"))
        key_v2 = compute_raw_key(request, _make_capabilities(api_version="v2"))
        assert key_v1 != key_v2


class TestNormalizedKeyDerivation:
    def test_stable_across_repeated_calls(self) -> None:
        raw_key = compute_raw_key(_make_request(), _make_capabilities())
        first = compute_normalized_key(raw_key, normalizer_version="n1", fx_source_id="ecb-2026")
        second = compute_normalized_key(raw_key, normalizer_version="n1", fx_source_id="ecb-2026")
        assert first == second

    def test_differs_when_raw_key_differs(self) -> None:
        raw_key_a = compute_raw_key(_make_request(origin="AMS"), _make_capabilities())
        raw_key_b = compute_raw_key(_make_request(origin="RTM"), _make_capabilities())
        key_a = compute_normalized_key(raw_key_a, normalizer_version="n1", fx_source_id="ecb-2026")
        key_b = compute_normalized_key(raw_key_b, normalizer_version="n1", fx_source_id="ecb-2026")
        assert key_a != key_b

    def test_differs_when_normalizer_version_differs(self) -> None:
        raw_key = compute_raw_key(_make_request(), _make_capabilities())
        key_n1 = compute_normalized_key(raw_key, normalizer_version="n1", fx_source_id="ecb-2026")
        key_n2 = compute_normalized_key(raw_key, normalizer_version="n2", fx_source_id="ecb-2026")
        assert key_n1 != key_n2

    def test_differs_when_fx_source_id_differs(self) -> None:
        raw_key = compute_raw_key(_make_request(), _make_capabilities())
        key_ecb = compute_normalized_key(raw_key, normalizer_version="n1", fx_source_id="ecb-2026")
        key_xe = compute_normalized_key(raw_key, normalizer_version="n1", fx_source_id="xe-2026")
        assert key_ecb != key_xe

    def test_a_mapper_bugfix_reuses_the_same_raw_key_with_a_new_normalized_key(self) -> None:
        """Master plan S5's own stated payoff of the two-table split: a
        normalizer-version bump changes ``normalized_key`` but never
        touches ``raw_key`` -- the cached raw bytes are still addressable
        by the SAME raw_key after the bugfix.
        """
        request = _make_request()
        capabilities = _make_capabilities()
        raw_key_before = compute_raw_key(request, capabilities)
        raw_key_after = compute_raw_key(request, capabilities)
        assert raw_key_before == raw_key_after

        key_before_fix = compute_normalized_key(
            raw_key_before, normalizer_version="n1", fx_source_id="ecb-2026"
        )
        key_after_fix = compute_normalized_key(
            raw_key_after, normalizer_version="n2", fx_source_id="ecb-2026"
        )
        assert key_before_fix != key_after_fix


# ---------------------------------------------------------------------------
# CacheRepository: read/write roundtrip
# ---------------------------------------------------------------------------


class TestRawLayerRoundtrip:
    def test_write_then_read_returns_identical_payload(self, repo: CacheRepository) -> None:
        raw_key = compute_raw_key(_make_request(), _make_capabilities())
        now = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
        payload = json.dumps({"offers": ["a", "b", "c"]})

        async def _scenario() -> str | None:
            await repo.put_raw(
                raw_key, payload, now=now, ttl=timedelta(hours=24), **_DEFAULT_RAW_WRITE_KWARGS  # type: ignore[arg-type]
            )
            return await repo.get_raw(
                raw_key,
                now=now + timedelta(minutes=1),
                provider="mock",
                origin="AMS",
                destination="DEL",
            )

        assert asyncio.run(_scenario()) == payload

    def test_miss_for_unknown_key(self, repo: CacheRepository) -> None:
        result = asyncio.run(
            repo.get_raw(
                "does-not-exist",
                now=datetime.now(UTC),
                provider="mock",
                origin="AMS",
                destination="DEL",
            )
        )
        assert result is None

    def test_put_is_idempotent_upsert_not_a_duplicate_row(self, repo: CacheRepository) -> None:
        raw_key = compute_raw_key(_make_request(), _make_capabilities())
        now = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)

        async def _scenario() -> str | None:
            await repo.put_raw(
                raw_key,
                "first-payload",
                now=now,
                ttl=timedelta(hours=24),
                **_DEFAULT_RAW_WRITE_KWARGS,  # type: ignore[arg-type]
            )
            await repo.put_raw(
                raw_key,
                "second-payload",
                now=now,
                ttl=timedelta(hours=24),
                **_DEFAULT_RAW_WRITE_KWARGS,  # type: ignore[arg-type]
            )
            return await repo.get_raw(
                raw_key, now=now, provider="mock", origin="AMS", destination="DEL"
            )

        result = asyncio.run(_scenario())

        row_count = repo._connection.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM raw_payloads WHERE raw_key = ?", (raw_key,)
        ).fetchone()[0]
        assert row_count == 1
        assert result == "second-payload"


class TestNormalizedLayerRoundtrip:
    def test_write_then_read_returns_identical_payload(self, repo: CacheRepository) -> None:
        raw_key = compute_raw_key(_make_request(), _make_capabilities())
        normalized_key = compute_normalized_key(
            raw_key, normalizer_version="n1", fx_source_id="ecb-2026"
        )
        now = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
        payload = json.dumps({"itineraries": ["x"]})

        async def _scenario() -> str | None:
            await repo.put_normalized(
                normalized_key,
                raw_key,
                payload,
                normalizer_version="n1",
                fx_source_id="ecb-2026",
                provider="mock",
                origin="AMS",
                destination="DEL",
                now=now,
                ttl=timedelta(hours=6),
            )
            return await repo.get_normalized(
                normalized_key,
                now=now + timedelta(minutes=1),
                provider="mock",
                origin="AMS",
                destination="DEL",
            )

        assert asyncio.run(_scenario()) == payload

    def test_miss_for_unknown_key(self, repo: CacheRepository) -> None:
        result = asyncio.run(
            repo.get_normalized(
                "does-not-exist",
                now=datetime.now(UTC),
                provider="mock",
                origin="AMS",
                destination="DEL",
            )
        )
        assert result is None


# ---------------------------------------------------------------------------
# TTL expiry (injectable clock)
# ---------------------------------------------------------------------------


class TestTtlExpiry:
    def test_entry_within_ttl_is_a_hit(self, repo: CacheRepository) -> None:
        raw_key = compute_raw_key(_make_request(), _make_capabilities())
        written_at = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
        just_before_expiry = written_at + timedelta(minutes=14, seconds=59)

        async def _scenario() -> str | None:
            await repo.put_raw(
                raw_key,
                "payload",
                now=written_at,
                ttl=timedelta(minutes=15),
                **_DEFAULT_RAW_WRITE_KWARGS,  # type: ignore[arg-type]
            )
            return await repo.get_raw(
                raw_key,
                now=just_before_expiry,
                provider="mock",
                origin="AMS",
                destination="DEL",
            )

        assert asyncio.run(_scenario()) == "payload"

    def test_entry_past_ttl_is_treated_as_a_miss(self, repo: CacheRepository) -> None:
        raw_key = compute_raw_key(_make_request(), _make_capabilities())
        written_at = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
        after_expiry = written_at + timedelta(minutes=15)

        async def _scenario() -> str | None:
            await repo.put_raw(
                raw_key,
                "payload",
                now=written_at,
                ttl=timedelta(minutes=15),
                **_DEFAULT_RAW_WRITE_KWARGS,  # type: ignore[arg-type]
            )
            return await repo.get_raw(
                raw_key, now=after_expiry, provider="mock", origin="AMS", destination="DEL"
            )

        assert asyncio.run(_scenario()) is None

    def test_entry_well_past_ttl_is_a_miss(self, repo: CacheRepository) -> None:
        raw_key = compute_raw_key(_make_request(), _make_capabilities())
        written_at = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
        long_after = written_at + timedelta(days=2)

        async def _scenario() -> str | None:
            await repo.put_raw(
                raw_key,
                "payload",
                now=written_at,
                ttl=timedelta(minutes=15),
                **_DEFAULT_RAW_WRITE_KWARGS,  # type: ignore[arg-type]
            )
            return await repo.get_raw(
                raw_key, now=long_after, provider="mock", origin="AMS", destination="DEL"
            )

        assert asyncio.run(_scenario()) is None


# ---------------------------------------------------------------------------
# TTL tier boundaries (resolve_ttl)
# ---------------------------------------------------------------------------


class TestResolveTtlTierBoundaries:
    """Master plan S5: >180d -> 24h, 30-180d -> 6h, 7-30d -> 1h, <7d ->
    15min, read here as lower-inclusive half-open (matching D9's own
    penalty-band convention) -- see ``resolve_ttl``'s docstring for the
    exact partition. ``_make_cache_settings`` uses distinct
    non-production values so each assertion pins down a specific tier, not
    a coincidence.
    """

    _SETTINGS = _make_cache_settings(
        over_180=1000, thirty_to_180=500, seven_to_30=200, under_7=50
    )
    _AS_OF = date(2026, 8, 16)

    def _departure(self, days_out: int) -> date:
        return self._AS_OF + timedelta(days=days_out)

    @pytest.mark.parametrize(
        ("days_out", "expected_minutes"),
        [
            (365, 1000),  # far future, deep in the over-180 tier
            (181, 1000),  # just over the 180 boundary -> over-180 tier
            (180, 500),  # exactly 180 -> the 30-180 tier (lower-inclusive top)
            (179, 500),  # just under 180 -> still 30-180
            (31, 500),  # still inside 30-180
            (30, 500),  # exactly 30 -> 30-180 tier (lower boundary, inclusive)
            (29, 200),  # just under 30 -> 7-30 tier
            (8, 200),  # inside 7-30
            (7, 200),  # exactly 7 -> 7-30 tier (lower boundary, inclusive)
            (6, 50),  # just under 7 -> under-7 tier
            (1, 50),  # inside under-7
            (0, 50),  # departing today -> under-7 tier
            (-5, 50),  # a departure date already in the past -> under-7 tier
        ],
    )
    def test_tier_boundary(self, days_out: int, expected_minutes: int) -> None:
        ttl = resolve_ttl(
            departure_date=self._departure(days_out), as_of=self._AS_OF, settings=self._SETTINGS
        )
        assert ttl == timedelta(minutes=expected_minutes)

    def test_this_projects_actual_departure_date_lands_in_the_over_180_tier(self) -> None:
        """Sanity check against the project's own real numbers: 2027-07-17
        seen from 2026-08-16 is exactly 335 days out -- comfortably in the
        over-180 tier, matching the master plan's own worked example.
        """
        ttl = resolve_ttl(
            departure_date=date(2027, 7, 17),
            as_of=date(2026, 8, 16),
            settings=self._SETTINGS,
        )
        assert ttl == timedelta(minutes=1000)


# ---------------------------------------------------------------------------
# Hit/miss/write counters
# ---------------------------------------------------------------------------


class TestCacheCounters:
    def test_miss_increments_raw_misses_only(self, repo: CacheRepository) -> None:
        asyncio.run(
            repo.get_raw(
                "missing", now=datetime.now(UTC), provider="mock", origin="AMS", destination="DEL"
            )
        )
        assert repo.counters.raw_misses == 1
        assert repo.counters.raw_hits == 0
        assert repo.counters.raw_writes == 0

    def test_write_then_hit_increments_writes_and_hits(self, repo: CacheRepository) -> None:
        raw_key = compute_raw_key(_make_request(), _make_capabilities())
        now = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)

        async def _scenario() -> None:
            await repo.put_raw(
                raw_key,
                "payload",
                now=now,
                ttl=timedelta(hours=1),
                **_DEFAULT_RAW_WRITE_KWARGS,  # type: ignore[arg-type]
            )
            await repo.get_raw(raw_key, now=now, provider="mock", origin="AMS", destination="DEL")

        asyncio.run(_scenario())

        assert repo.counters.raw_writes == 1
        assert repo.counters.raw_hits == 1
        assert repo.counters.raw_misses == 0

    def test_expired_entry_counts_as_a_miss_not_a_hit(self, repo: CacheRepository) -> None:
        raw_key = compute_raw_key(_make_request(), _make_capabilities())
        now = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)

        async def _scenario() -> None:
            await repo.put_raw(
                raw_key,
                "payload",
                now=now,
                ttl=timedelta(minutes=1),
                **_DEFAULT_RAW_WRITE_KWARGS,  # type: ignore[arg-type]
            )
            await repo.get_raw(
                raw_key,
                now=now + timedelta(hours=1),
                provider="mock",
                origin="AMS",
                destination="DEL",
            )

        asyncio.run(_scenario())
        assert repo.counters.raw_misses == 1
        assert repo.counters.raw_hits == 0

    def test_raw_and_normalized_counters_are_independent(self, repo: CacheRepository) -> None:
        raw_key = compute_raw_key(_make_request(), _make_capabilities())
        normalized_key = compute_normalized_key(
            raw_key, normalizer_version="n1", fx_source_id="ecb-2026"
        )
        now = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)

        async def _scenario() -> None:
            await repo.get_normalized(
                normalized_key, now=now, provider="mock", origin="AMS", destination="DEL"
            )
            await repo.put_raw(
                raw_key,
                "payload",
                now=now,
                ttl=timedelta(hours=1),
                **_DEFAULT_RAW_WRITE_KWARGS,  # type: ignore[arg-type]
            )

        asyncio.run(_scenario())

        assert repo.counters.normalized_misses == 1
        assert repo.counters.raw_misses == 0
        assert repo.counters.raw_writes == 1
        assert repo.counters.normalized_writes == 0


# ---------------------------------------------------------------------------
# Observability: hit/miss/write log events
# ---------------------------------------------------------------------------


class TestCacheEventLogging:
    def test_miss_emits_cache_miss_event(self, repo: CacheRepository) -> None:
        stream = io.StringIO()
        setup_logging(stream=stream)

        with run_context("test-run"):
            asyncio.run(
                repo.get_raw(
                    "missing",
                    now=datetime.now(UTC),
                    provider="mock",
                    origin="AMS",
                    destination="DEL",
                )
            )

        lines = stream.getvalue().strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["event"] == "cache.miss"
        assert record["provider"] == "mock"
        assert record["origin"] == "AMS"
        assert record["destination"] == "DEL"
        assert record["cache_key"] == "missing"

    def test_hit_emits_cache_hit_event(self, repo: CacheRepository) -> None:
        raw_key = compute_raw_key(_make_request(), _make_capabilities())
        now = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
        asyncio.run(
            repo.put_raw(
                raw_key,
                "payload",
                now=now,
                ttl=timedelta(hours=1),
                **_DEFAULT_RAW_WRITE_KWARGS,  # type: ignore[arg-type]
            )
        )

        stream = io.StringIO()
        setup_logging(stream=stream)
        with run_context("test-run"):
            asyncio.run(
                repo.get_raw(raw_key, now=now, provider="mock", origin="AMS", destination="DEL")
            )

        lines = stream.getvalue().strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["event"] == "cache.hit"
        assert record["cache_key"] == raw_key

    def test_write_emits_cache_write_event_with_ttl_seconds(self, repo: CacheRepository) -> None:
        raw_key = compute_raw_key(_make_request(), _make_capabilities())
        stream = io.StringIO()
        setup_logging(stream=stream)

        with run_context("test-run"):
            asyncio.run(
                repo.put_raw(
                    raw_key,
                    "payload",
                    now=datetime(2026, 8, 16, 12, 0, tzinfo=UTC),
                    ttl=timedelta(hours=1),
                    **_DEFAULT_RAW_WRITE_KWARGS,  # type: ignore[arg-type]
                )
            )

        lines = stream.getvalue().strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["event"] == "cache.write"
        assert record["ttl_seconds"] == 3600
        assert record["cache_key"] == raw_key
