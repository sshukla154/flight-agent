"""Tests for JSON log formatting, setup, and secret redaction.

`test_log_event_redacts_secret_shaped_field` is the test DECISIONS.md's T6
task explicitly demands: a fake secret in a field named like a secret must
never survive into the emitted line.
"""

import io
import json

import pytest

from flightagent.observability.context import run_context
from flightagent.observability.events import EventName
from flightagent.observability.logging import (
    REDACTED,
    EventValidationError,
    log_event,
    redact,
    setup_logging,
)


def test_log_event_redacts_secret_shaped_field() -> None:
    stream = io.StringIO()
    setup_logging(stream=stream)
    fake_secret = "FAKE_SECRET_DO_NOT_LEAK_9f3c"

    with run_context("test-run-id"):
        log_event(
            EventName.SEARCH_REQUESTED,
            provider="mock",
            origin="AMS",
            destination="DEL",
            max_stops=1,
            attempt=1,
            api_key=fake_secret,
        )

    raw = stream.getvalue()
    lines = raw.strip().splitlines()
    assert len(lines) == 1  # exactly one line per log record

    assert fake_secret not in raw  # the secret never reaches the emitted string, at all
    record = json.loads(lines[0])  # the (only) line is valid JSON
    assert record["api_key"] == REDACTED
    assert record["run_id"] == "test-run-id"
    assert record["event"] == "search.requested"


@pytest.mark.parametrize(
    "key",
    ["Authorization", "api_key", "client_secret", "Access_Token", "X-Api-Key", "password"],
)
def test_redact_masks_every_named_secret_shape(key: str) -> None:
    masked = redact({key: "shhh"})
    assert masked[key] == REDACTED


def test_redact_does_not_touch_ordinary_fields() -> None:
    masked = redact({"origin": "AMS", "offer_count": 37})
    assert masked == {"origin": "AMS", "offer_count": 37}


def test_redact_recurses_into_nested_structures() -> None:
    masked = redact({"headers": {"Authorization": "Bearer xyz"}, "items": [{"api_key": "k"}]})
    assert masked["headers"]["Authorization"] == REDACTED
    assert masked["items"][0]["api_key"] == REDACTED


def test_log_event_emits_exactly_one_line_of_valid_json_with_required_keys() -> None:
    stream = io.StringIO()
    setup_logging(stream=stream)

    with run_context("run-1"):
        log_event(EventName.RUN_STARTED)

    lines = stream.getvalue().strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert {"run_id", "event", "ts", "level", "schema_version"} <= record.keys()
    assert record["event"] == "run.started"
    assert record["run_id"] == "run-1"


def test_log_event_raises_on_missing_required_field() -> None:
    with pytest.raises(EventValidationError):
        log_event(EventName.SEARCH_REQUESTED, provider="mock")  # origin/destination/... missing


def test_setup_logging_is_idempotent_no_duplicate_lines() -> None:
    stream = io.StringIO()
    setup_logging(stream=stream)
    setup_logging(stream=stream)

    with run_context("run-1"):
        log_event(EventName.RUN_STARTED)

    lines = stream.getvalue().strip().splitlines()
    assert len(lines) == 1
