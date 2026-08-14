"""Run-scoped correlation identifiers, propagated via contextvars.

`run_id` and `task_id` are read by `observability.logging.log_event()` and
injected into every log line automatically, so nothing else in the codebase
has to thread them through call signatures. Master plan S7.

Identifier choice — UUID4, not ULID:

Master plan S7's own example log line shows a ULID-shaped `run_id`
("01J..."). This module deliberately uses `uuid.uuid4()` instead, per the
DECISIONS.md T6 task's explicit "ULID or UUID4, your call, document which"
allowance. Reasons:

1. `uuid.uuid4()` is stdlib — no new third-party dependency, and this
   project only installs what an explicit task specifies.
2. A correct ULID needs a Crockford base32 encoder plus a monotonic random
   component under concurrent generation — exactly the kind of small
   format-adjacent code that is easy to get subtly wrong, for a benefit
   (lexicographic sort-by-creation-time) this project does not need: every
   log line already carries its own `ts`, so time-ordering is available
   there without leaning on the id's own byte layout.
3. UUID4 collision probability is negligible at this project's scale (at
   most a few hundred runs, ever).

If a future phase needs ULID's sortability specifically, swap `new_run_id`'s
implementation — nothing else in this module depends on the id's shape.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4

_run_id: ContextVar[str | None] = ContextVar("flightagent_run_id", default=None)
_task_id: ContextVar[str | None] = ContextVar("flightagent_task_id", default=None)


def new_run_id() -> str:
    """Generate a fresh run correlation id. See module docstring: UUID4."""
    return str(uuid4())


def get_run_id() -> str | None:
    """Current run_id, or None if no `run_context` is active."""
    return _run_id.get()


def get_task_id() -> str | None:
    """Current task_id, or None if no `task_context` is active."""
    return _task_id.get()


@contextmanager
def run_context(run_id: str | None = None) -> Iterator[str]:
    """Bind `run_id` for the duration of the `with` block.

    Pass an explicit `run_id` to correlate with one minted elsewhere (e.g.
    before entering this context manager); omit it to mint a fresh one here.
    """
    resolved = run_id if run_id is not None else new_run_id()
    token = _run_id.set(resolved)
    try:
        yield resolved
    finally:
        _run_id.reset(token)


@contextmanager
def task_context(task_id: str) -> Iterator[str]:
    """Bind `task_id` for the duration of the `with` block.

    Expected shape (master plan S7): `f"{origin}-{destination}-s{max_stops}"`
    — deterministic and human-readable, which makes two runs' logs directly
    diffable. Not enforced here; later phases construct it, this module only
    carries it.
    """
    token = _task_id.set(task_id)
    try:
        yield task_id
    finally:
        _task_id.reset(token)
