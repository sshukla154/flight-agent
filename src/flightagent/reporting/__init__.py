"""flightagent.reporting -- report writer v1 (T15).

Renders the two D15 output artifacts from a ranked list of
``ScoredItinerary``:

- ``markdown.render_markdown_report`` -- the Markdown report (spec section
  6's "Recommended Flight" block + "Other Good Options" table), with the
  master plan S8.6 synthetic-data banner at the very top.
- ``json_report.build_results_document`` -- the JSON artifact, with
  ``data_source`` as a structural top-level field (S8.6).
- ``writer.write_report_artifacts`` -- atomic (write-temp-then-rename)
  writes of both to disk.
- ``booking_link`` -- the S8.2 booking-URL safety gate both of the above
  call before ever rendering a URL as a link.

See each module's docstring for what is explicitly out of scope in this
v1 (Direct Flight Analysis, Origin Comparison, Failed Searches -- all
later-phase sections; the full ``RunEnvelope``/``RunMeta`` machinery; the
per-provider booking-URL host allowlist).
"""

from __future__ import annotations

from flightagent.reporting.booking_link import (
    BookingUrlRejected,
    DataSource,
    ValidatedBookingUrl,
    escape_markdown_link_text,
    markdown_link,
    validate_booking_url,
)
from flightagent.reporting.json_report import build_results_document
from flightagent.reporting.markdown import SYNTHETIC_DATA_BANNER, render_markdown_report
from flightagent.reporting.writer import (
    DEFAULT_REPORT_PATH,
    DEFAULT_RESULTS_PATH,
    atomic_write_json,
    atomic_write_text,
    write_report_artifacts,
)

__all__ = [
    "DEFAULT_REPORT_PATH",
    "DEFAULT_RESULTS_PATH",
    "SYNTHETIC_DATA_BANNER",
    "BookingUrlRejected",
    "DataSource",
    "ValidatedBookingUrl",
    "atomic_write_json",
    "atomic_write_text",
    "build_results_document",
    "escape_markdown_link_text",
    "markdown_link",
    "render_markdown_report",
    "validate_booking_url",
    "write_report_artifacts",
]
