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
    ALL_DESTINATIONS_FAILED_EXIT_CODE,
    NO_VALID_ITINERARIES_EXIT_CODE,
    _deterministic_as_of,
    _parse_departure_date,
    _to_stop_mode,
    app,
    run,
)
from flightagent.domain.enums import CabinClass
from flightagent.domain.run import SearchRequest
from flightagent.providers.errors import (
    ProviderConfigError,
    ProviderNotConfigured,
    ProviderTimeoutError,
)
from tests.support.instrumented_provider import Fail, InstrumentedProvider, Succeed

runner = CliRunner()

# The 8 registered destinations, per airports.registry -- matching
# tests/unit/test_retry.py's own constant, so a script keyed by this tuple
# covers every task the planner builds for a single origin.
_ALL_8_DESTINATIONS = ("DEL", "BOM", "BLR", "HYD", "MAA", "CCU", "LKO", "VNS")

_ALL_DESTINATIONS_ARGS = [
    "run",
    "--origin",
    "AMS",
    "--date",
    "2027-07-17",
    "--max-stops",
    "1",
    "--all-destinations",
]

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


def _parse_log_lines(stderr_text: str) -> list[dict[str, object]]:
    """Every non-empty line of ``stderr_text`` parsed as one structured log
    record -- ``observability.logging.RedactingJsonFormatter`` emits exactly
    one JSON object per line (same helper as ``test_retry.py``'s own, kept
    local here rather than shared, matching this test suite's existing
    per-file-helper convention).
    """
    return [json.loads(line) for line in stderr_text.splitlines() if line.strip()]


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


