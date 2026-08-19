"""Tests for the approval-record endpoint (T47).

Same isolation pattern as ``tests/unit/test_api.py``: every test builds its
own ``FlightAgentSettings`` via ``load_config(cli_overrides=...)`` pointed
at a ``tmp_path``-scoped ``runs_dir``, and drives the ``FastAPI`` app
through ``TestClient`` directly -- no real server process, no real
provider call (the mock provider is deterministic and synthetic already).

Beyond the ordinary correctness tests, this file carries the adversarial
regression guard this task's own brief calls for: proof (not just an
assertion) that ``routes_approval.py`` cannot trigger any external action,
because no booking/payment tool exists anywhere in this codebase for it to
trigger.
"""

from __future__ import annotations

import ast
import inspect
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from flightagent.api import routes_approval
from flightagent.api.app import create_app
from flightagent.api.auth import API_KEY_ENV_VAR
from flightagent.api.routes_approval import approval_path, render_approval_prompt
from flightagent.config.loader import load_config
from flightagent.config.models import FlightAgentSettings

_TEST_API_KEY = "test-api-key-not-a-real-secret"


def _isolated_settings(tmp_path: Path) -> FlightAgentSettings:
    """Same isolation ``test_api.py`` gets -- all three output paths
    redirected under ``tmp_path`` so a test run never touches this repo's
    own ``out/``/``data/runs``.
    """
    return load_config(
        env={},
        cli_overrides={
            "output": {
                "report_path": str(tmp_path / "out" / "flight_report_2027-07-17.md"),
                "results_path": str(tmp_path / "out" / "flight_results_2027-07-17.json"),
                "runs_dir": str(tmp_path / "data" / "runs"),
            }
        },
    )


@pytest.fixture
def settings(tmp_path: Path) -> FlightAgentSettings:
    return _isolated_settings(tmp_path)


