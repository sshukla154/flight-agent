"""Minimal API-key authentication for every FastAPI endpoint (Phase 8b,
master plan section 8.7: "AuthN on every endpoint -- not left open 'just
for local dev'... Bind 127.0.0.1 by default in code").

This is deliberately the smallest thing that closes that checklist item,
not the fuller design master plan section 8.4 reserves for a real booking
capability token (HMAC-signed, itinerary-snapshot-scoped, its own
dedicated security review) -- there is still no booking tool in this
codebase to protect with that heavier mechanism. What this module adds is
a second, independent layer on top of the existing ``127.0.0.1`` bind
(``api/app.py``'s own primary control, unchanged) -- defense in depth
against the exact scenario that bind guards against becoming exploitable
by accident (a forgotten port-forward, a container mapped to ``0.0.0.0``).

The key is read directly from the ``FLIGHTAGENT_API_KEY`` environment
variable -- never through ``config.loader``'s TOML/env-var layering. That
layer is for non-secret settings (master plan section 7); secrets come
from env only (section 8.5), and this module follows the exact same
convention ``providers.amadeus``/``providers.duffel`` already established
for real credentials, which also keeps this value out of
``config.compute_config_digest``'s hashing entirely.

Fail-closed by design: ``create_app()`` calls ``_configured_api_key()``
once, synchronously, at app-creation time -- if the env var is unset, app
creation itself raises immediately, rather than silently serving every
request unauthenticated. This mirrors section 8.4's own "no approval-
service-unreachable-assume-yes branch" discipline, applied here to
authentication generally rather than only to the approval gate.
"""

from __future__ import annotations

import os
import secrets

from fastapi import Header, HTTPException

API_KEY_ENV_VAR = "FLIGHTAGENT_API_KEY"


def _configured_api_key() -> str:
    """The real key, read fresh from the environment every call -- never
    cached at import time, so a test's ``monkeypatch.setenv`` always
    takes effect regardless of import order.
    """
    key = os.environ.get(API_KEY_ENV_VAR)
    if not key:
        raise RuntimeError(
            f"{API_KEY_ENV_VAR} is not set -- the FastAPI service refuses to start "
            "without it (master plan section 8.7, fail-closed by design)."
        )
    return key


async def require_api_key(x_api_key: str = Header(default="")) -> None:
    """FastAPI dependency: every route this is attached to needs a
    matching ``X-Api-Key`` header.

    ``secrets.compare_digest`` rather than ``==`` -- a plain string
    comparison short-circuits on the first mismatched byte, which is a
    real (if narrow) timing side channel for a secret comparison;
    ``compare_digest`` runs in constant time regardless of where the
    strings first differ.
    """
    expected = _configured_api_key()
    if not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="invalid or missing API key")
