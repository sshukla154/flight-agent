"""Direct-vs-one-stop decision policy (Phase 5, T31, D10).

Public surface: :func:`flightagent.policy.direct_vs_stop.analyze_destination`,
which turns one destination's direct pool and one-stop pool (D13
pool-separated, ``cli.py``'s ``_build_direct_vs_stop_pools``) into a single
``domain.policy.DestinationAnalysis``.
"""

from __future__ import annotations

from flightagent.policy.direct_vs_stop import analyze_destination, evaluate_direct_tier

__all__ = ["analyze_destination", "evaluate_direct_tier"]
