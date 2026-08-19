"""Markdown report renderer v1 (T15).

Builds to the ORIGINAL spec's section 6 "Expected Output Format", quoted
in this task's brief -- not an invented format:

- A "Recommended Flight" block with exactly seven fields: Airline, Route,
  Departure, Layover, Arrival, Price (EUR), Booking.
- An "Other Good Options" table with exactly four columns: Airline |
  Route | Layover | Price.

Phase 5 (T33) adds the "Direct Flight Analysis" section: Addendum 1's own
five-column table (Destination | Airline | Price | Difference vs Cheapest
Stop | Recommendation), one row per registry destination regardless of
tier -- D15's amendment is explicit that this table is the only place a
NOT_AVAILABLE/all-tier destination is visible at all once the global
top-N ranking has truncated the main list. Rendered whenever the caller
supplies at least one ``DestinationAnalysis``; empty (the pre-Phase-5
default) renders no such section, matching every pre-Phase-5 call site.

``render_markdown_report`` still requires at least one ranked itinerary;
the empty/no-results report path (finding 0.5's NO_RESULTS/FAILED
``RunStatus`` values) remains out of this function's scope.

Phase 6 (T41) adds three more sections, all optional and additive like
every other post-v1 section documented above:

- "Origin Comparison" (master plan acceptance criterion A2-5c): one row
  per ``OriginSummary`` (T40, ``scoring.origin_summary.summarize_by_origin``),
  rendered whenever the caller supplies a non-empty ``origin_summaries``.
  An origin with no accepted itinerary still gets a row -- a dash plus an
  explicit reason (``reporting.view.origin_absence_reason``, read off
  ``task_outcomes``), never a silently missing row.
- "Self-Transfer Itineraries" (D5): one row per ``RejectedItinerary``
  carrying a ``RejectionCode.SELF_TRANSFER`` rejection, rendered whenever
  the caller supplies a non-empty ``self_transfer_rejections`` -- these
  never appear in "Recommended Flight"/"Other Good Options" (D5 excludes
  them from the valid ranked set entirely), visible here for audit only.
- D15's "top 3 per destination" is deliberately NOT rendered here -- D15's
  own wording scopes it to "additionally in the JSON"
  (``reporting.json_report``'s ``top_itineraries_by_destination``), not the
  Markdown report.

Phase 4 (T26) adds the "Failed Searches" section: rendered only when at
least one ``TaskOutcome`` in the run's ledger is in an error state
(PROVIDER_ERROR / RATE_LIMITED / TIMEOUT, ``domain.run``'s
``_ERROR_STATES``) -- never an empty heading when every task succeeded.

Phase 6 (T39) adds the "Early Stop Analysis" section: D12's EUR-threshold
early-stop rule, replayed post-hoc over the complete result set
(``orchestration.waves.replay_early_stop``) and rendered purely as an
annotation -- the full fan-out above always ran regardless of what this
table says. Rendered only when the caller supplies a non-empty
``{destination: EarlyStopEvaluation}`` mapping.

Phase 5 (T34) adds the closing "Final Summary" line: one of two prose
templates (A1-8's restated acceptance criterion, DECISIONS.md), chosen by
the tier of the ``DestinationAnalysis`` belonging to the report's OVERALL
best destination -- ``ranked[0]`` (``top`` below), the exact same
itinerary T15/T33 already treat as "the primary result" for the
"Recommended Flight" block, never a second, independently-chosen notion
of "the main destination". Every number in the sentence (the EUR price
difference, the whole-hours time saving) is read directly off that
``DestinationAnalysis``, never hardcoded -- rendered only when
``destination_analyses`` contains a matching, price-comparable entry for
that destination, so a pre-Phase-5 call site (no ``destination_analyses``)
or a NOT_AVAILABLE/direct-only primary destination (nothing priced to
state a difference from) renders no such line at all.

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

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from types import MappingProxyType

from flightagent.domain.enums import DirectTier
from flightagent.domain.itinerary import NormalizedItinerary
from flightagent.domain.money import Money
from flightagent.domain.policy import DestinationAnalysis, EarlyStopEvaluation
from flightagent.domain.run import TaskOutcome
from flightagent.domain.scoring import OriginSummary, ScoredItinerary
from flightagent.domain.validation import RejectedItinerary
from flightagent.reporting.booking_link import (
    BookingUrlRejected,
    DataSource,
    markdown_link,
    validate_booking_url,
)
from flightagent.reporting.view import (
    airline_string,
    destination_from_task_id,
    direct_tier_recommendation_label,
    failed_task_outcomes,
    first_segment,
    format_layover,
    format_price_eur,
    last_segment,
    origin_absence_reason,
    route_string,
    self_transfer_airports,
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


def _booking_field(itinerary: NormalizedItinerary) -> str:
    """The "Booking" field's rendered value -- never a raw, unvalidated
    URL (master plan S8.2: the booking-link validator is called at both
    ingestion, out of scope here, and render, which is exactly this
    function).

    The validation policy (``DataSource``) is derived from THIS
    itinerary's own ``provider`` field, never from a single value shared
    across the whole report -- master plan S8.6: "destinations are
    searched concurrently... and can legitimately differ... in data
    source within one run." Applying one document-level policy to every
    itinerary regardless of which provider actually produced it would
    silently apply the wrong safety check (e.g. requiring a mock-reserved
    domain, or skipping that check) to an itinerary that didn't come from
    that provider.
    """
    if itinerary.booking_url is None:
        return _NO_BOOKING_LINK_TEXT
    data_source: DataSource = "mock" if itinerary.provider == "mock" else "live"
    try:
        validated = validate_booking_url(str(itinerary.booking_url), data_source=data_source)
        link_text = f"Book with {airline_string(itinerary)}"
        return markdown_link(link_text, validated.url)
    except BookingUrlRejected:
        return _WITHHELD_BOOKING_LINK_TEXT


def _recommended_flight_block(item: ScoredItinerary) -> str:
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
        f"- **Booking:** {_booking_field(itinerary)}",
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


def _escape_table_cell(text: str) -> str:
    """Neutralize characters that would otherwise corrupt a Markdown table
    row -- a literal ``|`` splits into a phantom extra column, and an
    embedded newline breaks the row onto multiple lines. ``error_detail``
    is free-form text from a provider error message (S1), not a value this
    codebase controls the shape of, so this is not a hypothetical input.
    """
    return text.replace("|", "\\|").replace("\n", " ")


_NO_DIRECT_ITINERARY_TEXT = "–"
"""En dash, D18's own "no value to show" convention (the fare-matrix's
``NO_OFFERS`` cell) reused here for a ``DestinationAnalysis`` with no
``cheapest_direct`` (NOT_AVAILABLE) or no ``price_difference`` (the
degenerate "direct exists, no one-stop alternative" case, T31) -- never a
fabricated 0.00 or an empty cell that could be mistaken for a rendering
bug."""

_DIRECT_TIER_STAR = "★ "
"""Prefixed onto the Recommendation cell's text for ``DirectTier.RECOMMENDED``
only -- a display-only visual marker (this task's own "consider a visual
marker like the spec's own star" note), layered on top of
``reporting.view.direct_tier_recommendation_label``'s shared plain-text
label rather than baked into it, so the JSON artifact's ``recommendation``
field (which reuses that same shared label) stays star-free plain text."""


def _direct_tier_recommendation_cell(tier: DirectTier) -> str:
    label = direct_tier_recommendation_label(tier)
    if tier == DirectTier.RECOMMENDED:
        return f"{_DIRECT_TIER_STAR}{label}"
    return label


def _direct_flight_analysis_table(analyses: Sequence[DestinationAnalysis]) -> str:
    """Addendum 1's exact 5-column table (D10, T33): one row per
    ``DestinationAnalysis``, in the order given -- ALL of them, regardless
    of tier, per D15's amendment (a NOT_AVAILABLE destination has to stay
    visible here even though the global top-N ranking may have dropped it
    from every other section entirely).

    Airline/Price come from ``cheapest_direct`` -- this table is about the
    DIRECT option specifically -- and render as
    ``_NO_DIRECT_ITINERARY_TEXT`` when no direct service exists at all
    (``tier == NOT_AVAILABLE``, ``cheapest_direct is None``). Difference
    vs Cheapest Stop renders the same placeholder whenever
    ``price_difference`` is ``None`` -- either that same NOT_AVAILABLE
    case, or the degenerate "direct exists, no valid one-stop alternative"
    case (T31), where there is nothing priced to compare against.

    Any destination with ``score_policy_divergence`` set appends one
    sentence per destination after the table (finding 0.1) -- the ranked
    list's ``adjusted_score`` ordering and this table's tier can
    legitimately disagree above roughly a EUR755 one-stop fare, and the
    report must say so rather than silently contradicting itself two
    sections apart.
    """
    lines = [
        "## Direct Flight Analysis",
        "",
        "| Destination | Airline | Price | Difference vs Cheapest Stop | Recommendation |",
        "|---|---|---|---|---|",
    ]
    for analysis in analyses:
        if analysis.cheapest_direct is not None:
            airline = airline_string(analysis.cheapest_direct)
            price = format_price_eur(analysis.cheapest_direct.price_eur)
        else:
            airline = _NO_DIRECT_ITINERARY_TEXT
            price = _NO_DIRECT_ITINERARY_TEXT

        difference = (
            format_price_eur(analysis.price_difference)
            if analysis.price_difference is not None
            else _NO_DIRECT_ITINERARY_TEXT
        )
        recommendation = _direct_tier_recommendation_cell(analysis.tier)

        lines.append(
            f"| {analysis.destination} | {airline} | {price} | {difference} | "
            f"{recommendation} |"
        )

    divergent = [analysis for analysis in analyses if analysis.score_policy_divergence]
    if divergent:
        lines.append("")
        lines.append(
            "**Note:** for the destination(s) below, this table's recommendation and the "
            "ranked list's `adjusted_score` ordering disagree (finding 0.1):"
        )
        for analysis in divergent:
            lines.append(f"- **{analysis.destination}:** {analysis.divergence_explanation}")

    return "\n".join(lines)


def _failed_searches_table(failed: Sequence[TaskOutcome]) -> str:
    lines = [
        "## Failed Searches",
        "",
        "| Destination | Error Type | Detail |",
        "|---|---|---|",
    ]
    for outcome in failed:
        destination = destination_from_task_id(outcome.task_id)
        error_type = outcome.error_type if outcome.error_type is not None else "unknown"
        error_detail = (
            outcome.error_detail if outcome.error_detail is not None else "(no detail)"
        )
        lines.append(
            f"| {_escape_table_cell(destination)} | {_escape_table_cell(error_type)} | "
            f"{_escape_table_cell(error_detail)} |"
        )
    return "\n".join(lines)


def _format_ground_minutes(minutes: int | None) -> str:
    """``"45m"``/``"2h 5m"`` for a known ground-access duration, or the
    shared "no value" dash when ``minutes`` is ``None`` (``OriginSummary.ground_minutes``
    is ``None`` whenever that origin has no accepted itinerary to read a
    ``GroundLeg`` off of -- see that computed field's own docstring).
    Deliberately NOT ``reporting.view.format_layover``: that function's
    ``0 -> "Direct (no layover)"`` special case is about a FLIGHT layover,
    not ground-access time, and would misrepresent a (hypothetical)
    zero-minute ground leg as if it meant "no ground leg at all".
    """
    if minutes is None:
        return _NO_DIRECT_ITINERARY_TEXT
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins}m" if hours else f"{mins}m"


def _origin_comparison_table(
    summaries: Sequence[OriginSummary], *, task_outcomes: Sequence[TaskOutcome]
) -> str:
    """Master plan acceptance criterion A2-5c: one row per ``OriginSummary``
    (T40), in the order given -- ALL of them, matching
    ``scoring.origin_summary.summarize_by_origin``'s own "every origin gets
    a row, even an empty one" contract. An origin with no accepted
    itinerary (``summary.best is None``) still gets a row: the "Cheapest
    Fare" cell carries the shared dash placeholder plus an explicit,
    human-readable reason (``reporting.view.origin_absence_reason``, read
    off ``task_outcomes``) inline in that same cell -- never a silently
    missing row, and never a bare dash with no reason attached.
    """
    lines = [
        "## Origin Comparison",
        "",
        "| Origin | Ground Access | Cheapest Fare | Destination |",
        "|---|---|---|---|",
    ]
    for summary in summaries:
        ground = _format_ground_minutes(summary.ground_minutes)
        if summary.best is not None:
            fare = format_price_eur(summary.best.itinerary.price_eur)
            destination = last_segment(summary.best.itinerary).destination
        else:
            reason = origin_absence_reason(summary.origin, task_outcomes)
            fare = f"{_NO_DIRECT_ITINERARY_TEXT} ({reason})"
            destination = _NO_DIRECT_ITINERARY_TEXT
        lines.append(f"| {summary.origin} | {ground} | {fare} | {destination} |")
    return "\n".join(lines)


def _self_transfer_appendix_table(rejected: Sequence[RejectedItinerary]) -> str:
    """D5's "Self-transfer, not protected" appendix (T41): one row per
    ``RejectedItinerary`` carrying a ``RejectionCode.SELF_TRANSFER``
    rejection, in the order given. These itineraries are excluded from the
    valid ranked set entirely (D5) -- this table is the ONLY place one of
    them appears anywhere in the report, never the "Recommended
    Flight"/"Other Good Options" sections above.
    """
    lines = [
        "## Self-Transfer Itineraries (Excluded, Not Ranked)",
        "",
        "*Excluded from the valid ranked set (D5): a self-transfer / separate-ticket "
        "connection is not a protected itinerary, regardless of how generous its layover "
        "looks -- nothing protects the traveller if the first ticket runs late. Listed here "
        "for visibility only.*",
        "",
        "| Airline | Route | Price | Self-Transfer At | Reason |",
        "|---|---|---|---|---|",
    ]
    for entry in rejected:
        itinerary = entry.itinerary
        transfer_points = ", ".join(self_transfer_airports(itinerary))
        lines.append(
            f"| {airline_string(itinerary)} | {route_string(itinerary)} | "
            f"{format_price_eur(itinerary.price_eur)} | {transfer_points} | "
            f"{_escape_table_cell(entry.rejection.message)} |"
        )
    return "\n".join(lines)


_NOT_EVALUABLE_TEXT = "not yet evaluable (fewer than 2 prior origins)"
"""D12 rule 1 / finding 0.7: the rule needs >=2 prior origins with a valid
fare before it can compare anything at all. Rendered instead of a fake
"no" verdict for a destination where that threshold was never reached --
distinct from a real `triggered=False` verdict where a comparison DID run
and simply did not cross the threshold."""


def _early_stop_row(destination: str, evaluation: EarlyStopEvaluation) -> str:
    if evaluation.triggered:
        assert evaluation.triggering_origin is not None
        assert evaluation.margin is not None
        verdict = f"triggered at {evaluation.triggering_origin}"
        margin_text = format_price_eur(evaluation.margin)
    elif len(evaluation.compared_against) >= 2:
        verdict = "not triggered"
        margin_text = _NO_DIRECT_ITINERARY_TEXT
    else:
        verdict = _NOT_EVALUABLE_TEXT
        margin_text = _NO_DIRECT_ITINERARY_TEXT

    compared_against = (
        ", ".join(evaluation.compared_against) if evaluation.compared_against else "(none yet)"
    )
    return (
        f"| {destination} | wave {evaluation.evaluated_at_wave} | {verdict} | {margin_text} | "
        f"{compared_against} |"
    )


def _early_stop_table(early_stop_evaluations: Mapping[str, EarlyStopEvaluation]) -> str:
    """D12/T39's "Early Stop Analysis" section: what the EUR-threshold
    early-stop rule WOULD have done, replayed post-hoc over the complete
    result set (master plan S5's Option B) -- an annotation only, never a
    reflection of anything actually skipped (the full fan-out above always
    ran regardless of what this table says). One row per destination, in
    ``early_stop_evaluations``' own (registry destination) order.

    Every ``EarlyStopEvaluation`` carries ``mode="advisory"`` (T39 builds
    only the post-hoc replay -- see ``policy.early_stop.MODE``), so the
    heading states that plainly rather than repeating it in every row.
    """
    lines = [
        "## Early Stop Analysis",
        "",
        "*Advisory only -- early stop is off by default (D12), and this run always "
        "searched every origin regardless of what this table says. It shows what the "
        "EUR-threshold rule would have done, replayed after the fact.*",
        "",
        "| Destination | Evaluated at | Verdict | Margin | Compared against |",
        "|---|---|---|---|---|",
    ]
    for destination, evaluation in early_stop_evaluations.items():
        lines.append(_early_stop_row(destination, evaluation))
    return "\n".join(lines)


_FINAL_SUMMARY_DIRECT_TIERS = (DirectTier.RECOMMENDED, DirectTier.GOOD_VALUE)
"""Tiers for which the Final Summary (T34) uses the "recommend the direct
flight" template -- the same two tiers Addendum 1's report column renders
as some flavour of "Recommended" (``reporting.view``'s
``_DIRECT_TIER_RECOMMENDATION_LABELS``), reused here rather than a second,
independently-maintained tier grouping."""

_ONE_HOUR = timedelta(hours=1)
_HALF_HOUR = timedelta(minutes=30)


def _whole_hours_saved(time_saved: timedelta) -> int:
    """Round a (non-negative) ``time_saved`` to the nearest whole hour,
    half-up -- the spec's own "approximately 7 hours" phrasing implies
    rounding, not truncation. Computed via ``timedelta`` floor-division
    only (finding 0.3's convention: never route a number that ends up in a
    report through ``float``), never called on a negative ``time_saved``
    -- the caller omits the whole clause in that case instead.
    """
    return (time_saved + _HALF_HOUR) // _ONE_HOUR


def _hours_saved_clause(time_saved: timedelta | None) -> str:
    """`` and saves approximately {N} hour(s) of travel time"`` -- or the
    empty string when there is nothing true to say: ``time_saved`` is
    unknown (``None``) or negative (the direct itinerary is actually
    SLOWER, T32's design) never gets this clause, because the sentence
    must never state something false about the data.
    """
    if time_saved is None or time_saved < timedelta():
        return ""
    hours = _whole_hours_saved(time_saved)
    unit = "hour" if hours == 1 else "hours"
    return f" and saves approximately {hours} {unit} of travel time"


def _final_summary_sentence(analysis: DestinationAnalysis) -> str:
    """One of the two Final Summary templates (T34, A1-8), filled in from
    ``analysis`` -- never from the spec's own example numbers.

    Requires ``analysis.price_difference`` to be present: the caller
    (``render_markdown_report``) only invokes this once it has confirmed a
    real direct-vs-one-stop price comparison exists for the primary
    destination. A NOT_AVAILABLE tier (no direct service at all) or the
    "direct exists, no one-stop alternative" degenerate case (T31) has
    nothing priced to state a difference from, so neither template's claim
    would be true -- the caller renders no Final Summary line at all for
    those, rather than calling this function.
    """
    if analysis.price_difference is None:
        raise ValueError(
            "_final_summary_sentence requires analysis.price_difference to be present -- "
            "the caller must confirm a real direct-vs-one-stop comparison exists first"
        )
    diff_text = format_price_eur(analysis.price_difference)

    if analysis.tier in _FINAL_SUMMARY_DIRECT_TIERS:
        # DestinationAnalysis's own model_validator guarantees cheapest_direct
        # is present whenever tier != NOT_AVAILABLE.
        assert analysis.cheapest_direct is not None
        route = route_string(analysis.cheapest_direct)
        time_clause = _hours_saved_clause(analysis.time_saved)
        if analysis.price_difference.amount < 0:
            # The direct itinerary is not just tier-recommended but actually
            # CHEAPER than the one-stop alternative (price_difference has no
            # abs() by design -- see the module docstring). "Only €-X more
            # expensive" reads as broken English for a negative diff, so this
            # branch states the real relationship instead of the magnitude of
            # a negative number formatted as if it were a surcharge.
            cheaper_amount = -analysis.price_difference.amount
            cheaper_text = format_price_eur(
                Money(amount=cheaper_amount, currency=analysis.price_difference.currency)
            )
            return (
                f"The direct {route} flight is actually {cheaper_text} cheaper than the "
                f"cheapest valid one-stop option{time_clause}. I recommend choosing the "
                f"direct flight."
            )
        return (
            f"The direct {route} flight is only {diff_text} more expensive than the cheapest "
            f"valid one-stop option{time_clause}. I recommend choosing the direct flight."
        )

    return (
        f"The direct flight is {diff_text} more expensive than the cheapest valid one-stop "
        "itinerary, so the one-stop option offers significantly better value."
    )


def _primary_destination(top: ScoredItinerary) -> str:
    """The IATA code of the report's single primary result's destination
    -- ``ranked[0]`` (this function's caller's own ``top``), the exact
    itinerary T15/T33 already build the "Recommended Flight" block from.
    The Final Summary sentence (T34) judges this same destination's
    ``DestinationAnalysis``; there is no second notion of "the main
    destination" to invent.
    """
    return last_segment(top.itinerary).destination


def _primary_destination_analysis(
    top: ScoredItinerary, destination_analyses: Sequence[DestinationAnalysis]
) -> DestinationAnalysis | None:
    """The ``DestinationAnalysis`` matching ``top``'s destination, or
    ``None`` when ``destination_analyses`` is empty (no Phase 5 data
    supplied) or simply has no entry for that destination."""
    primary_destination = _primary_destination(top)
    for analysis in destination_analyses:
        if analysis.destination == primary_destination:
            return analysis
    return None


def render_markdown_report(
    ranked: Sequence[ScoredItinerary],
    *,
    departure_date: date,
    accepted_count: int,
    generated_at: datetime,
    task_outcomes: Sequence[TaskOutcome] = (),
    destination_analyses: Sequence[DestinationAnalysis] = (),
    early_stop_evaluations: Mapping[str, EarlyStopEvaluation] = MappingProxyType({}),
    origin_summaries: Sequence[OriginSummary] = (),
    self_transfer_rejections: Sequence[RejectedItinerary] = (),
) -> str:
    """Render the full v1 Markdown report.

    ``ranked`` is the already-ranked, already-top-N-truncated list (D15) --
    ``ranked[0]`` becomes the "Recommended Flight" block, and any remaining
    items become the "Other Good Options" table rows, in the order given
    (this function does not itself re-sort). ``accepted_count`` is the
    full valid-itinerary count *before* truncation, passed in separately
    per D15 ("truncation... applies to presentation only") -- this
    function never infers it from ``len(ranked)``.

    ``task_outcomes`` is the run's full task ledger (T26, Phase 4) --
    every ``TaskOutcome``, successful or not. This function filters it down
    to the error-state subset itself (``reporting.view.failed_task_outcomes``)
    and renders a "## Failed Searches" section listing each one's
    destination, error type, and detail -- but only when that subset is
    non-empty. Passing nothing (the default) renders no such section at
    all, matching the ``ranked``-only call sites this function already had
    before Phase 4.

    ``destination_analyses`` is one ``DestinationAnalysis`` per registry
    destination (Phase 5, T33, D10) -- rendered as the "## Direct Flight
    Analysis" section, ALL of them regardless of tier, whenever this
    sequence is non-empty. Passing nothing (the default) renders no such
    section, matching every pre-Phase-5 call site.

    The same ``destination_analyses`` also drives the closing "Final
    Summary" line (Phase 5, T34): whichever entry matches ``ranked[0]``'s
    destination (the report's one primary result) picks one of two prose
    templates by its ``tier``, filled in with its real EUR price
    difference and, when non-negative, its real whole-hours time saving.
    Rendered only when a matching, price-comparable entry exists; silently
    omitted otherwise (no ``destination_analyses``, no matching entry, or
    a NOT_AVAILABLE/direct-only primary destination) rather than stating
    something the data doesn't support.

    ``early_stop_evaluations`` is the T39 (Phase 6, D12) post-hoc replay
    annotation, keyed by destination (``orchestration.waves.replay_early_stop``)
    -- rendered as the "## Early Stop Analysis" section whenever this
    mapping is non-empty. Passing nothing (the default) renders no such
    section, matching every pre-Phase-6 call site. Purely informational --
    never changes ``ranked``/``accepted_count`` or anything else in this
    report.

    ``origin_summaries`` is T40's per-origin reduction
    (``scoring.origin_summary.summarize_by_origin``), one entry per
    configured origin -- rendered as the "## Origin Comparison" section
    (T41, master plan A2-5c) whenever this sequence is non-empty. Passing
    nothing (the default) renders no such section, matching every
    pre-Phase-6 call site.

    ``self_transfer_rejections`` is D5's excluded-itinerary set (T41): every
    itinerary rejected specifically for ``RejectionCode.SELF_TRANSFER``,
    paired with the ``Rejection`` that excluded it. Rendered as the
    "## Self-Transfer Itineraries" appendix whenever this sequence is
    non-empty -- separate from "## Failed Searches" (that section is for
    provider/task-level failures; this one is for an itinerary-level
    rejection on a specific rule). These itineraries never appear in
    "Recommended Flight"/"Other Good Options" regardless of this
    parameter -- D5 excludes them from ``ranked`` upstream, long before
    this function ever sees them.

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
    failed = failed_task_outcomes(task_outcomes)

    sections = [
        SYNTHETIC_DATA_BANNER,
        "",
        "This report is generated entirely from `MockProvider` synthetic data. No real "
        "fares, schedules, or booking links appear anywhere in this document.",
        "",
        f"# Flight Report — {departure_date.isoformat()}",
        "",
        _recommended_flight_block(top),
        "",
    ]
    if others:
        sections.append(_other_good_options_table(others))
        sections.append("")

    if origin_summaries:
        sections.append(_origin_comparison_table(origin_summaries, task_outcomes=task_outcomes))
        sections.append("")

    if destination_analyses:
        sections.append(_direct_flight_analysis_table(destination_analyses))
        sections.append("")

    if failed:
        sections.append(_failed_searches_table(failed))
        sections.append("")

    if early_stop_evaluations:
        sections.append(_early_stop_table(early_stop_evaluations))
        sections.append("")

    if self_transfer_rejections:
        sections.append(_self_transfer_appendix_table(self_transfer_rejections))
        sections.append("")

    sections.append(
        f"**Summary:** the recommended flight above is the top-ranked itinerary out of "
        f"{accepted_count} valid itinerary(ies) found for {departure_date.isoformat()}, "
        f"with an adjusted score of {top.components.adjusted_score}. Report generated "
        f"{generated_at.isoformat()}."
    )

    primary_analysis = _primary_destination_analysis(top, destination_analyses)
    if primary_analysis is not None and primary_analysis.price_difference is not None:
        sections.append("")
        sections.append(f"**Final Summary:** {_final_summary_sentence(primary_analysis)}")

    return "\n".join(sections) + "\n"
