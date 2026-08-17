"""``save_json`` tool -- the original spec's own named tool (§3.3):
"Persists intermediate and final results."

Master plan §1 ("MCP tool granularity") and §8.3 pin the exposed tool
registry to exactly three names -- ``search_flights``, ``airport_info``,
``save_json`` -- and a unit test elsewhere asserts the registry key set
EQUALS that fixed set, never a superset. This module is that third tool.

A thin function, deliberately: it persists ARBITRARY structured data --
intermediate results mid-pipeline, or a final artifact -- to a caller-given
path. It does not know or care what the data means, unlike
``reporting.json_report.build_results_document`` (which knows the specific
D15 results shape) or ``reporting.run_artifacts.write_run_artifacts``
(which knows the specific per-run directory convention). Both of those are
appropriate BUILDERS of the data this tool then persists; this tool is not
a competing builder.

Reuses ``reporting.writer.atomic_write_json`` rather than a raw
``json.dump`` -- per this task's explicit instruction not to reinvent
Phase 2's already-built, crash-safe (temp file + fsync + ``os.replace``)
write primitive. A raw ``json.dump(path.open("w"), data)`` has no such
guarantee: a crash or interrupt mid-write would leave a truncated,
unparseable file at ``path``, exactly the failure mode ``writer.py``'s own
module docstring exists to close.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from flightagent.reporting.writer import atomic_write_json


def save_json(data: Mapping[str, Any], path: Path | str, *, indent: int = 2) -> Path:
    """Persist ``data`` -- an arbitrary JSON-serializable mapping, whether
    an intermediate or a final result -- to ``path``, atomically.

    ``path`` may be given as a ``str`` (the natural shape for an
    MCP/LLM-facing tool call, whose arguments arrive as JSON-schema
    primitives, not ``Path`` objects) or a ``Path`` directly. The parent
    directory is created if missing (``atomic_write_json`` ->
    ``atomic_write_text``'s own behaviour), and the write is all-or-nothing:
    a reader can never observe a partially-written file at ``path``, and a
    failure leaves ``path`` exactly as it was before the call.

    Returns the resolved ``Path`` actually written, so a caller that only
    had a ``str`` gets back the same typed value ``reporting.writer``'s own
    functions use everywhere else in this codebase.
    """
    target = Path(path)
    atomic_write_json(target, data, indent=indent)
    return target
