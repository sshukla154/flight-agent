"""T28: makes ``tests/integration`` an actual package.

Every other ``tests/`` subdirectory (``unit``, ``e2e``, ``observability``) is
deliberately "rootless" (no ``__init__.py``, per ``tests/conftest.py``'s own
docstring on the project's test layout) -- each test module there is
collected under its own bare basename. That collides here: this directory's
own ``test_orchestrator.py``/``test_retry.py`` share a basename with
``tests/unit/test_orchestrator.py``/``tests/unit/test_retry.py``, and
pytest's default (rootless) import mode cannot collect two same-named
modules from different, package-less directories in one run ("import file
mismatch").

Giving ONLY this directory an ``__init__.py`` makes pytest import its
modules as ``integration.test_orchestrator`` / ``integration.test_retry``
instead of the bare ``test_orchestrator`` / ``test_retry`` two other files
already claim -- resolving the collision without touching the existing
rootless layout of any other ``tests/`` subdirectory, and without renaming
any file away from the exact path this phase's task brief specifies.
"""

from __future__ import annotations
