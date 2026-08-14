"""Typed configuration models mirroring ``config/defaults.toml``.

Master plan §7 ("Config, logging, metrics" → "Config layering") states the
rule this module exists to enforce: ``extra="forbid"`` on every model,
because "a typo'd key that silently does nothing is a class of bug that
survives for months." Every model below — including the array-of-tables
item ``PenaltyBand`` — sets it. None of these models are constructed
directly with real configuration data; ``config.loader.load_config()`` owns
layering the four precedence tiers into one dict and validates the whole
tree in a single ``FlightAgentSettings.model_validate()`` call, so a typo in
any layer produces exactly one consistent error path.

Fields that feed money arithmetic or the score formula (master plan §4:
"Money is Decimal ... Scores are Decimal too") are typed ``Decimal``, not
``float`` — pydantic converts a TOML float via its string representation
(``Decimal(str(v))``), so ``3.0`` becomes ``Decimal("3.0")`` cleanly rather
than picking up binary float artifacts.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict
from pydantic_settings import BaseSettings, SettingsConfigDict


class PenaltyBand(BaseModel):
    """One row of the layover penalty ladder — D9, lower-inclusive half-open.

    ``max_minutes`` is exclusive for every band except the last, whose top
    is the closed end of D8's ``[180, 360]`` validity window. The loader
    does not re-derive this from D8/D9 constants; ``config/defaults.toml``
    spells out all three bands explicitly so the boundary reading is
    visible in the file a reviewer actually opens.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    min_minutes: int
    max_minutes: int
    penalty_eur: Decimal


class ConcurrencySettings(BaseSettings):
    """``[concurrency]`` — master plan §5, "Concurrency: 8"."""

    model_config = SettingsConfigDict(extra="forbid")

    max_concurrent_searches: int


class RetrySettings(BaseSettings):
    """``[retry]`` — master plan §5 retry/backoff/circuit-breaker policy."""

    model_config = SettingsConfigDict(extra="forbid")

    max_attempts: int
    retry_budget_fraction: float
    backoff_base_seconds: float
    backoff_cap_seconds: float
    circuit_breaker_failure_threshold: int
    circuit_breaker_open_seconds: float


class LayoverSettings(BaseSettings):
    """``[layover]`` — D8 (closed validity window) and D9 (penalty bands)."""

    model_config = SettingsConfigDict(extra="forbid")

    layover_min_minutes: int
    layover_max_minutes: int
    penalty_bands: list[PenaltyBand]


class ScoringSettings(BaseSettings):
    """``[scoring]`` — finding 0.1 (direct-bonus divergence) and 0.8 (€/hour weight)."""

    model_config = SettingsConfigDict(extra="forbid")

    time_value_eur_per_hour: Decimal
    direct_bonus_eur: Decimal
    direct_bonus_mode: Literal["fixed", "proportional"]


class GroundTravelSettings(BaseSettings):
    """``[ground_travel]`` — D7's hard filter plus the parallel total_journey_score weights."""

    model_config = SettingsConfigDict(extra="forbid")

    max_ground_travel_minutes: int
    ground_cost_weight: Decimal
    ground_time_weight: Decimal


class EarlyStopSettings(BaseSettings):
    """``[early_stop]`` — D12: off by default, per-destination €-delta trigger."""

    model_config = SettingsConfigDict(extra="forbid")

    enabled: bool
    threshold_eur: Decimal


class CacheSettings(BaseSettings):
    """``[cache]`` — master plan §5 TTL tiers (by days-to-departure) and pagination cap."""

    model_config = SettingsConfigDict(extra="forbid")

    ttl_minutes_over_180_days: int
    ttl_minutes_30_to_180_days: int
    ttl_minutes_7_to_30_days: int
    ttl_minutes_under_7_days: int
    max_pages_per_task: int


class OutputSettings(BaseSettings):
    """``[output]`` — D15: exact output filenames and top-N truncation."""

    model_config = SettingsConfigDict(extra="forbid")

    report_path: str
    results_path: str
    top_n_global: int
    top_n_per_destination: int


class FlightAgentSettings(BaseSettings):
    """Root settings object — one field per ``config/defaults.toml`` table.

    Do not construct this directly with ``FlightAgentSettings(**data)`` —
    that path invokes ``BaseSettings.__init__``, whose own env/dotenv
    auto-sourcing would race against ``config.loader``'s explicit four-layer
    precedence. Always go through ``config.loader.load_config()``, which
    builds the fully-merged dict itself and calls
    ``FlightAgentSettings.model_validate()`` — this bypasses
    ``BaseSettings.__init__`` entirely, so the loader's merge order is the
    only thing that determines the result.

    ``env_prefix``/``env_nested_delimiter`` are declared here to document
    the ``FLIGHTAGENT__SECTION__KEY`` convention (master plan §7) even
    though the loader does its own env parsing rather than relying on
    ``BaseSettings``'s native env source — see ``config.loader`` for why.
    """

    model_config = SettingsConfigDict(
        extra="forbid",
        env_prefix="FLIGHTAGENT__",
        env_nested_delimiter="__",
    )

    concurrency: ConcurrencySettings
    retry: RetrySettings
    layover: LayoverSettings
    scoring: ScoringSettings
    ground_travel: GroundTravelSettings
    early_stop: EarlyStopSettings
    cache: CacheSettings
    output: OutputSettings
