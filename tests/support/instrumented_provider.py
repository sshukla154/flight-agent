"""``InstrumentedProvider`` -- a scriptable, call-recording ``FlightProvider``
test double (T25's own named deliverable in the master plan's Phase 4 list).

Satisfies ``FlightProvider`` structurally (no inheritance -- see
``providers/base.py``'s own docstring on why the protocol is a ``Protocol``
rather than an ABC): this class just has the right ``capabilities``
property and ``search`` coroutine shape.

Three things this double gives a test that the executor's retry loop (T25)
and later integration tests (T28) need:

1. A full call log (``call_log``) -- every call recorded with its origin,
   destination and timestamp, queryable after the run.
2. Peak concurrency tracking (``peak_in_flight``) -- incremented on entry,
   decremented on exit, so a test can assert the executor's semaphore
   really bounded how many calls this provider ever had in flight at once.
3. A per-destination SCRIPT: a sequence of ``ScriptStep`` (``Succeed`` or
   ``Fail``) describing what the Nth call for that destination should do.
   This is what lets a retry test say "fail twice, then succeed" and a
   partial-failure test say "always fails", per destination, independent
   of every other destination in the same run.

T28: an optional ``call_delay_seconds`` constructor knob (default ``0.0``,
so every T25 call site above is unaffected) makes concurrent overlap
actually observable end to end through the real CLI. Without it, a call
that never awaits anything runs to completion atomically before the event
loop ever switches to a sibling task -- the same reasoning
``tests/unit/test_orchestrator.py``'s own inline ``_ConcurrencyTrackingProvider``
already documents for the executor-level peak-concurrency test; this just
gives the SHARED double the identical capability so an integration test can
assert peak concurrency against ``InstrumentedProvider``'s own call log
instead of a second, throwaway fake. The delay is placed between the
in-flight increment and decrement (not before either), so it is the window
``peak_in_flight`` measures that actually widens, not some window outside it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

from flightagent.domain.itinerary import RawOffer
from flightagent.domain.run import SearchRequest
from flightagent.providers.base import CallBudget, ProviderCapabilities, ProviderSearchResult
from flightagent.providers.errors import ProviderError
from flightagent.providers.mock.generator import generate_offers

INSTRUMENTED_API_VERSION = "instrumented-v1"


@dataclass(frozen=True)
class Succeed:
    """Scripted step: this call succeeds with ``offer_count`` offers.

    Offers are built via ``providers.mock.generator.generate_offers`` (the
    same deterministic generator ``MockProvider`` uses), then cycled to
    exactly ``offer_count`` entries -- so every offer this double ever
    returns is a real, schema-valid ``RawOffer``, never a hand-rolled stub
    that happens to satisfy today's fields but silently rots as the
    domain model grows.
    """

    offer_count: int = 1


@dataclass(frozen=True)
class Fail:
    """Scripted step: this call raises ``exception``.

    ``exception`` is a concrete, already-constructed ``ProviderError``
    instance (e.g. ``ProviderTimeoutError("boom", provider="x")``) so a
    test can control not just the error's type/retryability but also its
    message and, for ``ProviderRateLimitedError``, its ``retry_after``.
    The same instance is raised on every call that lands on this step
    (harmless to re-raise -- Python attaches a fresh traceback each time),
    which is what makes an "always fails" script as simple as a single
    ``Fail(...)`` entry with no ``Succeed`` ever following it.
    """

    exception: ProviderError


ScriptStep = Succeed | Fail


@dataclass(frozen=True)
class CallRecord:
    """One recorded call: which (origin, destination, max_stops) it
    searched, and when.

    ``max_stops`` (Phase 5, T29): with the CLI's ``--all-destinations``
    path now searching BOTH modes per destination, a call log keyed only
    by destination could no longer tell two calls to the same destination
    apart -- this field is what lets a test assert "one direct call and one
    one-stop call per destination", not just "two calls".
    """

    origin: str
    destination: str
    timestamp: datetime
    max_stops: int


def _offers_for(request: SearchRequest, offer_count: int) -> tuple[RawOffer, ...]:
    """``offer_count`` schema-valid offers for ``request``, cycling through
    ``generate_offers``'s deterministic output so any non-negative count is
    satisfiable regardless of how many offers the generator itself produces
    for a given request shape.

    Some request shapes legitimately generate ZERO offers (Phase 5, T29:
    ``providers.mock.generator``'s no-direct-service destinations) -- in
    that case there is nothing to cycle through, so this returns ``()``
    regardless of ``offer_count`` rather than raising ``ZeroDivisionError``
    trying to index modulo an empty sequence. A script asking for offers
    from a request shape the real generator says has none simply gets none;
    that is the honest answer, not a bug in this double.
    """
    if offer_count <= 0:
        return ()
    generated = generate_offers(request)
    if not generated:
        return ()
    return tuple(generated[i % len(generated)] for i in range(offer_count))


class InstrumentedProvider:
    """Scriptable, call-recording ``FlightProvider`` test double.

    ``scripts`` maps destination IATA code -> an ordered sequence of
    ``ScriptStep``. The Nth call (0-indexed) to that destination, AT A
    GIVEN ``max_stops`` MODE, consults ``scripts[destination][N]``; once
    that mode's own script is exhausted, its LAST step repeats forever --
    this is deliberate, not an oversight, so ``scripts={"DEL": [Fail(...)]}``
    reads naturally as "DEL always fails" and ``scripts={"DEL": [Fail(...),
    Fail(...), Succeed(2)]}`` reads as "DEL fails twice then succeeds and
    keeps succeeding", without a test having to pad either script out to an
    arbitrary length.

    Phase 5 (T29): the call index is tracked PER ``(destination, max_stops)``
    pair, not per destination alone. Before dual-mode search existed, a run
    only ever searched one mode per destination, so the two were
    equivalent; now that ``--all-destinations`` searches both modes
    concurrently, sharing one counter across them would make a
    "fail-twice-then-succeed" script for a destination consumed
    unpredictably by whichever mode's task happened to call first --
    genuinely nondeterministic under concurrent dispatch, not merely
    inconvenient. Each mode instead replays the SAME script independently
    from its own index 0, so ``scripts={"HYD": [Fail(...), Fail(...),
    Succeed(2)]}`` means "HYD's direct search fails twice then succeeds,
    AND, independently, HYD's one-stop search fails twice then succeeds" --
    deterministic regardless of scheduling order between the two.
    ``call_count(destination)`` still reports the TOTAL across both modes.

    A destination with no script at all defaults to ``Succeed(1)`` on
    every call -- an unconfigured destination just works, which matters
    for concurrency-focused tests that don't care about any particular
    destination's outcome.
    """

    def __init__(
        self,
        *,
        scripts: dict[str, list[ScriptStep]] | None = None,
        provider_name: str = "instrumented",
        call_delay_seconds: float = 0.0,
    ) -> None:
        self._scripts: dict[str, tuple[ScriptStep, ...]] = {
            destination: tuple(steps) for destination, steps in (scripts or {}).items()
        }
        self._call_counts: dict[tuple[str, int], int] = {}
        self._provider_name = provider_name
        self._call_delay_seconds = call_delay_seconds
        self.call_log: list[CallRecord] = []
        self._in_flight = 0
        self.peak_in_flight = 0

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_name=self._provider_name,
            api_version=INSTRUMENTED_API_VERSION,
            auth_style="none",
            paginated=False,
            native_currency_forceable=True,
            returns_booking_url=False,
            stop_filter_style="nonstop_boolean",
        )

    def call_count(self, destination: str) -> int:
        """How many calls this destination has received so far, summed
        across BOTH ``max_stops`` modes (Phase 5, T29)."""
        return sum(
            count for (dest, _max_stops), count in self._call_counts.items() if dest == destination
        )

    def call_count_for_mode(self, destination: str, max_stops: int) -> int:
        """How many calls this destination has received at exactly
        ``max_stops`` (Phase 5, T29) -- the per-mode counterpart to
        ``call_count``, for tests that care about one mode specifically."""
        return self._call_counts.get((destination, max_stops), 0)

    def calls_for(self, destination: str) -> tuple[CallRecord, ...]:
        """Every recorded call for ``destination``, in call order,
        across BOTH modes."""
        return tuple(record for record in self.call_log if record.destination == destination)

    async def search(self, request: SearchRequest, budget: CallBudget) -> ProviderSearchResult:
        self._in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self._in_flight)
        try:
            if self._call_delay_seconds > 0:
                # Deliberately INSIDE the try/finally, between the
                # increment and decrement above/below -- see the module
                # docstring's T28 note. Sleeping here, not before the
                # increment, is what lets every concurrently-dispatched
                # call actually overlap in ``peak_in_flight`` instead of
                # each one running to completion atomically before the
                # event loop ever switches to a sibling task.
                await asyncio.sleep(self._call_delay_seconds)
            destination = request.destination
            mode_key = (destination, request.max_stops)
            call_index = self._call_counts.get(mode_key, 0)
            self._call_counts[mode_key] = call_index + 1
            self.call_log.append(
                CallRecord(
                    origin=request.origin,
                    destination=destination,
                    timestamp=datetime.now(UTC),
                    max_stops=request.max_stops,
                )
            )

            step = self._step_for(destination, call_index)
            if isinstance(step, Fail):
                raise step.exception

            offers = _offers_for(request, step.offer_count)
            return ProviderSearchResult(
                offers=offers,
                truncated=False,
                pages_fetched=1,
                http_calls=1,
                raw_payload_refs=tuple(offer.raw_payload_ref for offer in offers),
            )
        finally:
            self._in_flight -= 1

    def _step_for(self, destination: str, call_index: int) -> ScriptStep:
        script = self._scripts.get(destination)
        if not script:
            return Succeed(offer_count=1)
        if call_index < len(script):
            return script[call_index]
        return script[-1]


__all__ = [
    "CallRecord",
    "Fail",
    "InstrumentedProvider",
    "ScriptStep",
    "Succeed",
]
