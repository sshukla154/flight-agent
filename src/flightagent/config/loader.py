"""Four-layer configuration loading — master plan §7, "Config layering".

Precedence, later wins::

    packaged defaults.toml
      < ./config/config.toml (or $FLIGHTAGENT_CONFIG path override)
      < environment variables (FLIGHTAGENT__SECTION__KEY)
      < CLI flags                    # CLI layer wired in a later phase

All four layers are merged as plain ``dict``s and validated exactly once,
at the very end, via ``FlightAgentSettings.model_validate()``. That single
call point is deliberate: every model in ``config.models`` sets
``extra="forbid"``, so a typo anywhere in any layer — a mistyped TOML key,
a misspelled env var section, a bad CLI override — surfaces as the same
``pydantic.ValidationError`` shape, rather than four different silent-vs-
loud failure paths depending on which layer introduced it.

Why this module parses env vars itself instead of letting
``pydantic_settings.BaseSettings`` do it natively: ``BaseSettings``'s own
env source silently *drops* an env var whose top-level section name it
does not recognise (e.g. ``FLIGHTAGENT__BOGSU__X`` for a typo'd
"layover"), because it only descends into fields it already knows about.
That is exactly the "typo'd key survives silently" bug this project is
built to prevent (master plan §7), so the env layer here is parsed into a
plain nested dict with no such filtering and handed to
``model_validate()`` alongside the TOML layers — an unrecognised section
*or* key fails loudly there instead.
"""

from __future__ import annotations

import hashlib
import json
import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from flightagent.config.models import FlightAgentSettings

ENV_PREFIX = "FLIGHTAGENT__"
ENV_NESTED_DELIMITER = "__"
CONFIG_PATH_ENV_VAR = "FLIGHTAGENT_CONFIG"
DEFAULT_CONFIG_TOML_RELATIVE_PATH = Path("config") / "config.toml"

# Keys whose name alone marks them as secrets, so config_digest never hashes
# one in even if a future config table accidentally grew one. Master plan
# §7: secrets come from env or a secret file, never from the config file —
# this is a second, structural line of defence, not a substitute for that
# rule.
_SECRET_KEY_MARKERS = (
    "secret",
    "token",
    "password",
    "api_key",
    "apikey",
    "client_secret",
    "access_token",
    "bearer",
)


class ConfigError(RuntimeError):
    """Raised for configuration problems this loader detects itself.

    Schema-level problems (unknown keys, missing required keys, wrong
    types) are raised by pydantic as ``ValidationError`` from the single
    ``model_validate()`` call in ``load_config`` — this class is reserved
    for problems that exist before validation can even start, such as a
    missing packaged-defaults file or a malformed env var name.
    """


