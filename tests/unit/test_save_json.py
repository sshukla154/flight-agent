"""Unit tests for ``flightagent.tools.save_json`` (T45).

Master plan §1/§8.3: ``save_json`` is one of exactly three tools this
project's MCP registry will ever expose. This file proves the two things
that actually matter about it as a "persist arbitrary structured data"
primitive:

- it produces valid, re-readable JSON at the given path;
- it reuses ``reporting.writer``'s atomic (temp file + fsync + ``os.replace``)
  write rather than a raw ``json.dump`` -- proved with the SAME
  interrupted-write pattern ``test_report.py``'s
  ``TestAtomicWriter.test_interrupted_write_leaves_no_partial_or_final_file``
  already established for the D15 writer, so this is not a second,
  independently-invented atomicity test, it is the same proof applied to
  the new call site.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from flightagent.tools.save_json import save_json


class TestSaveJsonWritesValidReadableJson:
    def test_writes_a_dict_that_round_trips_exactly(self, tmp_path: Path) -> None:
        target = tmp_path / "intermediate.json"
        data = {"run_id": "abc-123", "stage": "normalize", "count": 42, "ok": True}

        returned = save_json(data, target)

        assert returned == target
        assert json.loads(target.read_text(encoding="utf-8")) == data

    def test_accepts_a_str_path_and_returns_a_path(self, tmp_path: Path) -> None:
        target_str = str(tmp_path / "final.json")
        data = {"final": True}

        returned = save_json(data, target_str)

        assert isinstance(returned, Path)
        assert returned == Path(target_str)
        assert json.loads(Path(target_str).read_text(encoding="utf-8")) == data

    def test_creates_missing_parent_directories(self, tmp_path: Path) -> None:
        nested = tmp_path / "runs" / "run-42" / "intermediate.json"

        save_json({"stage": "score"}, nested)

        assert nested.is_file()
        assert json.loads(nested.read_text(encoding="utf-8")) == {"stage": "score"}

    def test_overwrites_existing_content_wholly_not_partially(self, tmp_path: Path) -> None:
        target = tmp_path / "state.json"
        target.write_text('{"stale": true, "leftover_key": "must not survive"}', encoding="utf-8")

        save_json({"stale": False}, target)

        assert json.loads(target.read_text(encoding="utf-8")) == {"stale": False}

    def test_empty_mapping_is_valid_input(self, tmp_path: Path) -> None:
        target = tmp_path / "empty.json"

        save_json({}, target)

        assert json.loads(target.read_text(encoding="utf-8")) == {}

    def test_nested_and_non_ascii_content_is_preserved(self, tmp_path: Path) -> None:
        target = tmp_path / "nested.json"
        data = {
            "itineraries": [{"itinerary_id": "itin_1", "airline": "Lufthansa"}],
            "note": "Düsseldorf → Chennai",
        }

        save_json(data, target)

        assert json.loads(target.read_text(encoding="utf-8")) == data


class TestSaveJsonIsAtomic:
    """Proves ``save_json`` reused ``reporting.writer.atomic_write_json``
    rather than a raw ``json.dump`` -- a raw dump would leave a truncated,
    unparseable file behind on this exact failure; the atomic writer leaves
    neither a partial nor a final file.
    """

    def test_interrupted_write_leaves_no_partial_or_final_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "intermediate.json"

        def _boom(_fd: int) -> None:
            raise OSError("simulated crash mid-write")

        monkeypatch.setattr(os, "fsync", _boom)

        with pytest.raises(OSError, match="simulated crash mid-write"):
            save_json({"must": "never land"}, target)

        assert not target.exists()
        assert list(tmp_path.iterdir()) == []

    def test_failed_write_does_not_disturb_pre_existing_content(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "state.json"
        target.write_text('{"good": "data"}', encoding="utf-8")

        def _boom(_fd: int) -> None:
            raise OSError("simulated crash mid-write")

        monkeypatch.setattr(os, "fsync", _boom)

        with pytest.raises(OSError, match="simulated crash mid-write"):
            save_json({"bad": "data"}, target)

        assert json.loads(target.read_text(encoding="utf-8")) == {"good": "data"}
