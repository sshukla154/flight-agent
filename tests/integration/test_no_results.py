"""Integration: the NO_RESULTS/D19 contract, proved end to end through the
real CLI (T28).

Two NO_RESULTS scenarios plus a regression guard for the pre-Phase-4
single-destination path.

**A confirmed, pre-existing gap this file works around (found while
building this test, not a hypothetical):** ``domain.run.RunEnvelope``'s own
status validator requires ``RunStatus.NO_RESULTS`` to have at least one
task in ``TaskState.OK``/``NO_OFFERS`` (see ``domain/run.py``'s
``_validate_status``). If literally EVERY one of the 8 planned tasks ends
in ``TaskState.ALL_REJECTED`` (offers existed, all were rejected by
validation, no task ever had zero offers and no task ever errored),
``cli.py``'s own ``_compute_run_status`` still computes a NO_RESULTS
candidate -- but constructing the actual ``RunEnvelope`` with that status
then raises ``pydantic.ValidationError`` from the model itself, because
``has_successful`` (>=1 OK/NO_OFFERS task) is false. ``cli.py``'s own
``_compute_run_status`` docstring already names this exact combination as
"a real gap in the already-built domain model (verified empirically, not a
hypothetical) ... flagged separately as a follow-up rather than patched
here" -- confirmed again here by literally constructing that scenario
against the real CLI and observing the unhandled ``ValidationError``
(``result.exception``) instead of a clean NO_RESULTS report.

Both scenarios below therefore leave exactly ONE of the 8 destinations
returning zero offers (``TaskState.NO_OFFERS``, an honest "the route
doesn't exist" case, per D18's own cell-state table) rather than also
rejecting it on the same rule as the other seven -- this keeps
``has_successful`` true and the run genuinely NO_RESULTS, without
papering over or silently avoiding the underlying gap: it is called out
explicitly here, not hidden. The dominant rejection code is computed only
from the SEVEN destinations that did return (and reject) an offer, and
stays unambiguous either way (one rejection code contributes 100% of the
tally in each scenario).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flightagent.cli import (
    _SPEC_NO_RESULTS_MESSAGE,
    NO_VALID_ITINERARIES_EXIT_CODE,
    app,
)
from flightagent.domain.itinerary import Leg, RawOffer
from flightagent.domain.money import Money
from flightagent.domain.run import SearchRequest
from flightagent.domain.segment import Layover, Segment
from flightagent.providers.base import CallBudget, ProviderCapabilities, ProviderSearchResult

runner = CliRunner()

_ORIGIN = "AMS"
_NO_OFFERS_DESTINATION = "VNS"
"""The one destination left at zero offers in both NO_RESULTS scenarios --
see this module's own docstring for exactly why."""

_HUB = "DXB"

_ALL_DESTINATIONS_ARGS = [
    "run",
    "--origin",
    _ORIGIN,
    "--date",
    "2027-07-17",
    "--max-stops",
    "1",
    "--all-destinations",
]