def _find_repo_root(start: Path) -> Path:
    """Walk upward from ``start`` until a directory containing ``pyproject.toml`` is found.

    Used to locate the packaged ``config/defaults.toml`` (master plan §3
    places it at the project root, a sibling of ``src/``, not inside the
    ``flightagent`` package) without hardcoding a fixed number of parent
    hops that would silently break if this module ever moves.
    """
    for candidate in (start, *start.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise ConfigError(f"could not locate project root (no pyproject.toml found above {start})")


_REPO_ROOT = _find_repo_root(Path(__file__).resolve().parent)
PACKAGED_DEFAULTS_PATH = _REPO_ROOT / "config" / "defaults.toml"


def _load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` onto ``base``; ``override`` wins on every leaf.

    Two dicts at the same path merge key-by-key. Anything else at a shared
    path (a list, a scalar, or a dict meeting a non-dict) is replaced
    wholesale by ``override``'s value — deliberate for ``penalty_bands``: a
    layer that overrides the band table replaces it completely rather than
    trying to splice individual rows across layers.
    """
    merged = dict(base)
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


def _parse_env_layer(
    env: Mapping[str, str],
    prefix: str = ENV_PREFIX,
    delimiter: str = ENV_NESTED_DELIMITER,
) -> dict[str, Any]:
    """Parse ``FLIGHTAGENT__SECTION__KEY`` env vars into a nested dict.

    Every prefixed env var becomes a leaf in the returned tree with no
    filtering against known model fields — an unrecognised section or key
    flows straight through to ``FlightAgentSettings.model_validate()``,
    whose ``extra="forbid"`` turns it into a hard error. Values are left as
    raw strings; pydantic coerces them (int/float/Decimal/bool/str) during
    validation.
    """
    prefix_upper = prefix.upper()
    result: dict[str, Any] = {}
    for raw_key, raw_value in env.items():
        key_upper = raw_key.upper()
        if not key_upper.startswith(prefix_upper):
            continue
        suffix = key_upper[len(prefix_upper) :]
        segments = [segment.lower() for segment in suffix.split(delimiter)]
        if not suffix or any(segment == "" for segment in segments):
            raise ConfigError(f"malformed config env var name: {raw_key!r}")

        node = result
        for segment in segments[:-1]:
            existing = node.get(segment)
            if existing is None:
                existing = {}
                node[segment] = existing
            if not isinstance(existing, dict):
                raise ConfigError(
                    f"conflicting config env vars: {raw_key!r} treats "
                    f"{delimiter.join(segments[:-1])!r} as both a value and a section"
                )
            node = existing
        node[segments[-1]] = raw_value
    return result


def _resolve_config_override_path(
    config_path: Path | None,
    env: Mapping[str, str],
) -> Path | None:
    """Pick layer 2: explicit path > $FLIGHTAGENT_CONFIG > ./config/config.toml."""
    if config_path is not None:
        return config_path
    env_override = env.get(CONFIG_PATH_ENV_VAR)
    if env_override:
        return Path(env_override)
    return (
        DEFAULT_CONFIG_TOML_RELATIVE_PATH
        if DEFAULT_CONFIG_TOML_RELATIVE_PATH.is_file()
        else None
    )


def load_config(
    *,
    defaults_path: Path | None = None,
    config_path: Path | None = None,
    env: Mapping[str, str] | None = None,
    cli_overrides: dict[str, Any] | None = None,  # CLI layer wired in a later phase
) -> FlightAgentSettings:
    """Resolve the effective configuration from all four layers.

    Parameters exist mainly so tests can substitute layers without
    mutating ``os.environ`` or the filesystem:

    - ``defaults_path``: override the packaged ``config/defaults.toml``.
    - ``config_path``: force layer 2 to a specific file, skipping the
      ``$FLIGHTAGENT_CONFIG`` / ``./config/config.toml`` lookup entirely.
    - ``env``: override the environment mapping (defaults to
      ``os.environ``).
    - ``cli_overrides``: layer 4. There is no CLI yet — a future Typer
      command plugs in here with the flag values it parsed, as a plain
      dict shaped like the TOML tables (e.g.
      ``{"layover": {"layover_min_minutes": 200}}``).
    """
    env_mapping: Mapping[str, str] = env if env is not None else os.environ

    defaults_file = defaults_path if defaults_path is not None else PACKAGED_DEFAULTS_PATH
    if not defaults_file.is_file():
        raise ConfigError(f"packaged defaults not found at {defaults_file}")
    merged = _load_toml(defaults_file)

    override_file = _resolve_config_override_path(config_path, env_mapping)
    if override_file is not None:
        if not override_file.is_file():
            raise ConfigError(f"configured override file does not exist: {override_file}")
        merged = _deep_merge(merged, _load_toml(override_file))

    merged = _deep_merge(merged, _parse_env_layer(env_mapping))

    if cli_overrides:
        merged = _deep_merge(merged, cli_overrides)

    return FlightAgentSettings.model_validate(merged)


def _strip_secrets(value: Any) -> Any:
    """Recursively drop any dict key whose name looks like a secret."""
    if isinstance(value, dict):
        return {
            key: _strip_secrets(val)
            for key, val in value.items()
            if not any(marker in key.lower() for marker in _SECRET_KEY_MARKERS)
        }
    if isinstance(value, list):
        return [_strip_secrets(item) for item in value]
    return value


def canonical_json(data: Any) -> str:
    """Deterministic JSON serialisation used as the digest's hash input.

    Sorted keys and no separator whitespace so the same effective config
    always produces the same bytes regardless of dict insertion order.
    """
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)


def compute_config_digest(settings: FlightAgentSettings) -> str:
    """``sha256(canonical_json(effective_config minus secrets))`` (master plan §7).

    Ties a generated report to the exact config that produced it. The
    current config models never carry a credential — those live in env
    vars per ``.env.example`` and D6 — but ``_strip_secrets`` is a
    structural safeguard against a future config table accidentally
    growing one, not a substitute for keeping secrets out in the first
    place.
    """
    payload = _strip_secrets(settings.model_dump(mode="json"))
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
