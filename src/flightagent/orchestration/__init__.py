"""Task planning and bounded-concurrency execution (master plan S3).

``orchestration`` may import ``domain``, ``config``, ``providers``, and
``observability`` (master plan S3's import-linter contract: it sits above
every other layer except ``agent``). See ``plan.py`` for how one origin's
task set is built and ``executor.py`` for how that set is executed.
"""

from __future__ import annotations

from flightagent.orchestration.executor import TaskExecutionResult, execute_plan
from flightagent.orchestration.plan import build_plan_for_origin

__all__ = [
    "TaskExecutionResult",
    "build_plan_for_origin",
    "execute_plan",
]
