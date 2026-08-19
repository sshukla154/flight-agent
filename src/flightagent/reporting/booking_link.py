"""Booking-URL safety gate -- master plan S8.2, v1 slice (T15).

Master plan S8.2 specifies a much larger ``validate_booking_url()``: a
PSL-aware per-provider host allowlist (``tldextract``, matched on the
public-suffix boundary, never ``endswith``), userinfo rejection,
punycode/homograph rejection, credential-bearing query-string rejection, a
length cap, and a dedicated Markdown-escaping helper. The per-provider host
allowlist is explicitly out of scope here -- D6 means no real adapter
exists yet (Amadeus/Duffel land in Phase 7), so there is nothing to
allowlist against. This module builds the slice that is cheap and
load-bearing TODAY, per this task's brief:

- **https scheme only** (master plan S8.8 checklist, CRITICAL).
- No userinfo in the authority (``user:pass@host`` or a bare ``@``).
- Host must be pure ASCII (S8.2's homograph rule).
- Length capped at 2048 characters.
- When ``data_source == "mock"`` (the only value that exists until Phase 7
  -- ``MockProvider`` is the only provider in this codebase), the host
  must sit under one of the RFC 2606 (``.test``, ``.example``) or RFC 6761
  (``.invalid``) reserved suffixes, which are guaranteed to never resolve
  on the public internet. This is the concrete form of this task's brief:
  "since there is no real provider allowlist yet, a mock-generated booking
  URL should point to a clearly-fake domain... so it can never be mistaken
  for a real booking link."

Anything this module rejects must never reach the rendered report as a
raw, clickable URL -- callers (``reporting.markdown``,
``reporting.json_report``) render a safe fallback instead. Called at
render time only in this task's scope; master plan S8.2 also calls for
this at provider-adapter ingestion time, which is Phase 7 scope (no
adapter exists yet to call it from).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from urllib.parse import parse_qsl, urlsplit

DataSource = Literal["mock", "live"]

_ALLOWED_SCHEME = "https"
_MAX_URL_LENGTH = 2048

_RESERVED_MOCK_SUFFIXES: tuple[str, ...] = (".invalid", ".test", ".example")
"""RFC 2606 (``.test``, ``.example``) and RFC 6761 (``.invalid``) reserved
suffixes -- none of these can ever resolve on the public internet, so a
mock booking URL under one of them can never collide with, or be mistaken
for, a real airline/OTA domain."""

_UNSAFE_LINK_TARGET_CHARS: tuple[str, ...] = (" ", "\t", "\n", "\r", ")")
"""Characters that would let a URL break out of Markdown's ``(...)`` link
target syntax (master plan S8.2's link-injection rule)."""

_SENSITIVE_QUERY_KEYS: tuple[str, ...] = (
    "password",
    "token",
    "session",
    "api_key",
    "passport",
    "dob",
    "card",
    "cvv",
)
"""Master plan S8.2: a query-string key matching one of these (case-
insensitive) means something already went wrong upstream -- e.g. an
adapter forwarding a search-session token into what should be a public
link. Rejected outright, never silently stripped, so the caller sees a
loud failure instead of a link that looks fine but leaked a credential."""


class BookingUrlRejected(ValueError):
    """Raised by ``validate_booking_url``/``markdown_link`` when a booking
    URL fails a S8.2 check.

    Carries a stable, machine-readable ``reason`` code (snake_case, never
    renamed once shipped -- callers and tests key on it) so a caller can
    log or report *why* a URL was rejected, not just that it was.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class ValidatedBookingUrl:
    """A booking URL that has passed every check in ``validate_booking_url``."""

    url: str
    host: str


def validate_booking_url(url: str, *, data_source: DataSource) -> ValidatedBookingUrl:
    """Validate one booking URL per this module's S8.2 v1 slice.

    Raises ``BookingUrlRejected`` -- never returns a partially-valid
    result -- for:

    - a URL longer than 2048 characters (``url_too_long``)
    - anything but an ``https`` scheme (``non_https_scheme``)
    - userinfo in the authority, e.g. ``user:pass@host`` or a bare ``@``
      before the host (``userinfo_present``)
    - a missing/empty host (``missing_host``)
    - a non-ASCII host (``non_ascii_host`` -- S8.2's homograph rule: given
      a small, fixed, self-controlled domain set, the robust rule is to
      reject anything that isn't byte-for-byte ASCII rather than try to
      "support" IDN)
    - a query-string key matching ``_SENSITIVE_QUERY_KEYS`` (case-
      insensitive) -- ``password``, ``token``, ``session``, ``api_key``,
      ``passport``, ``dob``, ``card``, ``cvv`` (``sensitive_query_param``)
    - for ``data_source="mock"``: a host that is not under one of the
      reserved test suffixes above (``mock_host_not_reserved``)

    ``data_source="live"`` skips the last check only. It does not silently
    wave a live URL through forever -- the real per-provider PSL allowlist
    master plan S8.2 specifies for that branch is Phase 7 scope (there is
    no real adapter yet to allowlist against), and this docstring is the
    marker for whoever builds Phase 7 to come back and close it.
    """
    if len(url) > _MAX_URL_LENGTH:
        raise BookingUrlRejected(
            "url_too_long", f"booking URL exceeds {_MAX_URL_LENGTH} characters"
        )

    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError as exc:
        raise BookingUrlRejected(
            "malformed_url", f"booking URL could not be parsed: {exc}"
        ) from exc

    if parts.scheme != _ALLOWED_SCHEME:
        raise BookingUrlRejected(
            "non_https_scheme",
            f"booking URL scheme must be exactly {_ALLOWED_SCHEME!r}, got {parts.scheme!r}",
        )

    if "@" in parts.netloc:
        raise BookingUrlRejected(
            "userinfo_present",
            "booking URL authority contains userinfo ('@') -- rejected per master plan S8.2",
        )

    if not host:
        raise BookingUrlRejected("missing_host", "booking URL has no host")

    if not host.isascii():
        raise BookingUrlRejected(
            "non_ascii_host",
            f"booking URL host {host!r} is not pure ASCII -- rejected per master plan "
            f"S8.2's homograph rule",
        )

    query_keys = {key.lower() for key, _value in parse_qsl(parts.query, keep_blank_values=True)}
    sensitive_keys_present = query_keys & set(_SENSITIVE_QUERY_KEYS)
    if sensitive_keys_present:
        raise BookingUrlRejected(
            "sensitive_query_param",
            f"booking URL query string carries sensitive key(s) {sorted(sensitive_keys_present)!r} "
            f"-- rejected per master plan S8.2",
        )

    if data_source == "mock" and not any(
        host.endswith(suffix) for suffix in _RESERVED_MOCK_SUFFIXES
    ):
        raise BookingUrlRejected(
            "mock_host_not_reserved",
            f"mock booking URL host {host!r} is not under a reserved test suffix "
            f"{_RESERVED_MOCK_SUFFIXES!r} -- a synthetic run must never emit a booking "
            f"link that could be mistaken for a real one",
        )

    return ValidatedBookingUrl(url=url, host=host)


def escape_markdown_link_text(text: str) -> str:
    r"""Escape ``\``, ``[``, ``]`` and `` ` `` in Markdown link text.

    Master plan S8.2: a crafted value like ``Air India](https://evil.com
    "`` must not be able to break out of ``[...]`` and redefine the link
    target. A backtick is escaped too -- some Markdown renderers let an
    inline code span override normal link-syntax parsing, so an
    unescaped backtick inside link text is its own, separate way to
    corrupt the surrounding structure, not just cosmetic. Applied
    unconditionally to every string placed inside ``[...]`` -- airline
    codes are already IATA-pattern-constrained
    (``domain.airport.CarrierCode``, ``^[A-Z0-9]{2}$``) so this is a no-op
    for them today, but master plan S8.1's threat model treats every
    provider-sourced field as untrusted, so the escaping is not
    conditioned on a per-field "looks risky" judgement call.

    ``\\`` is replaced first so the backslashes this function itself
    introduces for ``[``/``]``/`` ` `` are never re-escaped a second time.
    """
    return (
        text.replace("\\", "\\\\")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("`", "\\`")
    )


def markdown_link(text: str, url: str) -> str:
    """Build one Markdown link ``[text](url)`` -- never plain f-string
    interpolation (master plan S8.2).

    ``text`` is escaped via ``escape_markdown_link_text``. ``url`` is
    expected to already be the output of ``validate_booking_url`` (an
    already-validated https URL); this function additionally refuses to
    place it inside ``(...)`` if it contains whitespace, a raw ``)``, or
    any non-ASCII character, any of which could either break the link
    target's syntax or reintroduce S8.2's homograph risk at render time.
    Raises ``BookingUrlRejected`` (reason ``unsafe_link_target``) rather
    than silently stripping the offending characters.
    """
    if not url.isascii() or any(char in url for char in _UNSAFE_LINK_TARGET_CHARS):
        raise BookingUrlRejected(
            "unsafe_link_target",
            "booking URL contains whitespace, ')', or a non-ASCII character unsafe for a "
            "Markdown link target",
        )
    return f"[{escape_markdown_link_text(text)}]({url})"
