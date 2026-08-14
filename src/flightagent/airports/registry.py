"""Airport registry — the merged view over ``config/airports.yaml``
(general reference data) and ``config/ground_access.yaml`` (Nieuwegein ->
origin ground-access data).

Master plan §3/§6 and DECISIONS.md deliberately keep these as TWO
files: ``airports.yaml`` is general reference data (name/city/country/
IANA tz/lat/lon) that would be unchanged no matter where the traveller
starts from, while ``ground_access.yaml`` is specific to this project's
home base (Nieuwegein, NL) and would be entirely different data under a
different home base. This module is where the two get joined for the
rest of the codebase to consume as one typed ``Airport`` record per
airport — origins carry a ``ground`` leg and a ``priority``, destinations
carry neither, because ground travel is only modelled at the European
end of the trip (master plan §6).

``Airport`` lives here rather than in ``flightagent.domain.airport``
specifically to avoid a circular import: ``domain.ground.GroundLeg``
already imports ``IataCode`` from ``domain.airport``, so a domain-level
``Airport`` model that also carried a ``GroundLeg`` field would create
``domain.airport -> domain.ground -> domain.airport``. Defining it in
this non-domain module instead — which is free to import both
``domain.airport`` and ``domain.ground`` — sidesteps that without
touching either module. Only ``IataCode`` is imported from
``domain.airport``, never redefined, per this task's explicit
instruction; ``GroundLeg`` is reused unchanged from ``domain.ground``
(T7), not re-specified here.

Loads both YAML catalogs through
``flightagent.config.catalogs.load_yaml_catalog`` (T5) exactly once —
this module does not implement a second YAML reader. The module-level
``origins()``/``destinations()``/``get()`` functions lazily build a
single cached ``AirportRegistry`` from the real ``config/`` files on
first use; ``reload()`` exists solely so tests can point the registry at
override paths (e.g. fixture files) instead of mutating the real
``config/`` directory — production code never needs to call it.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from flightagent.config.catalogs import load_yaml_catalog
from flightagent.config.loader import PACKAGED_DEFAULTS_PATH
from flightagent.domain.airport import IataCode
from flightagent.domain.ground import GroundLeg
from flightagent.domain.money import Money

# config/loader.py already knows where the real config/ directory is
# (PACKAGED_DEFAULTS_PATH = <repo_root>/config/defaults.toml); reusing
# its parent rather than re-deriving the repo root a second way keeps
# there being exactly one source of truth for "where config/ lives".
_CONFIG_DIR = PACKAGED_DEFAULTS_PATH.parent
DEFAULT_AIRPORTS_PATH = _CONFIG_DIR / "airports.yaml"
DEFAULT_GROUND_ACCESS_PATH = _CONFIG_DIR / "ground_access.yaml"


class AirportRegistryError(RuntimeError):
    """Raised for a structural problem in the catalogs' *content* — a
    malformed row, a duplicate IATA code, a ``ground_access.yaml`` row
    referencing an IATA code absent from ``airports.yaml``, or a
    ``priority`` collision. Distinct from
    ``config.catalogs.CatalogLoadError``, which is about the YAML file
    itself being unreadable, not about what it says once parsed.
    """


class UnknownAirportError(LookupError):
    """Raised when an IATA code is not present in either catalog.

    Never a silent ``None``/default — master plan §4 is explicit that a
    missing or wrong airport entry must fail the caller loudly, because
    it would otherwise silently corrupt every duration computed through
    that airport downstream. This is exactly what ``tools.airport_info``
    relies on to satisfy its "raise a clear error for an unknown IATA
    code" requirement.
    """


class Airport(BaseModel):
    """One merged airport record.

    Origins (present in ``config/ground_access.yaml``) carry ``ground``
    and ``priority``; destinations carry neither. ``priority`` has no
    upper bound on purpose — master plan §6 notes "someone will add an
    eleventh airport" as the reason the ground-travel limit exists as a
    real filter rather than a no-op; hardcoding a ``le=10`` here would
    quietly contradict that.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    iata: IataCode
    name: str
    city: str
    country: str
    iana_tz: str
    lat: float
    lon: float
    priority: int | None = Field(default=None, ge=1)
    ground: GroundLeg | None = None

    @property
    def is_origin(self) -> bool:
        """True for the 10 European origins, False for the 8 destinations."""
        return self.ground is not None


