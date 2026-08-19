"""Tests for the API-key auth layer itself (Phase 8b, ``api.auth``).

``tests/unit/test_api.py``/``test_approval.py``/``test_safety.py`` prove
every existing route still works WITH the correct key supplied -- this
file proves the actual gate: no key, a wrong key, and no env var at all
each behave exactly as master plan section 8.7 requires (fail-closed at
app creation, 401 on every subsequent mismatched request).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from flightagent.api.app import create_app
from flightagent.api.auth import API_KEY_ENV_VAR
from flightagent.config.loader import load_config
from flightagent.config.models import FlightAgentSettings

_TEST_API_KEY = "test-api-key-not-a-real-secret"


def _isolated_settings(tmp_path: Path) -> FlightAgentSettings:
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


class TestAppCreationFailsClosed:
    def test_create_app_raises_without_the_env_var_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)
        with pytest.raises(RuntimeError, match=API_KEY_ENV_VAR):
            create_app(settings=_isolated_settings(tmp_path))

    def test_create_app_raises_when_the_env_var_is_set_but_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(API_KEY_ENV_VAR, "")
        with pytest.raises(RuntimeError, match=API_KEY_ENV_VAR):
            create_app(settings=_isolated_settings(tmp_path))


class TestRequestsAreGated:
    @pytest.fixture
    def client(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
        monkeypatch.setenv(API_KEY_ENV_VAR, _TEST_API_KEY)
        return TestClient(create_app(settings=_isolated_settings(tmp_path)))

    def test_no_header_at_all_is_401(self, client: TestClient) -> None:
        response = client.get("/healthz")
        assert response.status_code == 401

    def test_wrong_key_is_401(self, client: TestClient) -> None:
        response = client.get("/healthz", headers={"X-Api-Key": "not-the-right-key"})
        assert response.status_code == 401

    def test_correct_key_passes_through(self, client: TestClient) -> None:
        response = client.get("/healthz", headers={"X-Api-Key": _TEST_API_KEY})
        assert response.status_code == 200

    def test_gate_applies_to_healthz_too_no_carve_out(self, client: TestClient) -> None:
        """Master plan section 8.7's own wording is "every endpoint" --
        this asserts there is no unauthenticated health-check exemption,
        since that would be an easy, plausible-looking place to quietly
        add one later."""
        response = client.get("/healthz")
        assert response.status_code == 401
