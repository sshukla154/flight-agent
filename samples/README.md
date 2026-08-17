# samples/

The committed B-26 sample artifact: a single AMS→DEL, one-stop, mock-provider
search, generated once via `scripts/generate_sample.py` and checked into git
(deliberately outside `out/`, which is wholesale-gitignored).

`tests/unit/test_sample_artifact.py` reads these two files and asserts the
restated acceptance criterion B-26 (`DECISIONS.md`): the recommended
itinerary's `layover_minutes == 240`. It never regenerates them and never
constructs a provider — the test stays fast and can't flake.

The 240-minute value comes from a hand-authored fixture
(`src/flightagent/providers/mock/fixtures/ams_del_onestop.json`), not from
the mock generator's usual programmatic mode (`flightagent run --origin AMS
--dest DEL ...` on its own produces a ~270-minute layover instead — see
`scripts/generate_sample.py`'s own docstring for why).

Regenerate only when a change to scoring, validation, the layover-band
config, or report rendering would legitimately change this sample's
content:

```
uv run python scripts/generate_sample.py
```

Review the diff by hand before committing — same golden-file discipline
every other frozen artifact in this project follows (`DECISIONS.md`,
"Confirm before Phase 5").
