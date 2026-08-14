"""Unit tests for flightagent.airports (Phase 1 / T8).

``test_origins_and_destinations_counts`` is the literal Phase 1 exit
criterion (``len(origins()) == 10 and len(destinations()) == 8``).
``test_origins_are_in_exact_priority_order`` proves the search call log's
priority order, not merely the count — DECISIONS.md/Addendum 2 and
several later acceptance tests depend on this exact ordering.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from flightagent.airports import registry
from flightagent.airports.registry import (
    Airport,
    AirportRegistry,
    AirportRegistryError,
    UnknownAirportError,
)
from flightagent.tools.airport_info import airport_info

_EXPECTED_ORIGIN_ORDER = ["AMS", "EIN", "RTM", "DUS", "BRU", "NRN", "CGN", "CRL", "MST", "GRQ"]
_EXPECTED_DESTINATIONS = {"DEL", "BOM", "BLR", "HYD", "MAA", "CCU", "LKO", "VNS"}


def test_origins_and_destinations_counts() -> None:
    """The literal Phase 1 exit criterion."""
    assert len(registry.origins()) == 10
    assert len(registry.destinations()) == 8


def test_origins_are_in_exact_priority_order() -> None:
    """Proves priority order, not just count (Addendum 2)."""
    assert [airport.iata for airport in registry.origins()] == _EXPECTED_ORIGIN_ORDER


def test_destination_iata_codes_match_expected_set() -> None:
    assert {airport.iata for airport in registry.destinations()} == _EXPECTED_DESTINATIONS


def test_origins_carry_ground_data_and_priority() -> None:
    for expected_priority, airport in enumerate(registry.origins(), start=1):
        assert airport.is_origin
        assert airport.ground is not None
        assert airport.priority == expected_priority
        assert airport.ground.mode in {"car", "train", "either"}
        assert airport.ground.source == "estimate"


def test_destinations_carry_no_ground_data() -> None:
    for airport in registry.destinations():
        assert not airport.is_origin
        assert airport.ground is None
        assert airport.priority is None


def test_all_airports_have_populated_reference_fields() -> None:
    for airport in registry.origins() + registry.destinations():
        assert airport.name
        assert airport.city
        assert airport.country
        assert airport.iana_tz
        assert -90.0 <= airport.lat <= 90.0
        assert -180.0 <= airport.lon <= 180.0


def test_airport_info_returns_city_and_country_for_ams() -> None:
    result = airport_info("AMS")
    assert result.city == "Amsterdam"
    assert result.country == "Netherlands"


def test_airport_info_returns_city_and_country_for_del() -> None:
    result = airport_info("DEL")
    assert result.city == "Delhi"
    assert result.country == "India"


def test_airport_info_unknown_code_raises() -> None:
    with pytest.raises(UnknownAirportError):
        airport_info("ZZZ")


def test_registry_get_unknown_code_raises() -> None:
    with pytest.raises(UnknownAirportError, match="ZZZ"):
        registry.get("ZZZ")


def _write_override_catalogs(tmp_path: Path) -> tuple[Path, Path]:
    """A tiny two-origin/one-destination catalog pair, deliberately listing
    the origins in the OPPOSITE order to their priority in
    airports.yaml — this is what proves origins() ordering comes from
    ground_access.yaml's ``priority`` field, not from file order.
    """
    airports_file = tmp_path / "airports.yaml"
    airports_file.write_text(
        """
airports:
  - iata: XXA
    name: Test Airport A
    city: Test City A
    country: Test Country
    iana_tz: Europe/Amsterdam
    lat: 1.0
    lon: 2.0
  - iata: XXB
    name: Test Airport B
    city: Test City B
    country: Test Country
    iana_tz: Europe/Berlin
    lat: 3.0
    lon: 4.0
  - iata: XXD
    name: Test Destination
    city: Test Dest City
    country: Test Dest Country
    iana_tz: Asia/Kolkata
    lat: 5.0
    lon: 6.0
""",
        encoding="utf-8",
    )

    ground_access_file = tmp_path / "ground_access.yaml"
    ground_access_file.write_text(
        """
ground_access:
  - to_airport: XXB
    from_location: "Test Origin, NL"
    mode: car
    minutes: 30
    distance_km: 20
    cost_eur: 5.00
    source: estimate
    as_of: 2026-08-14
    priority: 1
    notes: "test note B"
  - to_airport: XXA
    from_location: "Test Origin, NL"
    mode: train
    minutes: 60
    distance_km: 40
    cost_eur: 8.00
    source: estimate
    as_of: 2026-08-14
    priority: 2
    notes: "test note A"
