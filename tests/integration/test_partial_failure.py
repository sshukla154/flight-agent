"""Integration: partial and total run failure through the real CLI (T28).

Two scenarios, both driven end to end through
``flightagent run --all-destinations`` via Typer's ``CliRunner``:

- Exactly one destination (BOM) always errors ("simulating always 503s");
  the other seven succeed normally. T27's own exit-code scheme treats
  ``RunStatus.PARTIAL`` as still-successful (exit ``0``) -- checked here
  against the CLI's OWN exit-code constants, not assumed -- and BOM shows
  up in the Failed Searches section of BOTH artifacts with the right
  ``error_type``, while the other seven rank normally in both.
- Every destination errors. ``RunStatus.FAILED`` gets its own distinct,
  nonzero exit code (never ``NO_VALID_ITINERARIES_EXIT_CODE``, the code
  ``test_no_results.py``'s sibling scenario uses) and a message that must
  never repeat finding 0.5's original bug: a provider outage must not be
  reported as "no itinerary satisfied the layover rule".

Distinct from ``tests/unit/test_cli.py``'s own (already-committed)
``TestAllDestinationsFailed``: this file additionally proves the PARTIAL
path (7 succeed / 1 fails) end to end, and asserts the FAILED path's
message content directly, not just its exit code.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flightagent.airports.registry import destinations as registry_destinations
from flightagent.cli import (
    ALL_DESTINATIONS_FAILED_EXIT_CODE,
    NO_VALID_ITINERARIES_EXIT_CODE,
    app,
)
from flightagent.providers.errors import ProviderTimeoutError
from tests.support.instrumented_provider import Fail, InstrumentedProvider, Succeed

runner = CliRunner()

_ORIGIN = "AMS"
_ALL_8_DESTINATIONS = tuple(sorted(airport.iata for airport in registry_destinations()))
_FAILING_DESTINATION = "BOM"

_ALL_DESTINATIONS_ARGS = [
    "run",
    "--origin",
    _ORIGIN,
    "--date",
    "2027-07-17",
    "--max-stops",
    "1",
    "--all-destinations",
]

# The spec's own hardcoded string (D19 / finding 0.5), en dash included --
# must never appear when the run status is FAILED, not NO_RESULTS.
_SPEC_LAYOVER_MESSAGE_FRAGMENT = "3–6 hour layover rule"


@pytest.fixture
def isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def no_real_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace real ``asyncio.sleep`` with an instant no-op -- the retry
    backoff loop still RUNS (proving the retry mechanics actually fired),
    but this integration test doesn't burn real wall-clock seconds on a
    delay value it isn't asserting, matching
    ``tests/unit/test_retry.py``'s own established ``_patch_sleep`` pattern.
    """

    async def _fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)


class TestOneDestinationAlwaysFailsRestSucceed:
    def test_partial_run_exits_zero_and_reports_both_correctly(
        self, isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch, no_real_delay: None
    ) -> None:
        assert len(_ALL_8_DESTINATIONS) == 8
        succeeding = [d for d in _ALL_8_DESTINATIONS if d != _FAILING_DESTINATION]
        assert len(succeeding) == 7

        scripts = {destination: [Succeed(offer_count=2)] for destination in succeeding}
        # Never recovers -- every attempt (including every retry) hits
        # this same step, "simulating always 503s".
        scripts[_FAILING_DESTINATION] = [
            Fail(ProviderTimeoutError(f"{_FAILING_DESTINATION} 503", provider="instrumented"))
        ]
        provider = InstrumentedProvider(scripts=scripts)
        monkeypatch.setattr("flightagent.cli._build_provider", lambda name: provider)

        result = runner.invoke(app, _ALL_DESTINATIONS_ARGS)

        # T27's own exit-code scheme: PARTIAL is still success (0), checked
        # against the CLI's own constants rather than assumed.
        assert result.exit_code == 0, result.output
        assert result.exit_code != NO_VALID_ITINERARIES_EXIT_CODE
        assert result.exit_code != ALL_DESTINATIONS_FAILED_EXIT_CODE

        report_path = isolated_cwd / "out" / "flight_report_2027-07-17.md"
        results_path = isolated_cwd / "out" / "flight_results_2027-07-17.json"
        assert report_path.is_file()
        assert results_path.is_file()

        report_text = report_path.read_text(encoding="utf-8")
        results = json.loads(results_path.read_text(encoding="utf-8"))

        # The other 7 destinations are ranked normally in BOTH artifacts.
        json_destinations = {item["destination"] for item in results["top_itineraries"]}
        assert json_destinations, "expected at least one ranked itinerary"
        assert json_destinations <= set(succeeding)
        assert _FAILING_DESTINATION not in json_destinations
        assert "## Recommended Flight" in report_text

        # BOM shows up in Failed Searches -- BOTH artifacts, right
        # error_type -- while the 7 survivors are unaffected.
        assert results["failed_searches"] == [
            {
                "destination": _FAILING_DESTINATION,
                "error_type": "ProviderTimeoutError",
                "error_detail": f"{_FAILING_DESTINATION} 503",
            }
        ]

        assert "## Failed Searches" in report_text
        failed_section = report_text.split("## Failed Searches", 1)[1]
        assert _FAILING_DESTINATION in failed_section
        assert "ProviderTimeoutError" in failed_section


class TestAllDestinationsFail:
    def test_all_failing_gives_failed_status_distinct_exit_code_and_honest_message(
        self, isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch, no_real_delay: None
    ) -> None:
        provider = InstrumentedProvider(
            scripts={
                destination: [
                    Fail(ProviderTimeoutError(f"{destination} 503", provider="instrumented"))
                ]
                for destination in _ALL_8_DESTINATIONS
            }
        )
        monkeypatch.setattr("flightagent.cli._build_provider", lambda name: provider)

        result = runner.invoke(app, _ALL_DESTINATIONS_ARGS)

        assert result.exit_code == ALL_DESTINATIONS_FAILED_EXIT_CODE, result.output
        # Distinct from BOTH the partial-success path above and the
        # NO_RESULTS path (test_no_results.py's sibling scenario) -- never
        # confusable by exit code alone (finding 0.5's whole point).
        assert result.exit_code != 0
        assert result.exit_code != NO_VALID_ITINERARIES_EXIT_CODE
        assert not (isolated_cwd / "out").exists()

        combined_output = result.output
        # Must NEVER repeat finding 0.5's original bug: a provider outage
        # is not "no itinerary satisfied the layover rule".
        assert _SPEC_LAYOVER_MESSAGE_FRAGMENT not in combined_output
        assert "no itinerary satisfied" not in combined_output.lower()
        # The actual message content IS asserted, not just the exit code.
        assert "unreachable" in combined_output.lower()
        assert "every attempt otherwise failed" in combined_output.lower() or (
            "not merely short of valid itineraries" in combined_output.lower()
        )
