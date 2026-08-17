"""Unit tests for flightagent.config.

The two behaviours DECISIONS.md and master plan §7 call out as most
important are proved directly and by name:

- ``test_unrecognised_toml_key_raises`` / ``test_unrecognised_env_key_raises``:
  a typo'd key in any layer is a hard ``ValidationError``, never silently
  ignored (master plan §7).
- ``test_env_override_wins_over_packaged_default``: the env layer actually
  outranks the packaged default in the merged result, not just in prose.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from flightagent.config.catalogs import CatalogLoadError, load_yaml_catalog
from flightagent.config.loader import ConfigError, compute_config_digest, load_config

# Real packaged config/defaults.toml — loaded via the real loader path in
# most tests so the test suite exercises the file that actually ships,
# not a synthetic stand-in.


def test_packaged_defaults_match_decisions_register() -> None:
    """Loading with no overrides reproduces the exact DECISIONS.md defaults."""
    settings = load_config(env={})

    # D8: closed [180, 360] validity window.
    assert settings.layover.layover_min_minutes == 180
    assert settings.layover.layover_max_minutes == 360

    # D9: lower-inclusive half-open penalty bands, exactly as specified.
    bands = settings.layover.penalty_bands
    assert len(bands) == 3
    assert (bands[0].min_minutes, bands[0].max_minutes, bands[0].penalty_eur) == (
        180,
        240,
        Decimal("0"),
    )
    assert (bands[1].min_minutes, bands[1].max_minutes, bands[1].penalty_eur) == (
        240,
        300,
        Decimal("10"),
    )
    assert (bands[2].min_minutes, bands[2].max_minutes, bands[2].penalty_eur) == (
        300,
        360,
        Decimal("20"),
    )

    # finding 0.8 / finding 0.1: score weight and direct bonus.
    assert settings.scoring.time_value_eur_per_hour == Decimal("3.0")
    assert settings.scoring.direct_bonus_eur == Decimal("-120.0")
    assert settings.scoring.direct_bonus_mode == "fixed"

    # D7: hard ground-travel filter.
    assert settings.ground_travel.max_ground_travel_minutes == 150

    # D12: early stop off by default, €250 threshold.
    assert settings.early_stop.enabled is False
    assert settings.early_stop.threshold_eur == Decimal("250.0")

    # master plan §5.
    assert settings.concurrency.max_concurrent_searches == 8
    assert settings.retry.max_attempts == 3

    # D15: exact filenames and top-N truncation.
    assert settings.output.report_path == "out/flight_report_2027-07-17.md"
    assert settings.output.results_path == "out/flight_results_2027-07-17.json"
    assert settings.output.top_n_global == 10
    assert settings.output.top_n_per_destination == 3

    # T45: per-run artifact directory layout, additive to the two fixed
    # D15 paths above -- never derived from them, never overriding them.
    assert settings.output.runs_dir == "data/runs"


def test_unrecognised_toml_key_in_override_file_raises(tmp_path: Path) -> None:
    """A typo'd key in ./config/config.toml is a hard error, not a no-op.

    This is the single most important behaviour of the config module
    (master plan §7): every model sets extra="forbid" specifically so this
    case cannot pass silently.
    """
    override = tmp_path / "config.toml"
    override.write_text(
        '[layover]\nlayover_min_minutes = 190\nlayover_min_minuets = 190\n',
        encoding="utf-8",
    )

    with pytest.raises(ValidationError) as exc_info:
        load_config(config_path=override, env={})

    assert "layover_min_minuets" in str(exc_info.value)
    assert "extra_forbidden" in str(exc_info.value) or "Extra inputs" in str(exc_info.value)


def test_unrecognised_toml_section_in_override_file_raises(tmp_path: Path) -> None:
    """A whole unrecognised top-level TOML table is also a hard error."""
    override = tmp_path / "config.toml"
    override.write_text('[layovr]\nlayover_min_minutes = 190\n', encoding="utf-8")

    with pytest.raises(ValidationError):
        load_config(config_path=override, env={})


def test_unrecognised_env_key_raises() -> None:
    """A typo'd key introduced only via an env var override is also a hard error.

    Every other required field is still satisfied by the packaged
    defaults.toml, isolating the failure to the one bad env var.
    """
    with pytest.raises(ValidationError) as exc_info:
        load_config(env={"FLIGHTAGENT__LAYOVER__LAYOVER_MIN_MINUETS": "190"})

    assert "layover_min_minuets" in str(exc_info.value)


def test_unrecognised_env_section_raises() -> None:
    """An env var naming a section that does not exist at all also raises.

    This is the specific gap pydantic-settings' own native env source does
    NOT catch (it silently drops unknown top-level sections) — the reason
    loader.py parses the env layer itself instead of delegating to it.
    """
    with pytest.raises(ValidationError):
        load_config(env={"FLIGHTAGENT__BOGUS_SECTION__SOME_KEY": "1"})


def test_env_override_wins_over_packaged_default() -> None:
    """An env var override actually wins over the packaged default value."""
    baseline = load_config(env={})
    assert baseline.concurrency.max_concurrent_searches == 8  # sanity on the default itself

    overridden = load_config(
        env={"FLIGHTAGENT__CONCURRENCY__MAX_CONCURRENT_SEARCHES": "32"}
    )

    assert overridden.concurrency.max_concurrent_searches == 32
    # Untouched fields still come through from the packaged default —
    # proves this is a merge, not a full replacement.
    assert overridden.layover.layover_min_minutes == 180
    assert overridden.retry.max_attempts == 3


def test_config_toml_layer_overrides_packaged_default_but_env_still_wins(
    tmp_path: Path,
) -> None:
    """Full precedence chain: defaults < config.toml < env, in that order."""
    override = tmp_path / "config.toml"
    override.write_text(
        "[retry]\nmax_attempts = 5\nretry_budget_fraction = 0.25\n"
        "backoff_base_seconds = 0.5\nbackoff_cap_seconds = 8.0\n"
        "circuit_breaker_failure_threshold = 10\ncircuit_breaker_open_seconds = 60.0\n",
        encoding="utf-8",
    )

    from_file_only = load_config(config_path=override, env={})
    assert from_file_only.retry.max_attempts == 5  # config.toml beat the packaged default (3)

    from_file_and_env = load_config(
        config_path=override,
        env={"FLIGHTAGENT__RETRY__MAX_ATTEMPTS": "9"},
    )
    assert from_file_and_env.retry.max_attempts == 9  # env beat config.toml


def test_cli_overrides_are_applied_as_the_final_layer() -> None:
    """cli_overrides (the future CLI layer's plug-in point) wins over everything else."""
    settings = load_config(
        env={"FLIGHTAGENT__CONCURRENCY__MAX_CONCURRENT_SEARCHES": "32"},
        cli_overrides={"concurrency": {"max_concurrent_searches": 4}},
    )
    assert settings.concurrency.max_concurrent_searches == 4


def test_missing_packaged_defaults_raises_config_error(tmp_path: Path) -> None:
    """A missing defaults file is a ConfigError, not a confusing downstream failure."""
    missing = tmp_path / "does_not_exist.toml"
    with pytest.raises(ConfigError, match="packaged defaults not found"):
        load_config(defaults_path=missing, env={})


def test_missing_explicit_config_override_raises_config_error(tmp_path: Path) -> None:
    """An explicitly-requested override file that doesn't exist is an error, not a silent skip."""
    missing = tmp_path / "does_not_exist.toml"
    with pytest.raises(ConfigError, match="configured override file does not exist"):
        load_config(config_path=missing, env={})


def test_config_digest_is_stable_sha256_hex() -> None:
    """config_digest is a deterministic sha256 hex digest of the resolved config."""
    first = compute_config_digest(load_config(env={}))
    second = compute_config_digest(load_config(env={}))

    assert first == second
    assert len(first) == 64
    assert all(char in "0123456789abcdef" for char in first)


def test_config_digest_changes_when_effective_config_changes() -> None:
    """Two different resolved configs must not collide on the same digest."""
    baseline_digest = compute_config_digest(load_config(env={}))
    changed_digest = compute_config_digest(
        load_config(env={"FLIGHTAGENT__CONCURRENCY__MAX_CONCURRENT_SEARCHES": "9"})
    )

    assert baseline_digest != changed_digest


def test_load_yaml_catalog_returns_top_level_mapping(tmp_path: Path) -> None:
    catalog_file = tmp_path / "sample.yaml"
    catalog_file.write_text(yaml.safe_dump({"AMS": {"name": "Schiphol"}}), encoding="utf-8")

    result = load_yaml_catalog(catalog_file)

    assert result == {"AMS": {"name": "Schiphol"}}


def test_load_yaml_catalog_rejects_non_mapping_top_level(tmp_path: Path) -> None:
    catalog_file = tmp_path / "sample.yaml"
    catalog_file.write_text(yaml.safe_dump(["AMS", "EIN"]), encoding="utf-8")

    with pytest.raises(CatalogLoadError, match="top-level mapping"):
        load_yaml_catalog(catalog_file)


def test_load_yaml_catalog_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(CatalogLoadError, match="cannot read catalog file"):
        load_yaml_catalog(tmp_path / "does_not_exist.yaml")
