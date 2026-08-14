"""Single-line JSON structured logging, with secret redaction before emission.

Master plan S7: single-line JSON, closed event enum, per-event required
fields enforced in code. Master plan S8.5/S8.8: logs must redact
`Authorization`/`api_key`/`client_secret`-shaped fields before emission —
a named CRITICAL/HIGH security control, not a nice-to-have.
"""

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any, TextIO

from pydantic import ValidationError

from flightagent.observability.context import get_run_id, get_task_id
from flightagent.observability.events import EVENT_SCHEMAS, EventName

LOGGER_NAME = "flightagent"
SCHEMA_VERSION = "1.0"

# Attribute name used to smuggle the structured-fields dict through stdlib
# `logging`'s `extra=` mechanism without colliding with LogRecord's own
# reserved attribute names (`msg`, `args`, `name`, ... — passing e.g.
# `extra={"msg": ...}` directly raises at the `Logger.makeRecord` call).
_FIELDS_ATTR = "flightagent_fields"

# Key-name shapes treated as secrets and redacted before a line is ever
# emitted. Matched case-insensitively, by substring rather than by an exact
# name list, because header casing and provider-specific variants
# (`X-Api-Key`, `client_secret_key`, `refresh_token`) all need to be caught,
# not only the three literal names the plan names by example.
_SECRET_KEY_PATTERN = re.compile(
    r"(authorization|api[_-]?key|client[_-]?secret|access[_-]?token|"
    r"refresh[_-]?token|secret|password|passwd|bearer|private[_-]?key|"
    r"session[_-]?token)",
    re.IGNORECASE,
)
REDACTED = "***REDACTED***"


class EventValidationError(ValueError):
    """Raised when fields passed to `log_event` fail that event's required schema."""


def redact(value: Any) -> Any:
    """Recursively mask any dict key whose name looks secret-shaped.

    Applied to the fully-assembled log payload immediately before
    serialization in `RedactingJsonFormatter.format()`, so a secret cannot
    reach the emitted line regardless of which call site produced it or how
    deeply it is nested (e.g. inside a forwarded headers dict).
    """
    if isinstance(value, dict):
        return {
            key: (REDACTED if _SECRET_KEY_PATTERN.search(str(key)) else redact(val))
            for key, val in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


class RedactingJsonFormatter(logging.Formatter):
    """Renders exactly one line of JSON per `LogRecord`, secrets redacted."""

    def format(self, record: logging.LogRecord) -> str:
        fields = getattr(record, _FIELDS_ATTR, None)
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname.lower(),
            "schema_version": SCHEMA_VERSION,
        }
        if isinstance(fields, dict):
            payload.update(fields)
        else:
            payload["msg"] = record.getMessage()
        payload = redact(payload)
        # Compact separators and no `indent`: json.dumps never inserts a raw
        # newline inside a string it encodes (control chars are escaped as
        # `\n`), so this is structurally always exactly one line.
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def setup_logging(*, level: int = logging.INFO, stream: TextIO | None = None) -> None:
    """Configure the `flightagent` logger for single-line JSON output.

    Idempotent: replaces this logger's handlers rather than accumulating
    them, so calling it more than once (e.g. once from a CLI entry point,
    once from a test) does not duplicate output lines.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.handlers.clear()
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(RedactingJsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False


def log_event(
    event: EventName,
    *,
    level: int = logging.INFO,
    msg: str | None = None,
    **fields: Any,
) -> None:
    """Emit one structured log line for `event`.

    `fields` is validated against `EVENT_SCHEMAS[event]` before anything is
    logged: a missing required field raises `EventValidationError` rather
    than silently shipping an incomplete line. `run_id` and `task_id` are
    injected automatically from their contextvars (see `context.py`) — do
    not pass either as a keyword here.
    """
    schema = EVENT_SCHEMAS[event]
    try:
        validated = schema.model_validate(fields)
    except ValidationError as exc:
        raise EventValidationError(f"event {event.value!r}: {exc}") from exc

    payload: dict[str, Any] = {"event": event.value}
    run_id = get_run_id()
    if run_id is not None:
        payload["run_id"] = run_id
    task_id = get_task_id()
    if task_id is not None:
        payload["task_id"] = task_id
    payload.update(validated.model_dump())
    if msg is not None:
        payload["msg"] = msg

    logging.getLogger(LOGGER_NAME).log(level, msg or event.value, extra={_FIELDS_ATTR: payload})
