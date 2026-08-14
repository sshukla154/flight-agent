"""flightagent.config — typed settings, layered loading, generic catalog IO.

See master plan §7 ("Config, logging, metrics" → "Config layering") for
the four-layer precedence this package implements, and DECISIONS.md D14
for the "never silently convert currency" rule that governs the wider
project's stance on the same class of problem this module solves for
config keys: an unrecognised or ambiguous value is a hard error, never a
best-effort guess.
"""

from __future__ import annotations

from flightagent.config.catalogs import CatalogLoadError, load_yaml_catalog
from flightagent.config.loader import ConfigError, compute_config_digest, load_config
from flightagent.config.models import (
    CacheSettings,
    ConcurrencySettings,
    EarlyStopSettings,
    FlightAgentSettings,
    GroundTravelSettings,
    LayoverSettings,
    OutputSettings,
    PenaltyBand,
    RetrySettings,
    ScoringSettings,
)

__all__ = [
    "CacheSettings",
    "CatalogLoadError",
    "ConcurrencySettings",
    "ConfigError",
    "EarlyStopSettings",
    "FlightAgentSettings",
    "GroundTravelSettings",
    "LayoverSettings",
    "OutputSettings",
    "PenaltyBand",
    "RetrySettings",
    "ScoringSettings",
    "compute_config_digest",
    "load_config",
    "load_yaml_catalog",
]