@pytest.fixture
def client(
    settings: FlightAgentSettings, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    """See ``test_api.py``'s identical fixture docstring -- ``create_app()``
    now requires ``FLIGHTAGENT_API_KEY`` (Phase 8b, ``api.auth``)."""
    monkeypatch.setenv(API_KEY_ENV_VAR, _TEST_API_KEY)
    return TestClient(create_app(settings=settings), headers={"X-Api-Key": _TEST_API_KEY})


_SEARCH_BODY = {
    "origin": "AMS",
    "date": "2027-07-17",
    "max_stops": 1,
    "dest": "DEL",
    "provider": "mock",
}


def _create_real_run(client: TestClient) -> str:
    """Runs the real mock-provider search pipeline (same body
    ``test_api.py`` uses) and returns the ``run_id`` of a run that
    genuinely produced >=1 accepted itinerary.
    """
    response = client.post("/search", json=_SEARCH_BODY)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "complete"
    assert body["accepted_count"] >= 1
    return str(body["run_id"])


class TestApprovalPromptRendering:
    """The rendered prompt matches the spec's exact wording pattern with
    the run's own real top-ranked-itinerary price substituted in.
    """

    def test_prompt_matches_spec_wording_with_real_price(
        self, client: TestClient, settings: FlightAgentSettings
    ) -> None:
        run_id = _create_real_run(client)
        results_path = Path(settings.output.runs_dir) / run_id / "results.json"
        results = json.loads(results_path.read_text(encoding="utf-8"))
        expected_price = results["top_itineraries"][0]["price_eur"]
        assert expected_price  # sanity: the fixture run really produced a priced itinerary

        response = client.post(f"/runs/{run_id}/approve", json={"approved": True})

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["price_eur"] == expected_price
        expected_prompt = (
            f"I found a valid itinerary for €{expected_price}. "
            "Do you approve proceeding to booking?"
        )
        assert body["prompt"] == expected_prompt
        # And the pure rendering function, independently, produces the identical string --
        # not just "the route happens to format it this way once".
        assert render_approval_prompt(expected_price) == expected_prompt

    def test_prompt_pattern_matches_the_spec_shape_for_an_arbitrary_price(self) -> None:
        """A tighter structural check than the previous test's exact
        string: the spec's wording pattern (fixed prefix/suffix, price in
        the middle) holds for a price that did not come from a real run
        at all, proving the function is a genuine template, not a
        one-off string that happens to work for AMS-DEL's own price.
        """
        rendered = render_approval_prompt("999.99")
        pattern = re.compile(
            r"^I found a valid itinerary for €999\.99\. "
            r"Do you approve proceeding to booking\?$"
        )
        assert pattern.match(rendered)


class TestApprovalPersistence:
    """The approval record is genuinely persisted (a fresh disk read, not
    just an in-memory response) and retrievable.
    """

    def test_approval_is_written_to_disk_and_matches_the_response(
        self, client: TestClient, settings: FlightAgentSettings
    ) -> None:
        run_id = _create_real_run(client)

        response = client.post(f"/runs/{run_id}/approve", json={"approved": True})
        assert response.status_code == 200, response.text
        body = response.json()

        record_path = approval_path(run_id, runs_dir=Path(settings.output.runs_dir))
        assert record_path.is_file()
        persisted = json.loads(record_path.read_text(encoding="utf-8"))

        assert persisted == {
            "run_id": run_id,
            "approved": True,
            "price_eur": body["price_eur"],
            "prompt": body["prompt"],
            "recorded_at": body["recorded_at"],
        }

    def test_denial_is_persisted_with_approved_false_not_coerced_to_true(
        self, client: TestClient, settings: FlightAgentSettings
    ) -> None:
        run_id = _create_real_run(client)

        response = client.post(f"/runs/{run_id}/approve", json={"approved": False})
        assert response.status_code == 200, response.text
        assert response.json()["approved"] is False

        record_path = approval_path(run_id, runs_dir=Path(settings.output.runs_dir))
        persisted = json.loads(record_path.read_text(encoding="utf-8"))
        assert persisted["approved"] is False


class TestUnknownOrEmptyRun:
    def test_approve_unknown_run_id_is_404_not_fabricated_success(
        self, client: TestClient
    ) -> None:
        response = client.post("/runs/does-not-exist/approve", json={"approved": True})
        assert response.status_code == 404
        assert response.json()["detail"]

    def test_approve_run_with_zero_itineraries_is_404_not_a_fabricated_price(
        self,
        client: TestClient,
        settings: FlightAgentSettings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "flightagent.providers.mock.provider.generate_offers", lambda request: ()
        )
        created = client.post("/search", json=_SEARCH_BODY)
        assert created.status_code == 200, created.text
        assert created.json()["status"] == "no_results"
        run_id = created.json()["run_id"]

        response = client.post(f"/runs/{run_id}/approve", json={"approved": True})
        assert response.status_code == 404
        assert response.json()["detail"]

        # And nothing was written for it either -- refusing means refusing,
        # not "404 but write the file anyway".
        record_path = approval_path(run_id, runs_dir=Path(settings.output.runs_dir))
        assert not record_path.exists()

    def test_request_body_extra_field_is_422_not_silently_ignored(
        self, client: TestClient
    ) -> None:
        """``ApprovalRequestBody`` is ``extra="forbid"`` -- there is no
        free-text field on this request for anything to flow through.
        """
        run_id = _create_real_run(client)
        response = client.post(
            f"/runs/{run_id}/approve", json={"approved": True, "reason": "trust me"}
        )
        assert response.status_code == 422


class TestStructurallyIncapableOfBooking:
    """Adversarial regression guard, per this task's own brief: prove --
    not just assert -- that nothing in this endpoint's code path can
    trigger any external action.
    """

    _BOOKING_PAYMENT_SHAPED = re.compile(
        r"book(?!ing_url)|payment|checkout|purchase|charge|invoice|stripe|paypal|"
        r"ticket(?!_)",
        re.IGNORECASE,
    )
    """Matches a module/name that LOOKS booking- or payment-shaped.
    ``book(?!ing_url)`` excludes ``booking_url`` deliberately -- that
    string names a data FIELD elsewhere in this codebase (the URL on an
    itinerary), not a booking ACTION, and this route module does not even
    reference it; the negative lookahead exists so this pattern stays
    correct if it ever did. ``ticket(?!_)`` similarly leaves room for an
    unrelated ``ticket_id``-shaped identifier without weakening the actual
    check, though no such name exists in this file today either.
    """

    def test_no_booking_or_payment_shaped_import_in_the_route_module(self) -> None:
        """Parses ``routes_approval.py``'s own AST and inspects every
        ``import`` / ``from ... import`` statement's module path and
        imported names -- not a substring grep over the whole file (which
        would trip on this module's own docstrings/comments discussing
        why no booking capability exists), specifically the import
        surface, which is what would actually wire in a real action.

        This is a permanent guard: if a future change adds
        ``from flightagent.something.booking import Client`` or
        ``import stripe``, this test fails the build.
        """
        source = inspect.getsource(routes_approval)
        tree = ast.parse(source)

        imported_identifiers: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_identifiers.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported_identifiers.append(node.module)
                imported_identifiers.extend(alias.name for alias in node.names)

        assert imported_identifiers, "sanity check: the module actually has imports to scan"
        offending = [
            name for name in imported_identifiers if self._BOOKING_PAYMENT_SHAPED.search(name)
        ]
        assert offending == [], (
            f"routes_approval.py imports {offending!r}, which looks booking/payment-shaped -- "
            "this endpoint must remain structurally incapable of triggering a real action"
        )

    _FORBIDDEN_RESPONSE_CONTENT = re.compile(
        r"confirmation|ticket[_ ]?number|\bpnr\b|booking[_ ]?reference|payment[_ ]?reference",
        re.IGNORECASE,
    )

    def test_response_never_resembles_a_booking_confirmation_or_payment_reference(
        self, client: TestClient
    ) -> None:
        run_id = _create_real_run(client)

        response = client.post(f"/runs/{run_id}/approve", json={"approved": True})
        assert response.status_code == 200, response.text
        body = response.json()

        # The schema itself is a fixed, extra="forbid" shape -- no field
        # this endpoint could ever add without a code change, let alone one
        # injected at request time.
        assert set(body.keys()) == {
            "run_id",
            "approved",
            "price_eur",
            "prompt",
            "recorded_at",
            "message",
        }

        serialized = json.dumps(body)
        assert not self._FORBIDDEN_RESPONSE_CONTENT.search(serialized), (
            f"response body unexpectedly resembles a booking confirmation/payment "
            f"reference: {serialized!r}"
        )

    def test_response_message_explicitly_denies_being_an_authorization(
        self, client: TestClient
    ) -> None:
        """Not just "no forbidden content" -- the response actively says
        what it is (an audit record) and what it is not (an authorization
        to book), so a caller cannot mistake this 200 for a green light.
        """
        run_id = _create_real_run(client)
        response = client.post(f"/runs/{run_id}/approve", json={"approved": True})
        message = response.json()["message"].lower()
        assert "no booking action" in message or "no booking capability" in message