def _parse_airport_row(row: dict[str, Any]) -> dict[str, Any]:
    """One ``config/airports.yaml`` row -> the base ``Airport`` kwargs
    (``ground``/``priority`` are merged in separately by the caller)."""
    try:
        return {
            "iata": row["iata"],
            "name": row["name"],
            "city": row["city"],
            "country": row["country"],
            "iana_tz": row["iana_tz"],
            "lat": row["lat"],
            "lon": row["lon"],
        }
    except KeyError as exc:
        raise AirportRegistryError(
            f"config/airports.yaml row is missing required key {exc}: {row!r}"
        ) from exc


def _parse_ground_access_row(row: dict[str, Any]) -> tuple[str, int, GroundLeg]:
    """One ``config/ground_access.yaml`` row -> ``(iata, priority, GroundLeg)``.

    ``distance_km``/``cost_eur`` come out of YAML as ``float``/``int``,
    never as ``Decimal`` — ``Money`` (master plan §4) rejects a raw
    ``float`` outright, so both go through ``Decimal(str(...))`` rather
    than ``Decimal(...)`` directly, exactly as ``config.models`` already
    does for TOML floats.
    """
    try:
        notes_raw = row.get("notes")
        notes = notes_raw.strip() if isinstance(notes_raw, str) else notes_raw
        leg = GroundLeg(
            from_location=row["from_location"],
            to_airport=row["to_airport"],
            mode=row["mode"],
            duration=timedelta(minutes=row["minutes"]),
            distance_km=Decimal(str(row["distance_km"])),
            cost=Money(amount=Decimal(str(row["cost_eur"])), currency="EUR"),
            source=row["source"],
            as_of=row["as_of"],
            notes=notes,
        )
        priority = row["priority"]
    except KeyError as exc:
        raise AirportRegistryError(
            f"config/ground_access.yaml row is missing required key {exc}: {row!r}"
        ) from exc
    return row["to_airport"], priority, leg


def _merge_catalogs(
    airports_raw: dict[str, Any], ground_access_raw: dict[str, Any]
) -> tuple[dict[str, Airport], tuple[str, ...]]:
    """Merge the two parsed catalogs into ``{iata: Airport}`` plus the
    origin order (by ``priority``, ascending) — that order IS the
    ``origins()`` contract (DECISIONS.md Addendum 2), not an incidental
    detail of dict iteration.
    """
    airport_rows = airports_raw.get("airports")
    if not isinstance(airport_rows, list) or not airport_rows:
        raise AirportRegistryError(
            "config/airports.yaml must have a non-empty top-level 'airports' list"
        )
    ground_rows = ground_access_raw.get("ground_access")
    if not isinstance(ground_rows, list) or not ground_rows:
        raise AirportRegistryError(
            "config/ground_access.yaml must have a non-empty top-level 'ground_access' list"
        )

    ground_by_iata: dict[str, GroundLeg] = {}
    priority_by_iata: dict[str, int] = {}
    for row in ground_rows:
        iata, priority, leg = _parse_ground_access_row(row)
        if iata in ground_by_iata:
            raise AirportRegistryError(
                f"config/ground_access.yaml has more than one row for {iata!r}"
            )
        if priority in priority_by_iata.values():
            raise AirportRegistryError(
                f"config/ground_access.yaml priority {priority} is used by more than "
                f"one origin"
            )
        ground_by_iata[iata] = leg
        priority_by_iata[iata] = priority

    # Defensive: priorities should densely cover 1..N with no gaps or
    # duplicates. Deliberately derived from how many origin rows are
    # actually present rather than hardcoded to 10, so this still holds
    # if an eleventh origin is added later (master plan §6).
    expected_priorities = set(range(1, len(priority_by_iata) + 1))
    if set(priority_by_iata.values()) != expected_priorities:
        raise AirportRegistryError(
            f"config/ground_access.yaml priorities must be exactly "
            f"{sorted(expected_priorities)}, got {sorted(priority_by_iata.values())}"
        )

    by_iata: dict[str, Airport] = {}
    for row in airport_rows:
        kwargs = _parse_airport_row(row)
        iata = kwargs["iata"]
        if iata in by_iata:
            raise AirportRegistryError(f"config/airports.yaml has more than one row for {iata!r}")
        by_iata[iata] = Airport(
            **kwargs,
            priority=priority_by_iata.get(iata),
            ground=ground_by_iata.get(iata),
        )

    missing = sorted(set(ground_by_iata) - set(by_iata))
    if missing:
        raise AirportRegistryError(
            f"config/ground_access.yaml references IATA codes absent from "
            f"config/airports.yaml: {missing}"
        )

    origin_order = tuple(sorted(ground_by_iata, key=lambda code: priority_by_iata[code]))
    return by_iata, origin_order


