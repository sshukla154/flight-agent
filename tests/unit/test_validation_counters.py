"""Tests for the rejection-counter aggregator (T19) and its CLI wiring.

Master plan S7: ``EventName.VALIDATE_COMPLETED`` carries ``accepted_count``
and a ``rejection_counts`` map (``observability/events.py::ValidateCompletedFields``,
built in Phase 1). Nothing called ``log_event(EventName.VALIDATE_COMPLETED, ...)``
anywhere until this task -- the counting logic
(``validation.engine.summarize_validation_results``) and the actual
``cli.py`` wiring are both new here.

``TestSummarizeValidationResults`` exercises the aggregator directly against
hand-built ``ValidationResult``/``Rejection`` objects -- no full
``NormalizedItinerary``/provider/mock machinery needed, since the aggregator
only ever looks at ``ValidationResult.is_valid`` and ``.rejections``.

``TestValidateCompletedWiredIntoCli`` is the end-to-end proof the task brief
explicitly demands: a REAL CLI invocation (via ``CliRunner``, the exact
Phase 2 target command), with ``observability.logging.setup_logging``
pointed at a captured stream, asserting a real ``validate.completed`` JSON
log line appears carrying real ``accepted_count``/``rejection_counts``
values -- not just a unit test of the aggregator in isolation.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flightagent.cli import app
from flightagent.domain.enums import RejectionCode
from flightagent.domain.validation import Rejection, ValidationResult
from flightagent.observability.logging import setup_logging
from flightagent.validation.engine import summarize_validation_results

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


def _rejection(code: RejectionCode, *, rule_id: str = "rule") -> Rejection:
    return Rejection(
        code=code,
        message=f"{code.value} failed",
        observed="observed",
        expected="expected",
        rule_id=rule_id,
    )


def _valid(itinerary_id: str) -> ValidationResult:
    return ValidationResult(itinerary_id=itinerary_id, rejections=())


def _invalid(itinerary_id: str, *codes: RejectionCode) -> ValidationResult:
    return ValidationResult(
        itinerary_id=itinerary_id,
        rejections=tuple(_rejection(code) for code in codes),
    )


class TestSummarizeValidationResults:
    def test_accepted_count_matches_exact_number_of_valid_results_in_a_mixed_batch(self) -> None:
        results = [
            _valid("v1"),
            _invalid("r1", RejectionCode.TOO_MANY_STOPS),
            _valid("v2"),
            _invalid("r2", RejectionCode.LAYOVER_TOO_SHORT),
            _valid("v3"),
        ]

        accepted_count, rejection_counts = summarize_validation_results(results)

        assert accepted_count == 3
        assert rejection_counts == {
            RejectionCode.TOO_MANY_STOPS.value: 1,
            RejectionCode.LAYOVER_TOO_SHORT.value: 1,
        }

    def test_rejection_counts_tallies_multiple_itineraries_failing_the_same_code(self) -> None:
        """Two DIFFERENT itineraries both failing LAYOVER_TOO_LONG must
        tally to 2 against that one counter, not 1 -- the aggregator must
        not treat "this code already appeared" as done."""
        results = [
            _invalid("r1", RejectionCode.LAYOVER_TOO_LONG),
            _invalid("r2", RejectionCode.LAYOVER_TOO_LONG),
            _valid("v1"),
        ]

        accepted_count, rejection_counts = summarize_validation_results(results)

        assert accepted_count == 1
        assert rejection_counts == {RejectionCode.LAYOVER_TOO_LONG.value: 2}

    def test_rejection_counts_tallies_different_codes_independently(self) -> None:
        results = [
            _invalid("r1", RejectionCode.ORIGIN_MISMATCH),
            _invalid("r2", RejectionCode.DESTINATION_MISMATCH),
            _invalid("r3", RejectionCode.DATE_MISMATCH),
        ]

        accepted_count, rejection_counts = summarize_validation_results(results)

        assert accepted_count == 0
        assert rejection_counts == {
            RejectionCode.ORIGIN_MISMATCH.value: 1,
            RejectionCode.DESTINATION_MISMATCH.value: 1,
            RejectionCode.DATE_MISMATCH.value: 1,
        }

    def test_single_itinerary_with_multiple_simultaneous_rejections_increments_each_counter(
        self,
    ) -> None:
        """One itinerary failing three rules at once (the engine's own
        no-short-circuit contract -- ``test_validator.py::TestEngineAccumulatesAllRejections``)
        must contribute one increment to EACH of the three counters,
        proving this aggregator counts individual ``Rejection``s, not "one
        rejection reason per itinerary". A second itinerary shares exactly
        one of those three codes with the first, plus a fourth of its
        own -- proving cross-itinerary tallying and per-itinerary
        multi-rejection tallying both hold at the same time, not just in
        isolation from each other.
        """
        multiply_rejected = _invalid(
            "multi",
            RejectionCode.TOO_MANY_STOPS,
            RejectionCode.LAYOVER_TOO_SHORT,
            RejectionCode.ORIGIN_MISMATCH,
        )
        also_rejected = _invalid(
            "other",
            RejectionCode.TOO_MANY_STOPS,
            RejectionCode.CABIN_MISMATCH,
        )

        accepted_count, rejection_counts = summarize_validation_results(
            [multiply_rejected, also_rejected]
        )

        assert accepted_count == 0
        assert rejection_counts == {
            RejectionCode.TOO_MANY_STOPS.value: 2,
            RejectionCode.LAYOVER_TOO_SHORT.value: 1,
            RejectionCode.ORIGIN_MISMATCH.value: 1,
            RejectionCode.CABIN_MISMATCH.value: 1,
        }
        # And the multiply-rejected itinerary really did keep all three of
        # its own rejections -- not collapsed down to one.
        assert len(multiply_rejected.rejections) == 3
        assert not multiply_rejected.is_valid

    def test_empty_batch_returns_zero_accepted_and_empty_rejection_counts(self) -> None:
        accepted_count, rejection_counts = summarize_validation_results([])

        assert accepted_count == 0
        assert rejection_counts == {}

    def test_all_valid_batch_has_empty_rejection_counts(self) -> None:
        accepted_count, rejection_counts = summarize_validation_results(
            [_valid("v1"), _valid("v2")]
        )

        assert accepted_count == 2
        assert rejection_counts == {}


class TestValidateCompletedWiredIntoCli:
    """The end-to-end proof: a real ``flightagent run`` invocation must
    emit a real ``validate.completed`` structured log line -- not just
    exercise the aggregator in isolation."""

    def test_cli_run_emits_validate_completed_log_line_with_real_counts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        stream = io.StringIO()
        setup_logging(stream=stream)

        result = runner.invoke(app, _TARGET_ARGS)

        assert result.exit_code == 0, result.output

        log_lines = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
        validate_events = [
            line for line in log_lines if line.get("event") == "validate.completed"
        ]
        assert len(validate_events) == 1, log_lines

        (event,) = validate_events
        assert isinstance(event["accepted_count"], int)
        assert event["accepted_count"] >= 1  # the target invocation always validates >=1 offer
        assert isinstance(event["rejection_counts"], dict)
        # Every value in the rejection histogram is a real positive count,
        # never a placeholder/zero entry the aggregator forgot to prune.
        assert all(isinstance(v, int) and v > 0 for v in event["rejection_counts"].values())

    def test_cli_zero_valid_itineraries_still_emits_validate_completed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """T19's wiring runs unconditionally -- even the zero-valid exit
        path (which writes no report artifacts at all) must still have
        logged the histogram, per the ``cli.py`` docstring's own claim.
        """
        monkeypatch.setattr(
            "flightagent.providers.mock.provider.generate_offers", lambda request: ()
        )
        monkeypatch.chdir(tmp_path)
        stream = io.StringIO()
        setup_logging(stream=stream)

        result = runner.invoke(app, _TARGET_ARGS)

        assert result.exit_code != 0
        log_lines = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
        validate_events = [
            line for line in log_lines if line.get("event") == "validate.completed"
        ]
        assert len(validate_events) == 1
        assert validate_events[0]["accepted_count"] == 0
        assert validate_events[0]["rejection_counts"] == {}