@pytest.fixture
def isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _build_fixed_offer(
    request: SearchRequest, *, layover_minutes: int, destination_override: str | None = None
) -> RawOffer:
    """One hand-built, schema-valid one-stop ``RawOffer`` through ``_HUB``
    with an EXACT layover duration -- unlike
    ``providers.mock.generator.generate_offers``, which always guarantees
    its first offer's layover lands inside the REQUEST's own
    ``[layover_min, layover_max]`` window (by construction, it can never
    produce an out-of-window "guaranteed" offer), this gives full control
    over the one property these tests need to pin exactly.

    Both ends use the ``"UTC"`` IANA zone -- a real, valid ``tzdata``
    entry -- specifically so local == UTC and every DST/fold concern
    ``domain.segment.Segment`` would otherwise validate is trivially
    satisfied; the actual wall-clock zone of any airport involved is not
    what these tests are about.

    ``destination_override``, when given, builds an itinerary that
    ARRIVES somewhere other than ``request.destination`` -- the
    ``RejectionCode.DESTINATION_MISMATCH`` scenario below.
    """
    actual_destination = (
        destination_override if destination_override is not None else request.destination
    )
    depart_utc = datetime.combine(request.departure_date, time(9, 0), tzinfo=UTC)
    outbound_duration = timedelta(hours=6)
    inbound_duration = timedelta(hours=3)

    outbound_arrive_utc = depart_utc + outbound_duration
    inbound_depart_utc = outbound_arrive_utc + timedelta(minutes=layover_minutes)
    inbound_arrive_utc = inbound_depart_utc + inbound_duration

    outbound = Segment(
        segment_id=f"{request.origin}-{_HUB}-0",
        origin=request.origin,
        destination=_HUB,
        depart_utc=depart_utc,
        arrive_utc=outbound_arrive_utc,
        depart_local=depart_utc,
        arrive_local=outbound_arrive_utc,
        origin_tz="UTC",
        destination_tz="UTC",
        marketing_carrier="EK",
        flight_number="101",
        cabin=request.cabin,
        duration=outbound_duration,
    )
    inbound = Segment(
        segment_id=f"{_HUB}-{actual_destination}-0",
        origin=_HUB,
        destination=actual_destination,
        depart_utc=inbound_depart_utc,
        arrive_utc=inbound_arrive_utc,
        depart_local=inbound_depart_utc,
        arrive_local=inbound_arrive_utc,
        origin_tz="UTC",
        destination_tz="UTC",
        marketing_carrier="EK",
        flight_number="102",
        cabin=request.cabin,
        duration=inbound_duration,
    )
    layover = Layover(
        airport=_HUB,
        arrive_utc=outbound_arrive_utc,
        depart_utc=inbound_depart_utc,
        duration=timedelta(minutes=layover_minutes),
        local_window=(outbound_arrive_utc, inbound_depart_utc),
        requires_airport_change=False,
        requires_terminal_change=False,
    )
    leg = Leg(segments=(outbound, inbound), layovers=(layover,))
    return RawOffer(
        provider="fixed",
        provider_offer_id=f"fixed-{request.origin}-{actual_destination}",
        legs=(leg,),
        price=Money(amount=Decimal("700.00"), currency="EUR"),
        raw_payload_ref=f"fixed://{request.origin}-{actual_destination}",
        provider_booking_url=None,
    )


class _FixedOfferProvider:
    """A trivial ``FlightProvider`` (structural, see ``providers/base.py``):
    every destination except ``no_offers_destination`` returns exactly one
    ``_build_fixed_offer(..., layover_minutes=layover_minutes,
    destination_override=destination_override)``; that one destination
    returns zero offers (``TaskState.NO_OFFERS``) -- see this module's own
    docstring for why exactly one destination is carved out this way.
    """

    def __init__(
        self,
        *,
        layover_minutes: int,
        destination_override: str | None = None,
        no_offers_destination: str = _NO_OFFERS_DESTINATION,
    ) -> None:
        self._layover_minutes = layover_minutes
        self._destination_override = destination_override
        self._no_offers_destination = no_offers_destination

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_name="fixed",
            api_version="fixed-v1",
            auth_style="none",
            paginated=False,
            native_currency_forceable=True,
            returns_booking_url=False,
            stop_filter_style="nonstop_boolean",
        )

    async def search(self, request: SearchRequest, budget: CallBudget) -> ProviderSearchResult:
        if request.destination == self._no_offers_destination:
            return ProviderSearchResult(offers=(), truncated=False, pages_fetched=1, http_calls=1)
        offer = _build_fixed_offer(
            request,
            layover_minutes=self._layover_minutes,
            destination_override=self._destination_override,
        )
        return ProviderSearchResult(
            offers=(offer,),
            truncated=False,
            pages_fetched=1,
            http_calls=1,
            raw_payload_refs=(offer.raw_payload_ref,),
        )


