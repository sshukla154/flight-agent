"""Tests for the run_id/task_id contextvars."""

from flightagent.observability.context import (
    get_run_id,
    get_task_id,
    new_run_id,
    run_context,
    task_context,
)


def test_run_id_not_set_outside_any_context() -> None:
    assert get_run_id() is None


def test_run_context_injects_and_restores_run_id() -> None:
    assert get_run_id() is None
    with run_context("abc-123") as run_id:
        assert run_id == "abc-123"
        assert get_run_id() == "abc-123"
    assert get_run_id() is None


def test_run_context_generates_a_run_id_when_none_given() -> None:
    with run_context() as run_id:
        assert run_id == get_run_id()
        assert len(run_id) > 0


def test_new_run_id_generates_distinct_values() -> None:
    assert new_run_id() != new_run_id()


def test_task_id_not_set_outside_any_context() -> None:
    assert get_task_id() is None


def test_task_context_injects_and_restores_task_id() -> None:
    assert get_task_id() is None
    with task_context("AMS-DEL-s1") as task_id:
        assert task_id == "AMS-DEL-s1"
        assert get_task_id() == "AMS-DEL-s1"
    assert get_task_id() is None


def test_run_context_and_task_context_nest_independently() -> None:
    with run_context("run-1"), task_context("AMS-DEL-s1"):
        assert get_run_id() == "run-1"
        assert get_task_id() == "AMS-DEL-s1"
    assert get_run_id() is None
    assert get_task_id() is None
