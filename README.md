# flightagent

Autonomous flight-search agent: origins near Nieuwegein, NL, to India-bound
destinations, departure date 2027-07-17, economy, EUR. Mock-provider-first
(D6) — the mock adapter is a permanent, first-class code path, not a
temporary stub.

## What it does

Searches up to 10 European origins near Nieuwegein against 8 Indian
destinations, in both direct and one-stop modes. Validates layovers against
a 3–6 hour window (D8), scores and ranks itineraries by price, elapsed time,
and layover penalty (`config/defaults.toml`'s `[scoring]` table), and
renders both a Markdown report and a JSON results document. Real Amadeus
and Duffel adapters exist, interface-complete and payload-mapping tested
against captured fixtures, but ship uncredentialed (D6) — see "What's not
done yet" below.

## Quickstart

```bash
uv sync
uv run flightagent run --origin AMS --dest DEL --date 2027-07-17 --max-stops 1 --provider mock
```

This is the literal Phase 2 target invocation. It uses the mock provider's
*programmatic* generation mode, whose layover comes out around 270 minutes,
not the 240-minute example referenced elsewhere in this project's docs —
see [`samples/`](samples/) for the fixture-backed 240-minute example and
why the two differ.

## Full fan-out

```bash
uv run flightagent run --origin AMS --date 2027-07-17 --max-stops 0 --all-destinations --all-origins
```

10 origins × 8 destinations × 2 stop modes = 160 tasks. There is no `--all`
flag — this is the real flag combination that produces the full run.
Writes the same two fixed-path artifacts as the single-pair run (D15).

## Docker

```bash
docker compose run --rm agent
```

Runs the full fan-out above inside a container, writing to a named Docker
volume mounted at `/data`. CLI-only by design: the FastAPI service
(`src/flightagent/api/app.py`) ships with no authentication and hardcodes
`127.0.0.1`, explicitly documented in its own module as unsafe to expose
beyond localhost — containerizing it would mean overriding that safety
choice, which stays out of scope here (see `DECISIONS.md`'s Docker-scope
entry).

Override the command to run a single pair instead:

```bash
docker compose run --rm agent run --origin AMS --dest DEL --date 2027-07-17 --max-stops 1
```

## Where output lands

- `out/flight_report_2027-07-17.md` / `out/flight_results_2027-07-17.json`
  — fixed paths (D15), gitignored, overwritten atomically every run.
- `data/runs/<run_id>/report.md` / `results.json` — additive per-run
  copies, also gitignored.
- `samples/` — the one committed, static example (Phase 8), never touched
  by a real run.

## Configuration

Four-layer precedence, later wins: packaged `config/defaults.toml` <
`./config/config.toml` (or `$FLIGHTAGENT_CONFIG`) < `FLIGHTAGENT__SECTION__KEY`
environment variables < CLI flags. Every layer is validated with
`extra="forbid"`, so a typo'd key is a hard error at every layer, never a
silent no-op. See `config/defaults.toml` directly for the full key list
rather than a second, driftable copy of it here.

## Decision register

Every design decision — layover boundaries, scoring weights, direct-vs-stop
tiers, the output contract, and more — is recorded with rationale, blast
radius, and reversal cost in [`DECISIONS.md`](./DECISIONS.md).

## What's not done yet

- **Real credentials.** Amadeus/Duffel adapters are interface-complete but
  uncredentialed; `--provider amadeus`/`--provider duffel` raise
  `ProviderNotConfigured` at runtime (D6, by design, not a bug).
  `.env.example` documents the variable names only.
- **FastAPI authentication.** The service surface ships with no auth on
  any endpoint — acceptable only because it's bound to `127.0.0.1` by
  hardcoded default.
- **Remaining security-checklist items** (master plan §8.8, tracked as a
  follow-up "Phase 8b"): a `gitleaks`/secret-scan pre-commit hook,
  Markdown-link-escaping adversarial test fixtures, per-itinerary
  `retrieved_at`/`data_source` fields (currently run-level only),
  booking-URL validation at provider-ingestion time (currently render-time
  only), `pip-audit` wired into CI, and sensitive-query-key rejection in
  the booking-URL validator.
