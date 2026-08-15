"""End-to-end smoke test (T17) -- codifies, as a test CI runs forever, the
manual verification T16 already did by hand: the literal Phase 2 exit
criterion is a single ``flightagent run --origin AMS --dest DEL --date
2027-07-17 --max-stops 1 --provider mock`` invocation that exits ``0``,
writes both v1 report artifacts, and -- run twice -- produces
byte-identical output.

This module invokes the CLI in-process via Typer's own ``CliRunner``
(faster than shelling out, no subprocess dependency, and Typer's test
utilities are already used by ``tests/unit/test_cli.py``). Every invocation
runs from a ``monkeypatch.chdir``-ed temporary directory: both output
filenames in ``config/defaults.toml``'s ``[output]`` table
(``out/flight_report_2027-07-17.md``, ``out/flight_results_2027-07-17.json``)
are relative to the process's current working directory, so this is what
keeps the test from ever touching (or racing with) a developer's real
``out/`` directory or another test run's own tree.

The byte-identical assertion is the actual codified form of the phase's
exit criterion -- it is deliberately NOT weakened to "structurally similar"
or "same itinerary count" anywhere in this file. Two runs of the identical
command, each into its OWN separate temp directory (proving the equality
isn't an artifact of one run's files simply never having been touched by a
second invocation), must produce files whose bytes compare equal outright.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from typer.testing import CliRunner, Result

from flightagent.cli import app
from flightagent.reporting.markdown import SYNTHETIC_DATA_BANNER

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

_REPORT_FILENAME = "flight_report_2027-07-17.md"
_RESULTS_FILENAME = "flight_results_2027-07-17.json"

_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "config" / "results.schema.json"


def _invoke_in(directory: Path, monkeypatch: pytest.MonkeyPatch) -> Result:
    """Run the exact target invocation with cwd pinned to ``directory``.

    Isolation, not convenience: every artifact path in
    ``config/defaults.toml`` is relative to cwd, so pinning cwd per call is
    what keeps two invocations -- even within the same test -- from ever
    writing into each other's directory or the real repo's ``out/``.
    """
    monkeypatch.chdir(directory)
    return runner.invoke(app, _TARGET_ARGS)


class TestE2EMockRunArtifacts:
    """The target invocation, exercised through the real CLI parsing path,
    with every artifact checked: existence, non-emptiness, JSON Schema
    validity, and the two CRITICAL Markdown markers (master plan S8.6/S8.8:
    the synthetic-data banner and the Recommended Flight heading)."""

    def test_exit_code_zero(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        result = _invoke_in(tmp_path, monkeypatch)
        assert result.exit_code == 0, result.output

    def test_both_artifact_files_exist_and_are_non_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = _invoke_in(tmp_path, monkeypatch)
        assert result.exit_code == 0, result.output

        report_path = tmp_path / "out" / _REPORT_FILENAME
        results_path = tmp_path / "out" / _RESULTS_FILENAME

        assert report_path.is_file()
        assert results_path.is_file()
        assert report_path.stat().st_size > 0
        assert results_path.stat().st_size > 0

    def test_json_artifact_parses_and_validates_against_results_schema(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = _invoke_in(tmp_path, monkeypatch)
        assert result.exit_code == 0, result.output

        results_path = tmp_path / "out" / _RESULTS_FILENAME
        results_text = results_path.read_text(encoding="utf-8")
        results_doc = json.loads(results_text)  # raises on parse failure

        schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
        jsonschema.validate(instance=results_doc, schema=schema)  # raises on schema failure

    def test_markdown_artifact_has_synthetic_banner_and_recommended_flight_heading(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = _invoke_in(tmp_path, monkeypatch)
        assert result.exit_code == 0, result.output

        report_text = (tmp_path / "out" / _REPORT_FILENAME).read_text(encoding="utf-8")

        assert SYNTHETIC_DATA_BANNER in report_text
        assert "## Recommended Flight" in report_text


class TestE2EByteIdenticalDeterminism:
    """The phase's own exit criterion, codified literally: running the
    identical command twice -- each time into its OWN separate temp
    directory -- must produce artifacts whose bytes compare EQUAL, not just
    similar. This must never be weakened to a structural or count-based
    comparison; see this module's docstring."""

    def test_two_separate_runs_produce_byte_identical_artifacts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        first_dir = tmp_path / "run1"
        second_dir = tmp_path / "run2"
        first_dir.mkdir()
        second_dir.mkdir()

        first_result = _invoke_in(first_dir, monkeypatch)
        assert first_result.exit_code == 0, first_result.output

        second_result = _invoke_in(second_dir, monkeypatch)
        assert second_result.exit_code == 0, second_result.output

        first_report_bytes = (first_dir / "out" / _REPORT_FILENAME).read_bytes()
        second_report_bytes = (second_dir / "out" / _REPORT_FILENAME).read_bytes()
        first_results_bytes = (first_dir / "out" / _RESULTS_FILENAME).read_bytes()
        second_results_bytes = (second_dir / "out" / _RESULTS_FILENAME).read_bytes()

        assert first_report_bytes == second_report_bytes
        assert first_results_bytes == second_results_bytes
