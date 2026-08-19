"""FastAPI app factory (T46) -- exposes ``cli.py``'s existing search
pipeline over HTTP.

CRITICAL SAFETY REQUIREMENT (master plan section 8.7): "AuthN on every
endpoint -- not left open 'just for local dev', since local-dev-only
endpoints are exactly what gets exposed by a forgotten port-forward. Bind
127.0.0.1 by default in code, not as something the developer must
remember."

Phase 7 shipped with NO authentication on any endpoint -- reasonable for a
personal project's local dev surface at the time, but that made the bind
address the ONLY thing standing between this process and the open
internet if it were ever run somewhere reachable. ``DEFAULT_HOST`` below
is therefore hardcoded to ``127.0.0.1`` and ``serve()`` never accepts a
caller-supplied host that could override it upward to ``0.0.0.0`` by
accident -- a caller who genuinely needs a different bind address has to
edit this constant, not pass a flag, so the unsafe case can never be one
typo away.

Phase 8b (``api.auth``) adds a second, independent layer: every route
requires a matching ``X-Api-Key`` header, checked against
``FLIGHTAGENT_API_KEY``. This is DEFENSE IN DEPTH on top of the bind
address, not a replacement for it -- it is the deliberately minimal slice
master plan section 8.7 asks for ("AuthN on every endpoint"), not the
fuller HMAC-capability-token design section 8.4 reserves for an actual
booking capability (which still does not exist in this codebase).

**THIS SERVICE SURFACE IS STILL NOT SAFE TO EXPOSE BEYOND LOCALHOST.** Do
not put it behind a reverse proxy, a container port mapping to
``0.0.0.0``, or a cloud load balancer on the strength of the API key
alone -- see D22 in ``DECISIONS.md`` for the exact scope of what this
control does and does not cover.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from flightagent.api.auth import _configured_api_key, require_api_key
from flightagent.api.routes_approval import router as approval_router
from flightagent.api.routes_runs import router as runs_router
from flightagent.api.routes_search import router as search_router
from flightagent.config.loader import load_config
from flightagent.config.models import FlightAgentSettings

DEFAULT_HOST = "127.0.0.1"
"""Localhost only -- see module docstring. Never change this default to
``0.0.0.0`` (or any non-loopback address) without adding real
authentication to every route first (master plan section 8.7)."""

DEFAULT_PORT = 8000


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Load config ONCE at process startup rather than once per request --
    ``load_config()`` re-reads and re-validates the four-layer TOML/env
    stack on every call, which would be wasted work under repeated
    requests. Phase 7 has no shared cache connection to open or close
    here (T44's SQLite cache layer is a separate, disjoint task) -- this
    lifespan is deliberately this small per T46's own "do not
    over-engineer this" brief.
    """
    app.state.settings = load_config()
    yield


def create_app(*, settings: FlightAgentSettings | None = None) -> FastAPI:
    """Build the FastAPI application.

    ``settings``, when supplied, is stored directly on ``app.state`` and
    the startup lifespan (which would otherwise call ``load_config()``) is
    skipped entirely -- this is what lets a test hand in a
    ``FlightAgentSettings`` pointed at an isolated ``tmp_path`` output
    directory without ever touching the real packaged/``./config``
    layers. The real ``uvicorn`` entry point (``serve`` below) always
    omits it, so the lifespan loads the effective config exactly once at
    real startup.

    ``_configured_api_key()`` is called here, synchronously, before the
    ``FastAPI`` object is even constructed -- app creation itself raises
    ``RuntimeError`` if ``FLIGHTAGENT_API_KEY`` is unset, regardless of
    whether ``settings`` was supplied or a lifespan ever actually runs
    (master plan section 8.7, fail-closed). ``dependencies=[Depends(
    require_api_key)]`` on the ``FastAPI`` constructor -- not on each
    router individually -- gates every route, including ``/healthz``,
    uniformly: the checklist's own wording is "every endpoint," with no
    health-check carve-out.
    """
    _configured_api_key()

    if settings is not None:
        app = FastAPI(
            title="flightagent", version="0.1.0", dependencies=[Depends(require_api_key)]
        )
        app.state.settings = settings
    else:
        app = FastAPI(
            title="flightagent",
            version="0.1.0",
            lifespan=_lifespan,
            dependencies=[Depends(require_api_key)],
        )

    app.include_router(search_router)
    app.include_router(runs_router)
    app.include_router(approval_router)
    return app


def serve(*, port: int = DEFAULT_PORT) -> None:
    """Local-dev entry point: ``python -m flightagent.api.app``.

    Deliberately takes no ``host`` parameter -- see this module's own
    docstring for why ``DEFAULT_HOST`` is not a caller-overridable flag.
    Requires ``uvicorn``, a dev-only dependency (this project's test suite
    talks to the ``FastAPI`` app object directly via ``TestClient``/
    ``AsyncClient`` and never spins up a real server process).
    """
    import uvicorn

    uvicorn.run(create_app(), host=DEFAULT_HOST, port=port)


if __name__ == "__main__":
    serve()
