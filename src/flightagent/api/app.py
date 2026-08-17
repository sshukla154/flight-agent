"""FastAPI app factory (T46) -- exposes ``cli.py``'s existing search
pipeline over HTTP.

CRITICAL SAFETY REQUIREMENT (master plan section 8.7): "AuthN on every
endpoint -- not left open 'just for local dev', since local-dev-only
endpoints are exactly what gets exposed by a forgotten port-forward. Bind
127.0.0.1 by default in code, not as something the developer must
remember."

Phase 7 ships NO authentication on any endpoint -- reasonable for a
personal project's local dev surface, per this task's own brief, but that
makes the bind address the ONLY thing standing between this process and
the open internet if it is ever run somewhere reachable. ``DEFAULT_HOST``
below is therefore hardcoded to ``127.0.0.1`` and ``serve()`` never
accepts a caller-supplied host that could override it upward to
``0.0.0.0`` by accident -- a caller who genuinely needs a different bind
address has to edit this constant, not pass a flag, so the unsafe case
can never be one typo away.

**THIS SERVICE SURFACE IS NOT SAFE TO EXPOSE BEYOND LOCALHOST.** Do not
put it behind a reverse proxy, a container port mapping to ``0.0.0.0``, or
a cloud load balancer without adding real authentication first.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

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
    """
    if settings is not None:
        app = FastAPI(title="flightagent", version="0.1.0")
        app.state.settings = settings
    else:
        app = FastAPI(title="flightagent", version="0.1.0", lifespan=_lifespan)

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