""",
        encoding="utf-8",
    )
    return airports_file, ground_access_file


def test_reload_with_override_paths_uses_override_data(tmp_path: Path) -> None:
    """The module-level singleton can be pointed at override catalog
    files for testing, and restores cleanly back to the real files
    afterward so later tests are unaffected.
    """
    airports_file, ground_access_file = _write_override_catalogs(tmp_path)
    try:
        registry.reload(airports_path=airports_file, ground_access_path=ground_access_file)

        assert [a.iata for a in registry.origins()] == ["XXB", "XXA"]
        assert [a.iata for a in registry.destinations()] == ["XXD"]
        assert registry.get("XXA").city == "Test City A"
    finally:
        registry.reload()  # restore the real config/ singleton

    assert len(registry.origins()) == 10
    assert len(registry.destinations()) == 8


def test_airport_registry_rejects_ground_access_row_for_unknown_airport(tmp_path: Path) -> None:
    airports_file = tmp_path / "airports.yaml"
    airports_file.write_text(
        "airports:\n"
        "  - iata: XXA\n"
        "    name: Test Airport A\n"
        "    city: Test City A\n"
        "    country: Test Country\n"
        "    iana_tz: Europe/Amsterdam\n"
        "    lat: 1.0\n"
        "    lon: 2.0\n",
        encoding="utf-8",
    )
    ground_access_file = tmp_path / "ground_access.yaml"
    ground_access_file.write_text(
        "ground_access:\n"
        "  - to_airport: XXZ\n"
        '    from_location: "Test Origin, NL"\n'
        "    mode: car\n"
        "    minutes: 30\n"
        "    distance_km: 20\n"
        "    cost_eur: 5.00\n"
        "    source: estimate\n"
        "    as_of: 2026-08-14\n"
        "    priority: 1\n",
        encoding="utf-8",
    )

    with pytest.raises(AirportRegistryError, match="XXZ"):
        AirportRegistry(airports_path=airports_file, ground_access_path=ground_access_file)


def test_airport_registry_rejects_duplicate_priority(tmp_path: Path) -> None:
    airports_file = tmp_path / "airports.yaml"
    airports_file.write_text(
        "airports:\n"
        "  - iata: XXA\n"
        "    name: Test Airport A\n"
        "    city: Test City A\n"
        "    country: Test Country\n"
        "    iana_tz: Europe/Amsterdam\n"
        "    lat: 1.0\n"
        "    lon: 2.0\n"
        "  - iata: XXB\n"
        "    name: Test Airport B\n"
        "    city: Test City B\n"
        "    country: Test Country\n"
        "    iana_tz: Europe/Berlin\n"
        "    lat: 3.0\n"
        "    lon: 4.0\n",
        encoding="utf-8",
    )
    ground_access_file = tmp_path / "ground_access.yaml"
    ground_access_file.write_text(
        "ground_access:\n"
        "  - to_airport: XXA\n"
        '    from_location: "Test Origin, NL"\n'
        "    mode: car\n"
        "    minutes: 30\n"
        "    distance_km: 20\n"
        "    cost_eur: 5.00\n"
        "    source: estimate\n"
        "    as_of: 2026-08-14\n"
        "    priority: 1\n"
        "  - to_airport: XXB\n"
        '    from_location: "Test Origin, NL"\n'
        "    mode: car\n"
        "    minutes: 40\n"
        "    distance_km: 25\n"
        "    cost_eur: 6.00\n"
        "    source: estimate\n"
        "    as_of: 2026-08-14\n"
        "    priority: 1\n",
        encoding="utf-8",
    )

    with pytest.raises(AirportRegistryError, match="priority"):
        AirportRegistry(airports_path=airports_file, ground_access_path=ground_access_file)


def test_airport_is_frozen() -> None:
    airport = registry.get("AMS")
    with pytest.raises(ValidationError):
        airport.city = "Somewhere else"  # type: ignore[misc]


def test_airport_model_direct_import_uses_domain_iata_code() -> None:
    """``Airport`` reuses ``flightagent.domain.airport.IataCode`` rather
    than redefining the pattern — an all-uppercase-3-letter code is
    still enforced even when constructing ``Airport`` directly.
    """
    with pytest.raises(ValidationError):
        Airport(
            iata="ams",  # lowercase -- must be rejected by the shared IataCode pattern
            name="x",
            city="x",
            country="x",
            iana_tz="Europe/Amsterdam",
            lat=0.0,
            lon=0.0,
        )
