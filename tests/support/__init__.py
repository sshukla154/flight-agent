"""Test-only support package (T25).

Not part of ``src/flightagent`` (master plan S3's module tree) — this
package exists purely to give the test suite a shared, importable test
double (``instrumented_provider.InstrumentedProvider``) instead of each
test file redefining its own inline fake, the way ``test_orchestrator.py``
still does for its own narrower needs.
"""

from __future__ import annotations
