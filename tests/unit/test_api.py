"""Tests for the FastAPI service surface (T46).

Uses FastAPI's own ``TestClient`` (a thin wrapper around ``httpx``) against
the ``FastAPI`` app object directly -- no real server process or socket is
ever started, matching this task's own instruction that the test suite
must not spin one up.

Every test builds its own isolated ``FlightAgentSettings`` via
``load_config(cli_overrides=...)`` pointed at a ``tmp_path``-scoped
``runs_dir``/``report_path``/``results_path`` -- the same isolation
``tests/unit/test_cli.py`` gets from its ``isolated_cwd`` fixture, but
achieved without touching the process cwd at all (the API has no cwd
dependency to isolate; only the config values matter).
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from flightagent.api import app as api_app
from flightagent.api.app import create_app
from flightagent.api.auth import API_KEY_ENV_VAR
from flightagent.config.loader import load_config
from flightagent.config.models import FlightAgentSettings

_TEST_API_KEY = "test-api-key-not-a-real-secret"


def _isolated_settings(tmp_path: Path) -> FlightAgentSettings:
    """A real, fully-loaded ``FlightAgentSettings`` whose three output
    paths are all redirected under ``tmp_path`` -- so a test run's
    artifacts never touch this repo's own ``out/``/``data/runs`` and two
    parallel test runs never collide.
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
    """A ``TestClient`` bound to an app built with pre-loaded settings --
    ``create_app(settings=...)`` skips the startup lifespan's own
    ``load_config()`` call entirely, so this never reads the real
    packaged/``./config`` layers.

    ``create_app()`` now refuses to run without ``FLIGHTAGENT_API_KEY``
    set (Phase 8b, ``api.auth``) -- set here via ``monkeypatch`` so it's
    unset again after each test, and the returned client's own default
    ``headers`` carry the matching ``X-Api-Key`` so every request every
    test in this file makes passes auth without touching the ~15
    individual test functions below.
    """
    monkeypatch.setenv(API_KEY_ENV_VAR, _TEST_API_KEY)
    return TestClient(create_app(settings=settings), headers={"X-Api-Key": _TEST_API_KEY})


_SINGLE_DEST_BODY = {
    "origin": "AMS",
    "date": "2027-07-17",
    "max_stops": 1,
    "dest": "DEL",
    "provider": "mock",
}


