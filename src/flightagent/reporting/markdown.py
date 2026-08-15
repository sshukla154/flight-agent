"""Markdown report renderer v1 (T15).

Builds to the ORIGINAL spec's section 6 "Expected Output Format", quoted
in this task's brief -- not an invented format:

- A "Recommended Flight" block with exactly seven fields: Airline, Route,
  Departure, Layover, Arrival, Price (EUR), Booking.
- An "Other Good Options" table with exactly four columns: Airline |
  Route | Layover | Price.

Scope note (v1, per this task's brief): the "Direct Flight Analysis"
section (Phase 5, T33), the "Origin Comparison" table (Phase 6, T41), and
the "Failed Searches" section (Phase 4, T26/T27) are deliberately NOT
built here -- their underlying data (the direct-vs-stop policy,
multi-origin fan-out, partial-failure handling) doesn't exist until those
phases. ``render_markdown_report`` therefore requires at least one ranked
itinerary; the empty/no-results report path (finding 0.5's NO_RESULTS/
FAILED ``RunStatus`` values) is Phase 4 scope.

Assembly is plain f-string/helper functions, not a templating engine --
the report is small enough (one block, one table, one summary line) that
a new dependency (Jinja2) would not pay for itself yet.

Master plan S8.6 / S8.8 checklist (CRITICAL, not optional, and binding
from the very first report this project ever generates -- see this
module's own docstring for ``SYNTHETIC_DATA_BANNER``): mock output must
carry ``"data_source": "mock"`` as a structural JSON field (``json_report.py``)
*and* an unmissable banner at the very top of the Markdown, not a
footnote that can be truncated or ignored.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime

from flightagent.domain.itinerary import NormalizedItinerary
from flightagent.domain.scoring import ScoredItinerary
from flightagent.reporting.booking_link import (
    BookingUrlRejected,
    DataSource,
    markdown_link,
    validate_booking_url,
)
from flightagent.reporting.view import (
    airline_string,
    first_segment,
    format_layover,
    format_price_eur,
    last_segment,
    route_string,
    total_layover_minutes,
)

SYNTHETIC_DATA_BANNER = "**SYNTHETIC DATA — NOT REAL FARES — DO NOT BOOK BASED ON THIS REPORT**"
"""Master plan S8.6/S8.8 (CRITICAL): the exact banner text. Named here
rather than inlined at every call site so this module's own render
function and ``test_report.py``'s exact-text-and-position assertion can
never drift apart from one another.
"""

_NO_BOOKING_LINK_TEXT = "*(no booking link available)*"
_WITHHELD_BOOKING_LINK_TEXT = "*(booking link withheld — failed safety validation)*"


def _format_local(local_dt: datetime, tz_name: str) -> str:
    """``"2027-07-17 09:00 CEST (Europe/Amsterdam)"``. The IANA name is
    included alongside the abbreviation because an abbreviation like
    "CST"/"IST" is genuinely ambiguous across zones on its own.
    """
    return f"{local_dt.strftime('%Y-%m-%d %H:%M %Z')} ({tz_name})"


def _booking_field(itinerary: NormalizedItinerary, *, data_source: DataSource) -> str:
    """The "Booking" field's rendered value -- never a raw, unvalidated
    URL (master plan S8.2: the booking-link validator is called at both
    ingestion, out of scope here, and render, which is exactly this
    function).
    """
    if itinerary.booking_url is None:
        return _NO_BOOKING_LINK_TEXT
    try:
        validated = validate_booking_url(str(itinerary.booking_url), data_source=data_source)
        link_text = f"Book with {airline_string(itinerary)}"
        return markdown_link(link_text, validated.url)
    except BookingUrlRejected:
        return _WITHHELD_BOOKING_LINK_TEXT


def _recommended_flight_block(item: ScoredItinerary, *, data_source: DataSource) -> str:
    itinerary = item.itinerary
    departure_segment = first_segment(itinerary)
    arrival_segment = last_segment(itinerary)

    lines = [
        "## Recommended Flight",
        "",
        f"- **Airline:** {airline_string(itinerary)}",
        f"- **Route:** {route_string(itinerary)}",
        "- **Departure:** "
        f"{_format_local(departure_segment.depart_local, departure_segment.origin_tz)}",
        f"- **Layover:** {format_layover(total_layover_minutes(itinerary))}",
        "- **Arrival:** "
        f"{_format_local(arrival_segment.arrive_local, arrival_segment.destination_tz)}",
        "- **Price (EUR):** "
        f"{format_price_eur(itinerary.price_eur)} "
        f"(fare retrieved {itinerary.fare_as_of.isoformat()})",
        f"- **Booking:** {_booking_field(itinerary, data_source=data_source)}",
    ]
    return "\n".join(lines)


def _other_good_options_table(items: Sequence[ScoredItinerary]) -> str:
    lines = [
        "## Other Good Options",
        "",
        "| Airline | Route | Layover | Price |",
        "|---|---|---|---|",
    ]
    for item in items:
        itinerary = item.itinerary
        lines.append(
            f"| {airline_string(itinerary)} | {route_string(itinerary)} | "
            f"{format_layover(total_layover_minutes(itinerary))} | "
            f"{format_price_eur(itinerary.price_eur)} |"
        )
    return "\n".join(lines)


def render_markdown_report(
    ranked: Sequence[ScoredItinerary],
    *,
    departure_date: date,
    accepted_count: int,
    generated_at: datetime,
    data_source: DataSource = "mock",
) -> str:
    """Render the full v1 Markdown report.

    ``ranked`` is the already-ranked, already-top-N-truncated list (D15) --
    ``ranked[0]`` becomes the "Recommended Flight" block, and any remaining
    items become the "Other Good Options" table rows, in the order given
    (this function does not itself re-sort). ``accepted_count`` is the
    full valid-itinerary count *before* truncation, passed in separately
    per D15 ("truncation... applies to presentation only") -- this
    function never infers it from ``len(ranked)``.

    Raises ``ValueError`` if ``ranked`` is empty: the empty/no-results
    report path is Phase 4 scope, not this task's (see module docstring).
    """
    if not ranked:
        raise ValueError(
            "render_markdown_report requires at least one ranked itinerary -- the empty/"
            "no-results report path (finding 0.5's NO_RESULTS/FAILED RunStatus values) is "
            "Phase 4 scope (T26/T27), not v1's"
        )

    top, others = ranked[0], ranked[1:]

    sections = [
        SYNTHETIC_DATA_BANNER,
        "",
        "This report is generated entirely from `MockProvider` synthetic data. No real "
        "fares, schedules, or booking links appear anywhere in this document.",
        "",
        f"# Flight Report — {departure_date.isoformat()}",
        "",
        _recommended_flight_block(top, data_source=data_source),
        "",
    ]
    if others:
        sections.append(_other_good_options_table(others))
        sections.append("")

    sections.append(
        f"**Summary:** the recommended flight above is the top-ranked itinerary out of "
        f"{accepted_count} valid itinerary(ies) found for {departure_date.isoformat()}, "
        f"with an adjusted score of {top.components.adjusted_score}. Report generated "
        f"{generated_at.isoformat()}."
    )

    return "\n".join(sections) + "\n"
