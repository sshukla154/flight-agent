"""One-time generator for the committed B-26 sample artifact under samples/.

Run: uv run python scripts/generate_sample.py

DECISIONS.md's restated B-26: "the committed sample's recommended AMS->DEL
one-stop has layover_minutes == 240; the test reads the committed JSON."
This script is the ONLY place that JSON/Markdown pair is produced -- it is
NOT run by CI and NOT imported by tests/unit/test_sample_artifact.py, which
only reads the two committed files this script writes.

Regenerate only when a change to scoring, validation, layover-band, or
report-rendering logic would legitimately change this sample's content
(config/defaults.toml's [layover]/[scoring] tables, reporting/markdown.py,
reporting/json_report.py). Any diff this produces must be human-reviewed
before committing -- the same discipline every other golden file in this
project already follows (DECISIONS.md, "Confirm before Phase 5").

Builds the exact literal Phase 2 target invocation
(`flightagent run --origin AMS --dest DEL --date 2027-07-17 --max-stops 1
--provider mock`) but with MockProvider constructed in FIXTURE-FILE mode
(fixture_path=<ams_del_onestop.json>) instead of PROGRAMMATIC mode, so the
committed sample is pinned to the fixture's hand-authored 240-minute
scenario rather than the mock generator's own (currently 270-minute)
guaranteed offer -- see providers/mock/provider.py's two-mode docstring.
cli.py's own `_build_provider("mock")` never passes fixture_path, which is
exactly why this script exists rather than shelling out to `flightagent run`.
"""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

from flightagent.cli import build_search_request, run_single_destination_pipeline
from flightagent.config.loader import load_config
from flightagent.providers.mock import MockProvider
from flightagent.reporting.writer import write_report_artifacts

_REPO_ROOT = Path(__file__).resolve().parent.parent
_FIXTURE_PATH = (
    _REPO_ROOT
    / "src"
    / "flightagent"
    / "providers"
    / "mock"
    / "fixtures"
    / "ams_del_onestop.json"
)
_SAMPLES_DIR = _REPO_ROOT / "samples"


async def _build_artifacts() -> tuple[str, dict[str, object]]:
    settings = load_config()
    request = build_search_request(
        origin="AMS",
        destination="DEL",
        departure_date=date(2027, 7, 17),
        max_stops=1,
        settings=settings,
    )
    provider = MockProvider(fixture_path=_FIXTURE_PATH)
    result = await run_single_destination_pipeline(
        request, provider_instance=provider, settings=settings
    )
    if result.markdown is None or result.json_document is None:
        raise RuntimeError(
            "fixture-mode search produced zero valid itineraries -- "
            "ams_del_onestop.json or config/defaults.toml's [layover] table changed"
        )
    return result.markdown, result.json_document


def main() -> None:
    markdown, json_document = asyncio.run(_build_artifacts())
    report_path, results_path = write_report_artifacts(
        markdown=markdown,
        json_data=json_document,
        report_path=_SAMPLES_DIR / "flight_report_2027-07-17.md",
        results_path=_SAMPLES_DIR / "flight_results_2027-07-17.json",
    )
    print(f"wrote {report_path} and {results_path}")


if __name__ == "__main__":
    main()
