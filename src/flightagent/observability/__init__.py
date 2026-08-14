"""Structured logging, correlation ids, and the closed event-name enum.

Master plan S7. See `events.py` for the closed `EventName` enum and
per-event required-field schemas, `context.py` for the `run_id`/`task_id`
contextvars, and `logging.py` for the JSON formatter, setup function, and
secret redaction.
"""

from flightagent.observability.context import (
    get_run_id,
    get_task_id,
    new_run_id,
    run_context,
    task_context,
)
from flightagent.observability.events import EVENT_SCHEMAS, EventFields, EventName
from flightagent.observability.logging import (
    REDACTED,
    EventValidationError,
    RedactingJsonFormatter,
    log_event,
    redact,
    setup_logging,
)

__all__ = [
    "EVENT_SCHEMAS",
    "REDACTED",
    "EventFields",
    "EventName",
    "EventValidationError",
    "RedactingJsonFormatter",
    "get_run_id",
    "get_task_id",
    "log_event",
    "new_run_id",
    "redact",
    "run_context",
    "setup_logging",
    "task_context",
]