class TestRunArtifactDirectory:
    """T45: alongside the two D15 fixed-path artifacts, a CLI run also
    writes a copy under ``data/runs/<run_id>/`` -- individually addressable
    by ``run_id``, independent of the fixed-path pair. This class proves
    three things: the per-run copy exists and matches the D15 content; two
    separate invocations land in two DIFFERENT ``run_id`` directories; and
    the existing D15 byte-identical-artifacts regression property is
    untouched by any of this.
    """

    def test_single_dest_run_writes_both_d15_artifacts_and_a_per_run_copy(
        self, isolated_cwd: Path
    ) -> None:
        result = runner.invoke(app, _TARGET_ARGS)
        assert result.exit_code == 0, result.output

        report_path = isolated_cwd / "out" / "flight_report_2027-07-17.md"
        results_path = isolated_cwd / "out" / "flight_results_2027-07-17.json"
        assert report_path.is_file()
        assert results_path.is_file()

        runs_dir = isolated_cwd / "data" / "runs"
        run_dirs = list(runs_dir.iterdir())
        assert len(run_dirs) == 1
        run_report = run_dirs[0] / "report.md"
        run_results = run_dirs[0] / "results.json"
        assert run_report.is_file()
        assert run_results.is_file()
        assert run_report.read_text(encoding="utf-8") == report_path.read_text(encoding="utf-8")
        assert json.loads(run_results.read_text(encoding="utf-8")) == json.loads(
            results_path.read_text(encoding="utf-8")
        )

    def test_two_separate_invocations_get_two_different_run_id_directories(
        self, isolated_cwd: Path
    ) -> None:
        first = runner.invoke(app, _TARGET_ARGS)
        assert first.exit_code == 0, first.output
        second = runner.invoke(app, _TARGET_ARGS)
        assert second.exit_code == 0, second.output

        runs_dir = isolated_cwd / "data" / "runs"
        run_dirs = sorted(p.name for p in runs_dir.iterdir())
        assert len(run_dirs) == 2
        assert run_dirs[0] != run_dirs[1]

        # Content is deterministic (D15 / finding 0.3) even though the two
        # run_id directories that hold it are not.
        contents = {
            (runs_dir / name / "report.md").read_text(encoding="utf-8") for name in run_dirs
        }
        assert len(contents) == 1

    def test_d15_fixed_path_artifacts_stay_byte_identical_across_two_runs(
        self, isolated_cwd: Path
    ) -> None:
        """The existing D15 regression property
        (``TestTargetInvocation.test_two_runs_produce_byte_identical_artifacts``)
        must survive T45's addition untouched -- reasserted directly here,
        colocated with the new feature, rather than trusted by proximity.
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
        # stdout must also stay byte-identical -- T45 must never print the
        # (necessarily varying) run_id to stdout, only write it to disk.
        assert first.stdout == second.stdout

    def test_all_destinations_run_also_writes_a_per_run_copy_keyed_by_run_id(
        self, isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = InstrumentedProvider(
            scripts={destination: [Succeed(offer_count=2)] for destination in _ALL_8_DESTINATIONS}
        )
        monkeypatch.setattr("flightagent.cli._build_provider", lambda name: provider)

        result = runner.invoke(app, _ALL_DESTINATIONS_ARGS)
        assert result.exit_code == 0, result.output

        runs_dir = isolated_cwd / "data" / "runs"
        run_dirs = list(runs_dir.iterdir())
        assert len(run_dirs) == 1
        assert (run_dirs[0] / "report.md").is_file()
        assert (run_dirs[0] / "results.json").is_file()

        results_path = isolated_cwd / "out" / "flight_results_2027-07-17.json"
        fixed_results = json.loads(results_path.read_text(encoding="utf-8"))
        run_results = json.loads((run_dirs[0] / "results.json").read_text(encoding="utf-8"))
        assert run_results == fixed_results


class TestCacheWiring:
    """Phase 7's own literal completion bar (master plan, closing the dual
    verify pass's CRITICAL finding that ``persistence.cache_repo.CacheRepository``
    was built in T43/T44 but never called by anything): "second identical
    run issues 0 provider calls, logs cache_hit for all keys."

    ``InstrumentedProvider.call_log`` is asserted directly rather than via
    script exhaustion -- a single-``Succeed`` script per destination
    replays its last step forever (see that class's own docstring), so an
    un-cached second run would NOT raise or fail on its own; it would just
    silently make 16 more live calls. Only counting ``call_log`` catches
    that regression.
    """

    def test_second_identical_run_makes_zero_provider_calls(
        self, isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = InstrumentedProvider(
            scripts={destination: [Succeed(offer_count=2)] for destination in _ALL_8_DESTINATIONS}
        )
        monkeypatch.setattr("flightagent.cli._build_provider", lambda name: provider)

        first = runner.invoke(app, _ALL_DESTINATIONS_ARGS)
        assert first.exit_code == 0, first.output
        # 8 destinations x 2 modes (direct + one-stop, T29) = 16 live calls.
        calls_after_first_run = len(provider.call_log)
        assert calls_after_first_run == 16

        second = runner.invoke(app, _ALL_DESTINATIONS_ARGS)
        assert second.exit_code == 0, second.output
        assert len(provider.call_log) == calls_after_first_run, (
            "second identical run must hit the cache for every task and make "
            "zero additional provider calls"
        )

        cache_db_path = isolated_cwd / "cache" / "flightagent.sqlite3"
        assert cache_db_path.is_file()

        # The cache-hit path must reconstruct the exact same offers a live
        # call would have -- not just "some" report, the byte-identical one.
        results_path = isolated_cwd / "out" / "flight_results_2027-07-17.json"
        first_results = json.loads(results_path.read_text(encoding="utf-8"))
        assert first.stdout == second.stdout
        assert json.loads(results_path.read_text(encoding="utf-8")) == first_results

    def test_second_run_cache_hits_are_logged(
        self, isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = InstrumentedProvider(
            scripts={destination: [Succeed(offer_count=2)] for destination in _ALL_8_DESTINATIONS}
        )
        monkeypatch.setattr("flightagent.cli._build_provider", lambda name: provider)

        first = runner.invoke(app, _ALL_DESTINATIONS_ARGS)
        assert first.exit_code == 0, first.output

        second = runner.invoke(app, _ALL_DESTINATIONS_ARGS)
        assert second.exit_code == 0, second.output

        cache_hit_lines = [
            record
            for record in _parse_log_lines(second.stderr)
            if record.get("event") == "cache.hit" and record.get("layer") == "raw"
        ]
        assert len(cache_hit_lines) == 16


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


class TestAllDestinationsCliValidation:
    """T27: exactly one of ``--dest``/``--all-destinations`` must be given
    -- both, or neither, is a ``typer.BadParameter`` usage error (exit code
    2), never a silent ambiguity resolved in either flag's favour.
    """

    def test_both_dest_and_all_destinations_is_rejected(self, isolated_cwd: Path) -> None:
        args = [*_ALL_DESTINATIONS_ARGS, "--dest", "DEL"]
        result = runner.invoke(app, args)

        assert result.exit_code == 2, result.output
        assert "mutually exclusive" in result.output
        assert not (isolated_cwd / "out").exists()

    def test_neither_dest_nor_all_destinations_is_rejected(self, isolated_cwd: Path) -> None:
        args = [arg for arg in _ALL_DESTINATIONS_ARGS if arg != "--all-destinations"]
        result = runner.invoke(app, args)

        assert result.exit_code == 2, result.output
        assert "required" in result.output
        assert not (isolated_cwd / "out").exists()


class TestAllDestinationsSucceeds:
    """T27: ``--all-destinations`` fans ``--origin`` out across every
    registry destination, in BOTH ``max_stops`` modes (T29, Addendum 1).
    Proved here with T25's own ``InstrumentedProvider`` test double,
    scripted to succeed everywhere, via ``_build_provider`` monkeypatched to
    hand back that instrumented instance for ``--provider mock`` -- exactly
    the same substitution pattern ``test_retry.py`` uses for the executor,
    applied one layer up at the CLI.
    """

    def _succeeding_provider(self) -> InstrumentedProvider:
        # offer_count=2 exactly matches how many offers MockProvider's own
        # generator returns for these request shapes (verified against the
        # real target invocation) -- this double's offers are the SAME
        # deterministic ``generate_offers`` output, not a hand-rolled stub,
        # so "succeeds for all 8 destinations" here means the identical
        # thing it would mean running the real mock provider directly.
        # VNS's OWN direct-mode (max_stops=0) call is the one exception:
        # T29's generator fix makes VNS legitimately return zero direct
        # offers (NOT_AVAILABLE, D10) -- InstrumentedProvider's own
        # _offers_for helper honours that instead of fabricating 2 offers
        # anyway, so VNS's direct task ends NO_OFFERS while its one-stop
        # task still succeeds normally. NO_OFFERS is not an error state
        # (domain.run._ERROR_STATES), so this does not turn VNS into a
        # "Failed Search" below.
        return InstrumentedProvider(
            scripts={destination: [Succeed(offer_count=2)] for destination in _ALL_8_DESTINATIONS}
        )

    def test_issues_exactly_16_provider_calls_and_writes_both_artifacts(
        self, isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = self._succeeding_provider()
        monkeypatch.setattr("flightagent.cli._build_provider", lambda name: provider)

        result = runner.invoke(app, _ALL_DESTINATIONS_ARGS)

        assert result.exit_code == 0, result.output
        # T29: dual-mode search issues 2 calls per destination (direct AND
        # one-stop), never just 1 -- 8 destinations x 2 modes = 16.
        assert len(provider.call_log) == 16
        assert {record.destination for record in provider.call_log} == set(_ALL_8_DESTINATIONS)
        assert {record.origin for record in provider.call_log} == {"AMS"}
        assert {record.max_stops for record in provider.call_log} == {0, 1}
        for destination in _ALL_8_DESTINATIONS:
            destination_calls = [r for r in provider.call_log if r.destination == destination]
            modes_called = sorted(record.max_stops for record in destination_calls)
            assert modes_called == [0, 1], f"{destination} should be searched at both modes"

        report_path = isolated_cwd / "out" / "flight_report_2027-07-17.md"
        results_path = isolated_cwd / "out" / "flight_results_2027-07-17.json"
        assert report_path.is_file()
        assert results_path.is_file()

        report_text = report_path.read_text(encoding="utf-8")
        assert "SYNTHETIC DATA" in report_text
        assert "## Recommended Flight" in report_text
        # Every destination succeeded at >=1 mode -- T26's Failed Searches
        # section must stay absent, not render an empty heading. VNS's
        # direct-mode NO_OFFERS (see _succeeding_provider's own docstring)
        # is not an error state, so it does not populate this section.
        assert "Failed Searches" not in report_text

        results = json.loads(results_path.read_text(encoding="utf-8"))
        assert results["data_source"] == "mock"
        assert results["accepted_count"] >= 1
        assert results["failed_searches"] == []
        represented_destinations = {item["destination"] for item in results["top_itineraries"]}
        assert represented_destinations
        assert represented_destinations <= set(_ALL_8_DESTINATIONS)


class TestTruncatedDestinationEmitsWarningLog:
    """Non-blocking gap mitigation (Fix 2): when the global top-N cut
    (``config.output.top_n_global``, default 10) discards every accepted
    itinerary for a destination that DID have >=1 valid, accepted
    itinerary, that destination silently vanishes from the report -- this
    does not fix that (Phase 5/6 scope), it only makes sure the drop is
    OBSERVABLE via a ``EventName.RANK_DESTINATION_DROPPED`` WARNING log
    line.

    Reuses ``TestAllDestinationsSucceeds``'s own scripted double
    (``Succeed(offer_count=2)`` for all 8 destinations, the SAME
    deterministic ``generate_offers`` output every other test in this class
    uses) -- verified empirically (not asserted blind), now under T29's
    dual-mode search, to produce 25 accepted itineraries total (roughly
    double Phase 4's single-mode 11, since every destination except VNS
    now contributes from BOTH a direct AND a one-stop search), 15 more than
    ``top_n_global``'s default of 10, with destination ``VNS`` entirely cut
    from the truncated/shown set -- VNS's direct search legitimately
    returns zero offers (T29's generator fix), leaving it with only its
    one-stop search's single accepted itinerary, too little to make the
    cut once every other destination's (now doubled) itineraries compete
    for the same 10 slots.
    """

    def _succeeding_provider(self) -> InstrumentedProvider:
        return InstrumentedProvider(
            scripts={destination: [Succeed(offer_count=2)] for destination in _ALL_8_DESTINATIONS}
        )

    def test_destination_entirely_cut_by_truncation_logs_a_warning(
        self, isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = self._succeeding_provider()
        monkeypatch.setattr("flightagent.cli._build_provider", lambda name: provider)

        result = runner.invoke(app, _ALL_DESTINATIONS_ARGS)

        assert result.exit_code == 0, result.output

        results_path = isolated_cwd / "out" / "flight_results_2027-07-17.json"
        results = json.loads(results_path.read_text(encoding="utf-8"))
        # Ground truth (T29, dual-mode search): 25 accepted total, only 10
        # shown -- VNS has an accepted itinerary (it is NOT a failed
        # search) but is absent from every shown destination.
        assert results["accepted_count"] == 25
        assert len(results["top_itineraries"]) == 10
        shown_destinations = {item["destination"] for item in results["top_itineraries"]}
        assert "VNS" not in shown_destinations
        failed_destinations = {item["destination"] for item in results["failed_searches"]}
        assert "VNS" not in failed_destinations

        records = _parse_log_lines(result.stderr)
        dropped_records = [
            record for record in records if record.get("event") == "rank.destination_dropped"
        ]
        assert len(dropped_records) == 1
        record = dropped_records[0]
        assert record["level"] == "warning"
        assert record["destinations"] == ["VNS"]
        assert record["total_accepted"] == 25
        assert record["shown_count"] == 10

    def test_no_warning_when_nothing_is_truncated(
        self, isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The mirror case: a run where every destination fits inside
        ``top_n_global`` untruncated must never emit the warning -- it is
        not a generic "truncation happened" signal, only a "a destination
        was entirely erased by it" one.

        T29: dual-mode search roughly doubles accepted-itinerary volume (a
        direct AND a one-stop search per destination), so even
        ``offer_count=1`` everywhere produces 15 accepted itineraries --
        already past the default ``top_n_global`` of 10 (verified
        empirically). ``top_n_global`` is bumped via the env var config
        layer (``config.loader``'s layer 3) to comfortably exceed that, so
        this test genuinely proves the "nothing dropped" case rather than
        accidentally re-exercising the truncation path above.
        """
        monkeypatch.setenv("FLIGHTAGENT__OUTPUT__TOP_N_GLOBAL", "100")
        provider = InstrumentedProvider(
            scripts={destination: [Succeed(offer_count=1)] for destination in _ALL_8_DESTINATIONS}
        )
        monkeypatch.setattr("flightagent.cli._build_provider", lambda name: provider)

        result = runner.invoke(app, _ALL_DESTINATIONS_ARGS)

        assert result.exit_code == 0, result.output
        results_path = isolated_cwd / "out" / "flight_results_2027-07-17.json"
        results = json.loads(results_path.read_text(encoding="utf-8"))
        # Sanity: this really is the untruncated case, not an accident of
        # top_itineraries happening to still be <= 10.
        assert results["accepted_count"] == len(results["top_itineraries"])

        records = _parse_log_lines(result.stderr)
        dropped_records = [
            record for record in records if record.get("event") == "rank.destination_dropped"
        ]
        assert dropped_records == []


class TestAllDestinationsFailed:
    """T27 / finding 0.5: every task erroring must produce ``RunStatus.FAILED``
    -- a distinct exit code and distinct wording from the NO_RESULTS path,
    never the spec's misleading layover-rule string.
    """

    def test_every_destination_erroring_exits_with_the_failed_code_and_distinct_message(
        self, isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = InstrumentedProvider(
            scripts={
                destination: [
                    Fail(
                        ProviderTimeoutError(
                            f"{destination} unreachable", provider="instrumented"
                        )
                    )
                ]
                for destination in _ALL_8_DESTINATIONS
            }
        )
        monkeypatch.setattr("flightagent.cli._build_provider", lambda name: provider)

        result = runner.invoke(app, _ALL_DESTINATIONS_ARGS)

        assert result.exit_code == ALL_DESTINATIONS_FAILED_EXIT_CODE, result.output
        assert result.exit_code != NO_VALID_ITINERARIES_EXIT_CODE
        assert "3–6 hour layover rule" not in result.output
        assert "unreachable" in result.output or "failed" in result.output.lower()
        assert not (isolated_cwd / "out").exists()
