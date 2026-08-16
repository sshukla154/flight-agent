"""Tests for ``flightagent.cli`` (Phase 2 / T16 — the full pipeline wired
into one command).

Two things this file proves, at minimum, per the task brief:

- ``TestTargetInvocation``: the exact Phase 2 exit-criterion command
  (``flightagent run --origin AMS --dest DEL --date 2027-07-17
  --max-stops 1 --provider mock``) exits ``0`` and writes both artifacts —
  and, because the exit criterion also requires it, that two runs of the
  identical invocation produce byte-identical artifacts (the module
  docstring's own determinism argument, exercised here rather than just
  asserted in prose).
- ``TestUnconfiguredProvider``: a real, unconfigured provider name raises
  ``ProviderNotConfigured`` (T9's error taxonomy) rather than silently
  falling back to ``MockProvider`` — checked both through the real CLI
  parsing path (``CliRunner``) and by calling the underlying command
  function directly (Typer's ``@app.command()`` returns the function
  unmodified, so this is a legitimate, faster way to assert the exact
  exception type without going through Click's exception-wrapping).

Also covered: the zero-valid-itineraries exit code path (writes nothing),
and the small pure helper functions (``_to_stop_mode``,
``_deterministic_as_of``).
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from flightagent.cli import (
    NO_VALID_ITINERARIES_EXIT_CODE,
    _deterministic_as_of,
    _parse_departure_date,
    _to_stop_mode,
    app,
    run,
)
from flightagent.domain.enums import CabinClass
from flightagent.domain.run import SearchRequest
from flightagent.providers.errors import ProviderConfigError, ProviderNotConfigured

runner = CliRunner()

_TARGET_ARGS = [
    "run",
    "--origin",
    "AMS",
    "--dest",
    "DEL",
    "--date",
    "2027-07-17",
    "--max-stops",
    "1",
    "--provider",
    "mock",
]


@pytest.fixture
def isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run the CLI from an isolated directory so ``out/...`` (relative to
    cwd, per ``config/defaults.toml``'s ``[output]`` table) never touches
    the real repo's ``out/`` directory from inside the test suite."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestTargetInvocation:
    """The literal Phase 2 exit criterion, exercised via the real CLI
    parsing path (``CliRunner`` drives the actual Typer/Click argument
    parser, not a hand-built ``SearchRequest``)."""

    def test_target_command_exits_zero_and_writes_both_artifacts(
        self, isolated_cwd: Path
    ) -> None:
        result = runner.invoke(app, _TARGET_ARGS)

        assert result.exit_code == 0, result.output
        report_path = isolated_cwd / "out" / "flight_report_2027-07-17.md"
        results_path = isolated_cwd / "out" / "flight_results_2027-07-17.json"
        assert report_path.is_file()
        assert results_path.is_file()

        # Sanity on content, not just existence -- the banner and the
        # data_source field are the two CRITICAL master-plan S8.6 markers.
        assert "SYNTHETIC DATA" in report_path.read_text(encoding="utf-8")
        results = json.loads(results_path.read_text(encoding="utf-8"))
        assert results["data_source"] == "mock"
        assert results["accepted_count"] >= 1

    def test_two_runs_produce_byte_identical_artifacts(self, isolated_cwd: Path) -> None:
        """The exact property the phase's own exit criterion demands:
        running the identical command twice in a row must not change a
        single byte of either artifact -- see ``cli.py``'s module
        docstring for why a wall-clock timestamp would have broken this.
        """
        report_path = isolated_cwd / "out" / "flight_report_2027-07-17.md"
        results_path = isolated_cwd / "out" / "flight_results_2027-07-17.json"

        first = runner.invoke(app, _TARGET_ARGS)
        assert first.exit_code == 0, first.output
        report_bytes_1 = report_path.read_bytes()
        results_bytes_1 = results_path.read_bytes()

        second = runner.invoke(app, _TARGET_ARGS)
        assert second.exit_code == 0, second.output
        report_bytes_2 = report_path.read_bytes()
        results_bytes_2 = results_path.read_bytes()

        assert report_bytes_1 == report_bytes_2
        assert results_bytes_1 == results_bytes_2
        # stdout is deterministic too (no wall-clock content) -- not
        # required by the exit criterion, but cheap to prove and it is
        # exactly what a human diffing two terminal sessions would expect.
        # Deliberately ``.stdout``, not ``.output``: since the Phase 3 fix
        # wired ``setup_logging`` into the CLI's own ``@app.callback()``,
        # ``.output`` also carries the structured JSON log lines this
        # command now emits on stderr, and those lines legitimately carry
        # a real wall-clock ``ts`` field (that is the entire point of a
        # log timestamp) -- so the combined stream is NOT expected to be
        # byte-identical across two runs, only the plain-text stdout a
        # user actually reads is.
        assert first.stdout == second.stdout


class TestUnconfiguredProvider:
    """D6 / T9: a provider other than ``mock`` must raise
    ``ProviderNotConfigured`` and must never silently search with the mock
    provider instead."""

    def test_cli_runner_reports_nonzero_exit_and_the_right_exception(
        self, isolated_cwd: Path
    ) -> None:
        args = [*_TARGET_ARGS[:-1], "amadeus"]  # swap --provider mock -> amadeus
        result = runner.invoke(app, args)

        assert result.exit_code != 0
        assert isinstance(result.exception, ProviderNotConfigured)
        assert isinstance(result.exception, ProviderConfigError)  # T9 taxonomy check
        # And it must not have written anything -- a failed provider
        # resolution must not leave a stale/partial report behind.
        assert not (isolated_cwd / "out").exists()

    def test_calling_the_command_function_directly_raises_provider_not_configured(self) -> None:
        """``@app.command()`` returns the undecorated function (Typer's
        own documented behaviour), so this bypasses Click's argument
        parsing and exception wrapping entirely -- the sharpest possible
        assertion that the RIGHT exception type is raised, not just that
        *something* made the CLI exit nonzero.
        """
        with pytest.raises(ProviderNotConfigured) as exc_info:
            run(
                origin="AMS",
                dest="DEL",
                date_str="2027-07-17",
                max_stops=1,
                provider="duffel",
            )
        assert exc_info.value.provider == "duffel"

    def test_mock_provider_still_works_when_called_directly(
        self, isolated_cwd: Path
    ) -> None:
        """The mirror case: ``--provider mock`` must NOT raise, proving
        the previous test's exception is about the provider NAME, not
        about calling ``run`` directly in general."""
        run(origin="AMS", dest="DEL", date_str="2027-07-17", max_stops=1, provider="mock")
        assert (isolated_cwd / "out" / "flight_report_2027-07-17.md").is_file()


class TestZeroValidItinerariesExitCode:
    """Exit-code discipline: zero valid itineraries is a nonzero exit and
    writes NOTHING to ``out/`` -- never a silently-empty "successful"
    report (task brief, anticipating Phase 4's finding-0.5 contract).
    """

    def test_provider_returning_no_offers_exits_nonzero_and_writes_nothing(
        self, isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Simulate a NO_OFFERS provider response without needing a second
        # real provider -- MockProvider's programmatic mode calls
        # generate_offers() internally; forcing it to return () exercises
        # exactly the "zero offers survive to validation" branch.
        monkeypatch.setattr(
            "flightagent.providers.mock.provider.generate_offers", lambda request: ()
        )

        result = runner.invoke(app, _TARGET_ARGS)

        assert result.exit_code == NO_VALID_ITINERARIES_EXIT_CODE
        assert "0 valid itineraries" in result.output
        assert "No report written" in result.output
        assert not (isolated_cwd / "out").exists()


class TestHelperFunctions:
    def test_to_stop_mode_accepts_zero_and_one(self) -> None:
        assert _to_stop_mode(0) == 0
        assert _to_stop_mode(1) == 1

    def test_to_stop_mode_rejects_anything_else(self) -> None:
        with pytest.raises(typer.BadParameter):
            _to_stop_mode(2)

    def test_parse_departure_date_accepts_iso_format(self) -> None:
        assert _parse_departure_date("2027-07-17") == date(2027, 7, 17)

    def test_parse_departure_date_rejects_malformed_string(self) -> None:
        with pytest.raises(typer.BadParameter):
            _parse_departure_date("17/07/2027")

    def test_deterministic_as_of_is_stable_for_identical_requests(self) -> None:
        request_a = SearchRequest(
            origin="AMS",
            destination="DEL",
            departure_date=date(2027, 7, 17),
            cabin=CabinClass.ECONOMY,
            max_stops=1,
            adults=1,
            currency="EUR",
            layover_min=timedelta(minutes=180),
            layover_max=timedelta(minutes=360),
        )
        request_b = SearchRequest(**request_a.model_dump())

        assert _deterministic_as_of(request_a) == _deterministic_as_of(request_b)

    def test_deterministic_as_of_falls_strictly_before_departure(self) -> None:
        request = SearchRequest(
            origin="AMS",
            destination="DEL",
            departure_date=date(2027, 7, 17),
            cabin=CabinClass.ECONOMY,
            max_stops=1,
            adults=1,
            currency="EUR",
            layover_min=timedelta(minutes=180),
            layover_max=timedelta(minutes=360),
        )
        as_of = _deterministic_as_of(request)
        assert as_of.date() < request.departure_date