class TestRunIdPathTraversalIsBlocked:
    """SECURITY regression, not a data-quality nicety (``reporting.run_artifacts``,
    ``InvalidRunIdError``). An adversarial audit found a real, exploitable
    path-traversal vector: ``run_id`` reached a filesystem path join
    completely unsanitized on all three ``{run_id}``-scoped routes.
    Starlette's router blocks a multi-segment slash-encoded traversal
    (``..%2f..%2f``) but NOT a backslash-encoded one (``..%5c..%5c``),
    which stays inside a single ``{run_id}`` path segment past the router
    and is then honoured as a directory separator by Windows ``pathlib``
    during the join -- giving arbitrary-directory read AND write on any
    Windows deployment, not a one-level escape. Every case here plants a
    secret file OUTSIDE ``runs_dir`` and asserts the response is a plain
    404 that never contains that secret content -- proving the fix at the
    real HTTP layer, not just that ``InvalidRunIdError`` can be raised in
    isolation.
    """

    def _plant_secret_outside_runs_dir(self, settings: FlightAgentSettings) -> Path:
        runs_dir = Path(settings.output.runs_dir)
        secret_dir = runs_dir.parent / "secretdir"
        secret_dir.mkdir(parents=True, exist_ok=True)
        (secret_dir / "report.md").write_text("SECRET CONTENT THAT MUST NEVER BE SERVED")
        (secret_dir / "run_meta.json").write_text(
            json.dumps({"run_id": "attacker-planted", "status": "complete"})
        )
        return secret_dir

    def test_backslash_encoded_traversal_to_report_is_blocked(
        self, client: TestClient, settings: FlightAgentSettings
    ) -> None:
        self._plant_secret_outside_runs_dir(settings)
        response = client.get("/runs/..%5c..%5csecretdir/report.md")
        assert response.status_code == 404
        assert "SECRET CONTENT" not in response.text

    def test_backslash_encoded_traversal_to_run_summary_is_blocked(
        self, client: TestClient, settings: FlightAgentSettings
    ) -> None:
        self._plant_secret_outside_runs_dir(settings)
        response = client.get("/runs/..%5c..%5csecretdir")
        assert response.status_code == 404
        assert "attacker-planted" not in response.text

    def test_dot_encoded_traversal_is_blocked(
        self, client: TestClient, settings: FlightAgentSettings
    ) -> None:
        # A single "%2e%2e" segment decodes to a literal ".." that Starlette's
        # router lets through as one path segment (the multi-segment
        # "..%2f..%2f" form IS blocked at the router; this simpler form is
        # the one that reaches the route handler at all).
        response = client.get("/runs/%2e%2e/report.md")
        assert response.status_code == 404

    def test_traversal_write_via_approve_endpoint_is_blocked(
        self, client: TestClient, settings: FlightAgentSettings
    ) -> None:
        """The WRITE half of the same finding: ``POST .../approve`` must not
        be able to write ``approval.json`` outside ``runs_dir`` either."""
        secret_dir = self._plant_secret_outside_runs_dir(settings)
        response = client.post(
            "/runs/..%5c..%5csecretdir/approve", json={"approved": True}
        )
        assert response.status_code == 404
        assert not (secret_dir / "approval.json").exists()

    def test_a_real_wellformed_but_unknown_uuid4_still_404s_normally(
        self, client: TestClient
    ) -> None:
        """Sanity check: the fix must not change ordinary not-found
        behaviour for a syntactically valid run_id that simply doesn't
        exist -- only a MALFORMED run_id gets the new code path."""
        response = client.get("/runs/00000000-0000-4000-8000-000000000000/report.md")
        assert response.status_code == 404


class TestHealthz:
    def test_healthz_returns_200(self, client: TestClient) -> None:
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestSearchSingleDestination:
    def test_search_returns_run_id_and_writes_real_artifacts(
        self, client: TestClient, settings: FlightAgentSettings
    ) -> None:
        response = client.post("/search", json=_SINGLE_DEST_BODY)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "complete"
        assert body["accepted_count"] >= 1
        assert body["total_offers"] >= body["accepted_count"]
        run_id = body["run_id"]
        assert run_id

        # Not just a 200 -- the run's own artifact directory genuinely
        # exists on disk with real content, exactly like a CLI run's.
        run_dir = Path(settings.output.runs_dir) / run_id
        report_path = run_dir / "report.md"
        results_path = run_dir / "results.json"
        assert report_path.is_file()
        assert results_path.is_file()
        assert "SYNTHETIC DATA" in report_path.read_text(encoding="utf-8")
        results = json.loads(results_path.read_text(encoding="utf-8"))
        assert results["data_source"] == "mock"
        assert results["accepted_count"] == body["accepted_count"]

    def test_dest_and_all_destinations_together_is_422(self, client: TestClient) -> None:
        response = client.post(
            "/search", json={**_SINGLE_DEST_BODY, "all_destinations": True}
        )
        assert response.status_code == 422
        assert "mutually exclusive" in response.json()["detail"]

    def test_neither_dest_nor_all_destinations_is_422(self, client: TestClient) -> None:
        body = {k: v for k, v in _SINGLE_DEST_BODY.items() if k != "dest"}
        response = client.post("/search", json=body)
        assert response.status_code == 422
        assert "required" in response.json()["detail"]

    def test_unconfigured_provider_is_422_not_silent_mock_fallback(
        self, client: TestClient, settings: FlightAgentSettings
    ) -> None:
        response = client.post("/search", json={**_SINGLE_DEST_BODY, "provider": "amadeus"})
        assert response.status_code == 422
        assert "amadeus" in response.json()["detail"]
        # And it must not have run anything -- no run directory created.
        runs_dir = Path(settings.output.runs_dir)
        assert not runs_dir.exists() or not any(runs_dir.iterdir())


