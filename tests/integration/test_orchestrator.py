"""Integration: the full ``--all-destinations`` fan-out through the real CLI (T28).

Exercises the literal target invocation --
``flightagent run --all-destinations --origin AMS`` -- via Typer's
``CliRunner``, with ``tests.support.instrumented_provider.InstrumentedProvider``
substituted for ``MockProvider``. There is no dependency-injection
constructor argument on ``run()`` itself; the seam is
``flightagent.cli._build_provider``, a module-level resolver function --
monkeypatching it is exactly the pattern
``tests/unit/test_cli.py::TestAllDestinationsSucceeds`` already established
one layer down (T27) for this same CLI.

Distinct from that already-committed unit coverage: this file asserts the
EXACT call count against the instrumented provider's own call log (never
"at least 8" or inferred from the report's contents), and proves peak
concurrency genuinely OVERLAPS -- not merely stays under the bound, which a
fully (buggy) serial executor would also satisfy. See
``tests/support/instrumented_provider.py``'s own T28 docstring note for why
an artificial per-call delay is required for that second property to be
observable at all: a call that never awaits anything runs to completion
atomically, before the event loop ever switches to a sibling task.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from flightagent.airports.registry import destinations as registry_destinations
from flightagent.cli import app
from flightagent.config.loader import load_config
from tests.support.instrumented_provider import InstrumentedProvider, Succeed

runner = CliRunner()

_ORIGIN = "AMS"
_DEPARTURE_DATE = "2027-07-17"

_ALL_DESTINATIONS_ARGS = [
    "run",
    "--origin",
    _ORIGIN,
    "--date",
    _DEPARTURE_DATE,
    "--max-stops",
    "1",
    "--all-destinations",
]


@pytest.fixture
def isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run the CLI from an isolated directory so ``out/...`` never touches
    the real repo's ``out/`` directory -- matching
    ``tests/unit/test_cli.py``'s own fixture of the same name."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _registry_destination_codes() -> set[str]:
    return {airport.iata for airport in registry_destinations()}


class TestFanOutIssuesExactlyOneCallPerDestination:
    def test_issues_exactly_8_provider_calls_one_per_registry_destination(
        self, isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        expected_destinations = _registry_destination_codes()
        assert len(expected_destinations) == 8

        provider = InstrumentedProvider(
            scripts={
                destination: [Succeed(offer_count=2)] for destination in expected_destinations
            }
        )
        monkeypatch.setattr("flightagent.cli._build_provider", lambda name: provider)

        result = runner.invoke(app, _ALL_DESTINATIONS_ARGS)

        assert result.exit_code == 0, result.output

        # EXACT count -- asserted against the instrumented provider's own
        # call log, never inferred from the report's contents.
        assert len(provider.call_log) == 8

        called_destinations = [record.destination for record in provider.call_log]
        # No duplicate, no omission -- exactly the registry set, each
        # exactly once.
        assert set(called_destinations) == expected_destinations
        assert len(set(called_destinations)) == len(called_destinations)
        assert {record.origin for record in provider.call_log} == {_ORIGIN}


class TestFanOutConcurrencyIsBoundedAndReal:
    def test_peak_concurrency_overlaps_and_never_exceeds_configured_bound(
        self, isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = load_config()
        max_concurrent = settings.concurrency.max_concurrent_searches
        # The documented default this test targets (config/defaults.toml,
        # master plan S5 "Concurrency: 8") -- asserted explicitly so a
        # future default change surfaces here rather than silently
        # changing what this test proves.
        assert max_concurrent == 8

        expected_destinations = _registry_destination_codes()
        provider = InstrumentedProvider(
            scripts={
                destination: [Succeed(offer_count=2)] for destination in expected_destinations
            },
            # Long enough that 8 calls truly overlapping in real wall-clock
            # time is not a coincidence of scheduling; short enough the
            # test still runs in well under a second (all 8 delays overlap
            # rather than stack).
            call_delay_seconds=0.2,
        )
        monkeypatch.setattr("flightagent.cli._build_provider", lambda name: provider)

        result = runner.invoke(app, _ALL_DESTINATIONS_ARGS)

        assert result.exit_code == 0, result.output
        assert len(provider.call_log) == 8

        # Never exceeds the semaphore bound...
        assert provider.peak_in_flight <= max_concurrent
        # ...and genuinely reached it: proof this is a real concurrent
        # fan-out, not an executor that (bug notwithstanding) happens to
        # run everything serially, which would also satisfy the "<="
        # assertion above with peak_in_flight == 1.
        assert provider.peak_in_flight == max_concurrent