class TestLayoverTooShortIsTheSpecMessage:
    def test_dominant_layover_rejection_prints_the_exact_spec_string(
        self, isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Outside the closed [180, 360] validity window (D8) on the short
        # side -- every one of the 7 offering destinations rejects with
        # RejectionCode.LAYOVER_TOO_SHORT, and only that code.
        provider = _FixedOfferProvider(layover_minutes=120)
        monkeypatch.setattr("flightagent.cli._build_provider", lambda name: provider)

        result = runner.invoke(app, _ALL_DESTINATIONS_ARGS)

        assert result.exit_code == NO_VALID_ITINERARIES_EXIT_CODE, result.output
        assert not (isolated_cwd / "out").exists()

        # The exact spec JSON string, CHARACTER FOR CHARACTER -- including
        # the en dash -- printed to stdout (never stderr; this is the one
        # NO_RESULTS branch that is a plain, parseable stdout line).
        expected = json.dumps(
            {"status": "no_results", "message": _SPEC_NO_RESULTS_MESSAGE}, ensure_ascii=False
        )
        assert result.stdout.strip() == expected
        assert "3–6 hour layover rule" in result.stdout


class TestDominantRejectionOtherThanLayoverGetsAnHonestMessage:
    def test_destination_mismatch_dominant_code_does_not_print_the_layover_string(
        self, isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A valid mid-window layover (240min, D9's "exactly 4h" band) so
        # LAYOVER_TOO_SHORT/LONG never fires -- the ONLY rejection any of
        # the 7 offering destinations can hit is DESTINATION_MISMATCH,
        # because every offer deliberately arrives at "ZZZ" instead of the
        # requested destination.
        provider = _FixedOfferProvider(layover_minutes=240, destination_override="ZZZ")
        monkeypatch.setattr("flightagent.cli._build_provider", lambda name: provider)

        result = runner.invoke(app, _ALL_DESTINATIONS_ARGS)

        assert result.exit_code == NO_VALID_ITINERARIES_EXIT_CODE, result.output
        assert not (isolated_cwd / "out").exists()

        # finding 0.5's fix in action: NO_RESULTS alone never triggers the
        # spec's hardcoded string -- only a LAYOVER_TOO_SHORT/LONG dominant
        # code does. Here the dominant code is destination_mismatch, so an
        # accurate message naming that reason is printed instead.
        assert "3–6 hour layover rule" not in result.stdout
        assert "3–6 hour layover rule" not in result.stderr
        assert "no_results" in result.stderr
        assert "destination_mismatch" in result.stderr


class TestSingleDestinationRegressionGuard:
    """Phase 2/3's original single-destination invocation -- ``--dest``,
    no ``--all-destinations`` -- must still exit 0 and behave exactly as
    before, unaffected by everything Phase 4 (T24-T27) added alongside it.
    Uses the real, unmodified ``MockProvider`` (D6's default), the same
    target invocation ``tests/unit/test_cli.py::TestTargetInvocation``
    already pins byte-for-byte, restated here as the regression guard this
    task brief explicitly asks for: origin AMS (a required flag no earlier
    phase's invocation omitted), destination DEL, date 2027-07-17,
    max-stops 1.
    """

    def test_single_destination_dest_del_still_exits_zero(self, isolated_cwd: Path) -> None:
        result = runner.invoke(
            app,
            [
                "run",
                "--origin",
                _ORIGIN,
                "--dest",
                "DEL",
                "--date",
                "2027-07-17",
                "--max-stops",
                "1",
            ],
        )

        assert result.exit_code == 0, result.output
        report_path = isolated_cwd / "out" / "flight_report_2027-07-17.md"
        results_path = isolated_cwd / "out" / "flight_results_2027-07-17.json"
        assert report_path.is_file()
        assert results_path.is_file()

        assert "SYNTHETIC DATA" in report_path.read_text(encoding="utf-8")
        results = json.loads(results_path.read_text(encoding="utf-8"))
        assert results["data_source"] == "mock"
        assert results["accepted_count"] >= 1
