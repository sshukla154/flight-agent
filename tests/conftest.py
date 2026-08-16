"""Shared pytest bootstrap for the whole ``tests/`` tree.

This project's test layout has no ``tests/__init__.py`` -- every test
module is collected "rootless" (each subdirectory is its own importable
unit), matching every existing file under ``tests/unit``,
``tests/observability`` and ``tests/e2e``. That is fine for test modules
themselves, but ``tests/support`` (T25) is a real, dotted-import package
(``tests.support.instrumented_provider``) meant to be shared across test
files in different subdirectories -- and none of pytest's rootless
import-mode machinery puts the PROJECT ROOT (the parent of ``tests/``)
onto ``sys.path``, only each individual test file's own containing
directory. Without this, ``import tests.support...`` fails with
``ModuleNotFoundError: No module named 'tests'`` from any test file that
is not itself directly inside ``tests/``.

This file exists solely to fix that: pytest always imports every
``conftest.py`` it finds above a collected test file before collecting
that file, so inserting the project root here -- once -- makes
``tests.support`` importable from any test module anywhere under
``tests/``, without every individual test file repeating its own
``sys.path`` bootstrap.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_PROJECT_ROOT_STR = str(_PROJECT_ROOT)
if _PROJECT_ROOT_STR not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT_STR)
