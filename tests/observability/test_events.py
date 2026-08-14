"""Tests for the closed EventName enum and per-event required-field schemas."""

import pytest
from pydantic import ValidationError

from flightagent.observability.events import EVENT_SCHEMAS, EventName


def test_event_schemas_cover_every_closed_event() -> None:
    assert set(EVENT_SCHEMAS) == set(EventName)


def test_event_name_is_closed_rejects_arbitrary_string() -> None:
    with pytest.raises(ValueError):
        EventName("not.a.real.event")


def test_search_requested_schema_rejects_missing_required_field() -> None:
    schema = EVENT_SCHEMAS[EventName.SEARCH_REQUESTED]
    with pytest.raises(ValidationError):
        # destination, max_stops, attempt are all missing.
        schema.model_validate({"provider": "mock", "origin": "AMS"})


def test_validate_completed_schema_requires_rejection_counts_map() -> None:
    schema = EVENT_SCHEMAS[EventName.VALIDATE_COMPLETED]
    with pytest.raises(ValidationError):
        schema.model_validate({"accepted_count": 5})


def test_event_schema_allows_extra_fields_beyond_the_required_set() -> None:
    schema = EVENT_SCHEMAS[EventName.SEARCH_REQUESTED]
    validated = schema.model_validate(
        {
            "provider": "mock",
            "origin": "AMS",
            "destination": "DEL",
            "max_stops": 1,
            "attempt": 1,
            "some_future_field": "ok",
        }
    )
    assert validated.model_dump()["some_future_field"] == "ok"
