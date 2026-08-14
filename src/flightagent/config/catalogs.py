"""Generic YAML catalog loading.

Master plan §3 places several catalog files under ``config/``:
``airports.yaml`` (IATA → name, city, country, IANA tz, lat/lon),
``ground_access.yaml`` (Nieuwegein → airport: mode, minutes, cost,
source, as_of), and ``booking_links.yaml`` (deep-link URL templates).
None of those files exist yet — the airport registry is built in T8 — so
this module deliberately knows nothing about what an airport or a
ground-access row looks like. It provides only the one thing every
future typed catalog wrapper needs: a single, consistently-erroring way
to read a YAML file's top-level mapping.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class CatalogLoadError(RuntimeError):
    """Raised when a catalog file is missing, unreadable, or not a top-level mapping."""


def load_yaml_catalog(path: str | Path) -> dict[str, Any]:
    """Load a YAML catalog file and return its top-level mapping.

    Every catalog under ``config/`` is a top-level mapping, never a bare
    list or scalar, so callers get a plain ``dict[str, Any]`` and do not
    need to re-check the shape themselves. An empty file yields ``{}``
    rather than ``None``, so callers can always iterate the result
    without a null check.
    """
    catalog_path = Path(path)
    try:
        raw = catalog_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CatalogLoadError(f"cannot read catalog file: {catalog_path}") from exc

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise CatalogLoadError(f"catalog file is not valid YAML: {catalog_path}") from exc

    if data is None:
        return {}
    if not isinstance(data, dict):
        raise CatalogLoadError(
            f"catalog file {catalog_path} must contain a top-level mapping, "
            f"got {type(data).__name__}"
        )
    return data
