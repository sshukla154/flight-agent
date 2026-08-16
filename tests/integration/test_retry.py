"""Integration: the retry loop's success path, proved end to end through the
real CLI (T28 -- complementing T25's own unit-level retry tests in
``tests/unit/test_retry.py``, which exercise ``execute_plan`` directly).

Two things this file proves that a unit test calling ``execute_plan``
directly cannot:

1. A destination scripted to fail twice then succeed ends up correctly
   RANKED in the final Markdown/JSON artifacts -- not merely holding
   ``TaskState.OK`` at the executor's own boundary, but surviving all the
   way through normalize/validate/dedup/score/rank/write, and never
   appearing in the Failed Searches section.
2. ``EventName.SEARCH_RETRY`` log lines actually reach stderr through the
   real CLI wiring (``setup_logging(stream=sys.stderr)``, wired in the
   ``@app.callback()``) -- captured and parsed as real emitted JSON lines,
   not merely inferred from ``log_event`` having been called somewhere in
   the source. This is deliberately the same style of proof that originally
   caught Phase 3's logging-wiring gap (nothing attached to the
   ``"flightagent"`` logger, so every ``log_event`` call was silently
   dropped) -- trusting that the call site exists is exactly what let that
   bug ship unnoticed.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flightagent.cli import app
from flightagent.providers.errors import ProviderTimeoutError
from tests.support.instrumented_provider import Fail, InstrumentedProvider, Succeed

runner = CliRunner()

_ORIGIN = "AMS"
_RETRY_DESTINATION = "HYD"

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


@pytest.fixture
def isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def no_real_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Instant backoff -- see ``test_partial_failure.py``'s identical
    fixture docstring for why this doesn't weaken what's being proved."""

    async def _fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)


def _parse_log_lines(stderr_text: str) -> list[dict[str, object]]:
    """Every non-empty line of ``stderr_text`` parsed as one structured
    log record -- ``observability.logging.RedactingJsonFormatter`` emits
    exactly one JSON object per line, so a bare ``json.loads`` per line is
    a faithful parse, not a heuristic scrape.
    """
    return [json.loads(line) for line in stderr_text.splitlines() if line.strip()]


class TestRetrySucceedsEndToEndThroughTheFullCli:
    def test_destination_that_fails_twice_then_succeeds_is_ranked_not_failed(
        self, isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch, no_real_delay: None
    ) -> None:
        provider = InstrumentedProvider(
            scripts={
                _RETRY_DESTINATION: [
                    Fail(
                        ProviderTimeoutError(
                            f"{_RETRY_DESTINATION} timeout 1", provider="instrumented"
                        )
                    ),
                    Fail(
                        ProviderTimeoutError(
                            f"{_RETRY_DESTINATION} timeout 2", provider="instrumented"
                        )
                    ),
                    Succeed(offer_count=2),
                ]
            }
            # Every other destination is left unscripted -- defaults to
            # Succeed(1) on every call (see InstrumentedProvider's own
            # docstring), so this test isolates the ONE mechanic it claims
            # to prove.
        )
        monkeypatch.setattr("flightagent.cli._build_provider", lambda name: provider)

        result = runner.invoke(app, _ALL_DESTINATIONS_ARGS)

        assert result.exit_code == 0, result.output
        # Three calls: fail, fail, succeed -- the retry loop's own attempt
        # count, visible from the outside via the provider's call log.
        assert provider.call_count(_RETRY_DESTINATION) == 3

        results_path = isolated_cwd / "out" / "flight_results_2027-07-17.json"
        report_path = isolated_cwd / "out" / "flight_report_2027-07-17.md"
        assert results_path.is_file()
        assert report_path.is_file()

        results = json.loads(results_path.read_text(encoding="utf-8"))
        # Never marked as a failed search -- the retry succeeded.
        failed_destinations = {item["destination"] for item in results["failed_searches"]}
        assert _RETRY_DESTINATION not in failed_destinations

        # And it IS visible, ranked, all the way through to the final
        # report -- not merely OK at the executor's own boundary.
        ranked_destinations = {item["destination"] for item in results["top_itineraries"]}
        assert _RETRY_DESTINATION in ranked_destinations

        report_text = report_path.read_text(encoding="utf-8")
        assert "Failed Searches" not in report_text

    def test_search_retry_events_actually_reach_stderr(
        self, isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch, no_real_delay: None
    ) -> None:
        """Same scenario, but the assertion is on the REAL emitted log
        lines captured from the CLI invocation's own stderr -- proving
        ``SEARCH_RETRY`` actually fires through the real logging wiring,
        not merely that ``log_event(EventName.SEARCH_RETRY, ...)`` appears
        somewhere in ``executor.py``'s source.
        """
        provider = InstrumentedProvider(
            scripts={
                _RETRY_DESTINATION: [
                    Fail(
                        ProviderTimeoutError(
                            f"{_RETRY_DESTINATION} timeout 1", provider="instrumented"
                        )
                    ),
                    Fail(
                        ProviderTimeoutError(
                            f"{_RETRY_DESTINATION} timeout 2", provider="instrumented"
                        )
                    ),
                    Succeed(offer_count=2),
                ]
            }
        )
        monkeypatch.setattr("flightagent.cli._build_provider", lambda name: provider)

        result = runner.invoke(app, _ALL_DESTINATIONS_ARGS)
        assert result.exit_code == 0, result.output

        records = _parse_log_lines(result.stderr)
        retry_records = [
            record
            for record in records
            if record.get("event") == "search.retry"
            and record.get("destination") == _RETRY_DESTINATION
        ]

        # Exactly 2 retries -- one before attempt 2, one before attempt 3
        # -- matching the "fail, fail, succeed" script exactly.
        assert len(retry_records) == 2
        assert sorted(record["attempt"] for record in retry_records) == [2, 3]
        assert all(record["reason"] == "ProviderTimeoutError" for record in retry_records)
        assert all(record["origin"] == _ORIGIN for record in retry_records)
        assert all(record["provider"] == "instrumented" for record in retry_records)
