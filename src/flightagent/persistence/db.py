"""SQLite connection setup: WAL mode + idempotent schema application (T43).

Master plan S5 ("Caching: SQLite, not DuckDB"): "`sqlite3` is stdlib (zero
dependency for the core cache), WAL gives concurrent readers, and the file
is small enough to commit as a test fixture." This module owns exactly two
responsibilities -- opening a connection with the right pragmas, and
applying ``schema.sql`` -- so every other module in this package (and every
test) gets a consistently-configured connection without repeating either
step.

Deliberately plain ``sqlite3`` here, not the ``aiosqlite`` dependency this
task also adds: ``persistence.cache_repo.CacheRepository`` is the only
consumer of a live connection during normal operation, and it explicitly
delegates every blocking call to ``asyncio.to_thread`` itself (master plan
S5's own stated mechanism) rather than layering a second, already-async
driver underneath that. ``connect()`` is a plain synchronous function for
exactly that reason -- it is always invoked from inside a
``asyncio.to_thread`` call (or, in tests, from ordinary synchronous code),
never awaited directly.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open a SQLite connection configured for this project's cache usage.

    WAL journal mode is set immediately after connecting so a writer never
    blocks concurrent reader coroutines under the master-plan-specified
    8-way concurrent search fan-out this cache serves underneath. The
    schema is then applied via ``executescript`` -- every statement in
    ``schema.sql`` is an idempotent ``CREATE TABLE IF NOT EXISTS`` /
    ``CREATE INDEX IF NOT EXISTS``, so calling this on an
    already-migrated file (or a fresh ``:memory:`` database, which is what
    every unit test in ``tests/unit/test_cache.py`` uses) is always safe.

    ``db_path`` may be ``":memory:"`` for a private, single-connection
    in-memory database (SQLite's own WAL pragma is a silent no-op there --
    there is exactly one connection to the whole database in that case, so
    no concurrent-reader guarantee is needed or lost). For a real file
    path, the parent directory is created first, mirroring
    ``reporting.writer.atomic_write_text``'s own "create the parent dir if
    it's missing" behaviour for ``out/``.

    ``check_same_thread=False``: ``CacheRepository`` dispatches every
    blocking call for this connection through ``asyncio.to_thread``, whose
    default executor is a thread POOL, not one fixed worker thread -- two
    calls made moments apart on the same connection can legitimately land
    on two different OS threads, neither of which is necessarily the
    thread that called ``connect()``. ``sqlite3``'s default
    ``check_same_thread=True`` would raise ``ProgrammingError`` the moment
    that happens, even for calls that are never actually concurrent (every
    ``CacheRepository`` call is awaited to completion before the next one
    starts on that same coroutine). Genuinely concurrent calls from
    multiple tasks under the orchestrator's 8-way fan-out sharing one
    connection are still safe: the CPython-bundled SQLite library is built
    with ``SQLITE_THREADSAFE=1`` ("serialized" mode), which puts its own
    mutex around the connection -- concurrent callers serialize on that
    mutex rather than racing, so `check_same_thread=False` only removes
    Python's own same-thread guard, never SQLite's actual data-safety
    guarantee.
    """
    if str(db_path) != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(db_path), check_same_thread=False)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
    connection.commit()
    return connection
