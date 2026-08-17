"""FastAPI service surface (T46/T47, Phase 7).

Exposes ``cli.py``'s existing search pipeline over HTTP -- ``POST /search``
triggers the SAME ``run_single_destination_pipeline``/
``run_all_destinations_pipeline`` functions ``flightagent run`` calls, and
``GET /runs/{id}`` / ``GET /runs/{id}/report.md`` read T45's per-run
artifact directory layout (``reporting.run_artifacts``) to serve a run's
own report back by ``run_id``.

``POST /runs/{id}/approve`` (T47, ``routes_approval``) renders the spec's
section 8 approval prompt for a run's top-ranked itinerary and records the
human's decision as an audit-only record -- see that module's own
docstring for why it is deliberately incapable of authorizing any real
action (no booking tool exists in this codebase to authorize).

SAFETY (master plan section 8.7, CRITICAL): this surface ships with no
authentication in Phase 7. It binds to ``127.0.0.1`` by default IN CODE
(``app.DEFAULT_HOST`` / ``app.serve``) -- see ``app.py``'s own module
docstring before changing that default. NOT SAFE to expose beyond
localhost without adding real authentication first.
"""

from __future__ import annotations