class TestGetRunReport:
    def test_report_md_matches_the_written_file_with_markdown_content_type(
        self, client: TestClient, settings: FlightAgentSettings
    ) -> None:
        created = client.post("/search", json=_SINGLE_DEST_BODY)
        assert created.status_code == 200, created.text
        run_id = created.json()["run_id"]

        response = client.get(f"/runs/{run_id}/report.md")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/markdown")
        run_dir = Path(settings.output.runs_dir) / run_id
        assert response.text == (run_dir / "report.md").read_text(encoding="utf-8")
        assert "SYNTHETIC DATA" in response.text

    def test_unknown_run_id_report_is_404(self, client: TestClient) -> None:
        response = client.get("/runs/does-not-exist/report.md")
        assert response.status_code == 404


class TestGetRunSummary:
    def test_known_run_id_returns_status_and_metadata(self, client: TestClient) -> None:
        created = client.post("/search", json=_SINGLE_DEST_BODY)
        assert created.status_code == 200, created.text
        run_id = created.json()["run_id"]

        response = client.get(f"/runs/{run_id}")

        assert response.status_code == 200
        summary = response.json()
        assert summary["run_id"] == run_id
        assert summary["status"] == "complete"
        assert summary["origin"] == "AMS"
        assert summary["destination"] == "DEL"
        assert summary["all_destinations"] is False
        assert summary["report_available"] is True

    def test_unknown_run_id_is_404_not_500_or_empty_200(self, client: TestClient) -> None:
        response = client.get("/runs/does-not-exist")
        assert response.status_code == 404
        # A 404 must carry a real error body, never an empty-but-successful
        # shape a caller could mistake for "run found, nothing in it".
        assert response.json()["detail"]


class TestZeroValidItinerariesStillGetsARunId:
    """Mirrors ``test_cli.py``'s own ``TestZeroValidItinerariesExitCode``:
    zero valid itineraries writes no report (D15's contract, preserved
    here), but the API must still hand back a run_id and record a
    ``no_results`` status, rather than a 500 or a silently-empty success.
    """

    def test_no_offers_returns_no_results_status_and_no_report_file(
        self,
        client: TestClient,
        settings: FlightAgentSettings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "flightagent.providers.mock.provider.generate_offers", lambda request: ()
        )

        response = client.post("/search", json=_SINGLE_DEST_BODY)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "no_results"
        assert body["accepted_count"] == 0
        run_id = body["run_id"]

        # No report was written for this run (D15's own "writes nothing"
        # contract) -- GET .../report.md must 404, not fabricate one.
        report_response = client.get(f"/runs/{run_id}/report.md")
        assert report_response.status_code == 404

        # But the run itself is still known and inspectable.
        summary_response = client.get(f"/runs/{run_id}")
        assert summary_response.status_code == 200
        assert summary_response.json()["status"] == "no_results"
        assert summary_response.json()["report_available"] is False

        run_dir = Path(settings.output.runs_dir) / run_id
        assert not (run_dir / "report.md").exists()


class TestDefaultHostBinding:
    """Master plan section 8.7 (CRITICAL): this service surface must bind
    to 127.0.0.1 by default IN CODE, not merely as documentation a
    developer has to remember to honour.
    """

    def test_default_host_constant_is_localhost(self) -> None:
        assert api_app.DEFAULT_HOST == "127.0.0.1"

    def test_serve_actually_passes_the_default_host_constant_to_uvicorn(self) -> None:
        """Not just "the constant equals 127.0.0.1 somewhere" -- this
        proves ``serve()`` itself wires that exact constant into the
        ``uvicorn.run(..., host=...)`` call, so changing ``DEFAULT_HOST``
        changes the real bind address rather than ``serve`` quietly using
        a second, independently hardcoded literal.
        """
        source = inspect.getsource(api_app.serve)
        assert "host=DEFAULT_HOST" in source
        # And serve() must never accept a caller-supplied host override --
        # that would make the safe default exactly one keyword argument
        # away from being bypassed.
        assert "host" not in inspect.signature(api_app.serve).parameters
