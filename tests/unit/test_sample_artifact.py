"""Tests for the committed B-26 sample artifact (samples/).

DECISIONS.md's restated B-26: "the committed sample's recommended
AMS->DEL one-stop has layover_minutes == 240; the test reads the
committed JSON." This file IS that test.

Reads ONLY the two files already committed under samples/ -- never
constructs a MockProvider, never calls run_single_destination_pipeline,
never regenerates anything (scripts/generate_sample.py owns that, run by
hand). Kept deliberately provider-free so this test stays fast and can
never flake on RNG/seed/timing, unlike a test that re-ran the pipeline.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema

from flightagent.reporting.markdown import SYNTHETIC_DATA_BANNER

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SAMPLES_DIR = _REPO_ROOT / "samples"
_REPORT_PATH = _SAMPLES_DIR / "flight_report_2027-07-17.md"
_RESULTS_PATH = _SAMPLES_DIR / "flight_results_2027-07-17.json"
_SCHEMA_PATH = _REPO_ROOT / "config" / "results.schema.json"


def _load_results() -> dict[str, Any]:
    return json.loads(_RESULTS_PATH.read_text(encoding="utf-8"))


class TestSampleArtifactFilesAreCommitted:
    def test_both_files_exist(self) -> None:
        assert _REPORT_PATH.is_file()
        assert _RESULTS_PATH.is_file()


class TestSampleArtifactValidatesAgainstSchema:
    def test_json_conforms_to_results_schema(self) -> None:
        schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
        jsonschema.validate(_load_results(), schema)


class TestB26RecommendedItineraryLayover:
    """The restated B-26 predicate, verbatim."""

    def test_recommended_itinerary_is_ams_del_one_stop_with_240_minute_layover(self) -> None:
        document = _load_results()
        assert document["accepted_count"] >= 1
        recommended = document["top_itineraries"][0]

        assert recommended["origin"] == "AMS"
        assert recommended["destination"] == "DEL"
        assert recommended["stop_count"] == 1
        assert recommended["layover_minutes"] == 240

    def test_data_source_is_structurally_mock(self) -> None:
        assert _load_results()["data_source"] == "mock"

    def test_booking_url_is_present_and_passes_the_mock_safety_gate(self) -> None:
        recommended = _load_results()["top_itineraries"][0]
        assert recommended["booking_url"] is not None
        assert recommended["booking_url_valid"] is True


class TestSampleMarkdownReport:
    def test_synthetic_data_banner_is_the_very_first_line(self) -> None:
        text = _REPORT_PATH.read_text(encoding="utf-8")
        assert text.splitlines()[0] == SYNTHETIC_DATA_BANNER