class AirportRegistry:
    """The merged, validated set of every airport this project knows
    about, loaded from ``config/airports.yaml`` + ``config/ground_access.yaml``
    exactly once at construction time.
    """

    def __init__(
        self,
        airports_path: Path = DEFAULT_AIRPORTS_PATH,
        ground_access_path: Path = DEFAULT_GROUND_ACCESS_PATH,
    ) -> None:
        airports_raw = load_yaml_catalog(airports_path)
        ground_access_raw = load_yaml_catalog(ground_access_path)
        self._by_iata, self._origin_order = _merge_catalogs(airports_raw, ground_access_raw)

    def origins(self) -> list[Airport]:
        """The origin airports, in ascending ``priority`` order.

        This order is load-bearing, not cosmetic — DECISIONS.md's
        restatement of Addendum 2 and several later acceptance tests
        assert the search call log follows it exactly.
        """
        return [self._by_iata[iata] for iata in self._origin_order]

    def destinations(self) -> list[Airport]:
        """The destination airports (no ``ground`` entry), in
        ``config/airports.yaml`` file order. This order is not
        load-bearing for destinations, but is kept stable for
        readability and to match what has already been shown to the
        project owner.
        """
        return [airport for airport in self._by_iata.values() if airport.ground is None]

    def all_airports(self) -> list[Airport]:
        """Every known airport (origins + destinations), file order."""
        return list(self._by_iata.values())

    def get(self, iata: str) -> Airport:
        """Look up one airport by IATA code.

        Raises ``UnknownAirportError`` rather than returning ``None`` —
        see that class's docstring for why.
        """
        try:
            return self._by_iata[iata]
        except KeyError:
            raise UnknownAirportError(f"unknown IATA code: {iata!r}") from None


_registry: AirportRegistry | None = None


def reload(
    airports_path: Path | None = None,
    ground_access_path: Path | None = None,
) -> AirportRegistry:
    """(Re)build the module-level singleton, optionally from override
    paths, and return it.

    Exists so tests can point the registry at fixture files instead of
    mutating the real ``config/`` directory. Production code never needs
    to call this directly — ``origins()``/``destinations()``/``get()``
    lazily build the singleton from the real files on first use.
    """
    global _registry
    _registry = AirportRegistry(
        airports_path if airports_path is not None else DEFAULT_AIRPORTS_PATH,
        ground_access_path if ground_access_path is not None else DEFAULT_GROUND_ACCESS_PATH,
    )
    return _registry


def _get_registry() -> AirportRegistry:
    global _registry
    if _registry is None:
        _registry = AirportRegistry()
    return _registry


def origins() -> list[Airport]:
    """The 10 European origins, in priority order (see ``AirportRegistry.origins``)."""
    return _get_registry().origins()


def destinations() -> list[Airport]:
    """The 8 Indian destinations (see ``AirportRegistry.destinations``)."""
    return _get_registry().destinations()


def get(iata: str) -> Airport:
    """Look up one airport by IATA code; raises ``UnknownAirportError`` if unknown."""
    return _get_registry().get(iata)
