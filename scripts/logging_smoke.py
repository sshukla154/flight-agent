"""Phase 1 exit-criterion smoke test.

"A logging_setup smoke emits one line of valid JSON containing run_id,
event, and ts" — this script is that literal exit criterion. It calls the
logging setup, emits exactly one `run.started` log line with a real
`run_id`, and does nothing else.

Run: uv run python scripts/logging_smoke.py
"""

from flightagent.observability import EventName, log_event, new_run_id, run_context, setup_logging


def main() -> None:
    setup_logging()
    with run_context(new_run_id()):
        log_event(EventName.RUN_STARTED)


if __name__ == "__main__":
    main()
