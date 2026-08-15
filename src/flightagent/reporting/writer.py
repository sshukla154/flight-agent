"""Atomic artifact writer (T15).

Writes each artifact to a temp file that is a SIBLING of its final path
(same directory, therefore same filesystem), flushes and ``fsync``s it,
then atomically renames it onto the final path with ``os.replace`` -- so a
reader (or a crash mid-write) can never observe a partially-written report
or JSON file. ``os.replace`` is atomic within one filesystem: POSIX
``rename(2)`` semantics on Linux/macOS, and on Windows specifically the
reason this uses ``os.replace`` rather than ``os.rename`` -- plain
``os.rename`` raises ``FileExistsError`` if the destination already
exists on Windows, whereas ``os.replace`` uses ``MoveFileEx`` with
``MOVEFILE_REPLACE_EXISTING``, matching POSIX ``rename``'s overwrite
behaviour. Creating the temp file anywhere else (e.g. the OS temp
directory) would risk the final "rename" silently becoming a cross-
filesystem copy-then-delete, which is not atomic and reopens exactly the
partial-write window this module exists to close.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

DEFAULT_OUT_DIR = Path("out")
DEFAULT_REPORT_FILENAME = "flight_report_2027-07-17.md"
DEFAULT_RESULTS_FILENAME = "flight_results_2027-07-17.json"
"""D15's exact, spec-literal filenames -- see config/defaults.toml's
``[output]`` table, whose ``report_path``/``results_path`` already carry
these same two strings. Not derived from a ``departure_date`` parameter at
call time: D15 fixes the date to the one search date this project
targets, never "today"."""

DEFAULT_REPORT_PATH = DEFAULT_OUT_DIR / DEFAULT_REPORT_FILENAME
DEFAULT_RESULTS_PATH = DEFAULT_OUT_DIR / DEFAULT_RESULTS_FILENAME


def atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """Write ``content`` to ``path`` atomically.

    ``path``'s parent directory is created if missing -- ``out/`` is
    gitignored (Phase 1) and may not exist on a fresh checkout. On any
    failure before the final rename, the partially-written temp file is
    removed and the exception re-raised; ``path`` itself is never touched
    until the write is known-complete, so a failed write leaves either
    nothing new or the previous ``path`` content untouched, never a
    truncated file in its place.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp_name)
        raise


def atomic_write_json(path: Path, data: Mapping[str, Any], *, indent: int = 2) -> None:
    """``atomic_write_text`` for a JSON-serializable mapping.

    ``sort_keys=False`` -- ``json_report.build_results_document`` already
    emits its keys in a deliberate, readable order; re-sorting here would
    scramble that back to alphabetical for no benefit.
    """
    serialized = json.dumps(data, indent=indent, ensure_ascii=False, sort_keys=False)
    atomic_write_text(path, serialized + "\n")


def write_report_artifacts(
    *,
    markdown: str,
    json_data: Mapping[str, Any],
    report_path: Path = DEFAULT_REPORT_PATH,
    results_path: Path = DEFAULT_RESULTS_PATH,
) -> tuple[Path, Path]:
    """Atomically write both v1 artifacts and return their paths.

    Each file is written independently and atomically; this function does
    not attempt an all-or-nothing transaction across the *pair* of files
    (that would need a directory-level fsync/rename dance neither artifact
    currently requires) -- it guarantees each individual file is either
    fully the new content or untouched, which is the property
    ``test_report.py`` exercises.
    """
    atomic_write_text(report_path, markdown)
    atomic_write_json(results_path, json_data)
    return report_path, results_path
