# DECISIONS.md — flight-agent decision register

**Date:** 2026-08-14
**Project:** autonomous flight-search agent — 10 European origins near Nieuwegein NL x 8 Indian destinations x 2 stop-modes, departure 2027-07-17, economy, EUR.
**Stack:** Python 3.12, asyncio, pydantic, httpx, SQLite.

**This file is the authority for these decisions.** Implement from this file. The master plan (`C:\Users\s.shukla\.claude\projects\flight-agent\MASTER-PLAN.md`) holds the reasoning that produced them and stays the place to go when you need to know *why*; it is not the place to go when you need to know *what to build*. If the two ever disagree, this file wins and the master plan gets corrected.

**Status vocabulary — read this before treating anything here as settled.**

| Status | Meaning |
|---|---|
| `CONFIRMED` | The project owner has explicitly signed this off. |
| `DEFAULT (unconfirmed)` | A recommended default that work proceeds on so implementation is not blocked. The owner has **not** signed it off. It can change. |

As of 2026-08-14 exactly three decisions are `CONFIRMED`: **D16, D17, D18**. Every other decision in this register, D1 through D15, is `DEFAULT (unconfirmed)`. That distinction is the whole point of the register — do not quietly promote a default to a confirmation because code now depends on it.

---

## Table of contents

- [Decisions D1–D18](#decisions-d1d18)
  - [D1 — Multi-origin request schema](#d1--multi-origin-request-schema)
  - [D2 — One-way or return](#d2--one-way-or-return)
  - [D3 — Passenger count](#d3--passenger-count)
  - [D4 — Baggage](#d4--baggage)
  - [D5 — Self-transfer / separate-ticket itineraries](#d5--self-transfer--separate-ticket-itineraries)
  - [D6 — Real credentials vs mock-only as the shipped deliverable](#d6--real-credentials-vs-mock-only-as-the-shipped-deliverable)
  - [D7 — Does ground travel affect ranking](#d7--does-ground-travel-affect-ranking)
  - [D8 — Layover inclusivity](#d8--layover-inclusivity)
  - [D9 — Penalty bands at exactly 4h / 5h](#d9--penalty-bands-at-exactly-4h--5h)
  - [D10 — Direct tier thresholds](#d10--direct-tier-thresholds)
  - [D11 — Timezone for the departure-date check](#d11--timezone-for-the-departure-date-check)
  - [D12 — Early stop: default state and measurement basis](#d12--early-stop-default-state-and-measurement-basis)
  - [D13 — Is a direct itinerary valid inside a max_stops=1 search](#d13--is-a-direct-itinerary-valid-inside-a-max_stops1-search)
  - [D14 — Non-EUR provider prices](#d14--non-eur-provider-prices)
  - [D15 — Output filenames and top-N](#d15--output-filenames-and-top-n)
  - [D16 — User-facing sort / filter](#d16--user-facing-sort--filter)
  - [D17 — Fare matrix visual: surface](#d17--fare-matrix-visual-surface)
  - [D18 — Fare matrix visual: colour scale](#d18--fare-matrix-visual-colour-scale)
- [Confirm before Phase 5](#confirm-before-phase-5)
- [Restated acceptance criteria](#restated-acceptance-criteria)
- [Open questions carried into every report](#open-questions-carried-into-every-report)
- [Spec defects this project knowingly works around](#spec-defects-this-project-knowingly-works-around)

---

## Decisions D1–D18

Blast radius entries are task IDs from the full DAG (`agent-outputs/02-execution-plan-pass.md` §2) and are reproduced from the master plan's §2 table unchanged.

---

### D1 — Multi-origin request schema

**Decision.** Keep the provider-facing `search_flights` tool at **single-origin**: `{origin, destination, date, cabin, max_stops, adults, currency}`. Add a separate run-level `MultiOriginSearchRequest` above it carrying `{origins: [{iata, ground_minutes, priority}], destinations: [iata...], departure_date, cabin, max_stops_modes: [0, 1], adults, currency, max_ground_minutes, early_stop: {enabled, threshold_eur}}`. Build the 10x8x2 fan-out in the planner (`orchestration/plan.py`). No adapter ever sees more than one origin.

**Status.** `DEFAULT (unconfirmed)`

**Rationale.** The spec's own multi-origin JSON block is truncated mid-sentence, so there is nothing to conform to. Keeping the adapter single-origin means both real providers map one-to-one onto their native search call and the fan-out stays in one testable place.

**Blast radius.** T7, T29, T37, all adapters.

**Reversal cost.** **Expensive.** A multi-origin adapter contract changes every provider signature, the planner, the cache key shape, and the schema snapshot test that pins A2-6.

---

### D2 — One-way or return

**Decision.** Search **one-way only**. Model a `trip_type` enum and hold legs as a `list[Leg]` from day one, so adding a return leg is additive rather than a redesign.

**Status.** `DEFAULT (unconfirmed)`

**Rationale.** The spec never specifies a return date, and inventing one would put a fabricated parameter into every fare shown to the user.

**Blast radius.** T7, T11, duration semantics.

**Reversal cost.** **Moderate.** The enum and list absorb the structural change, but total-duration, layover and scoring semantics all become per-leg, and every golden file regenerates.

---

### D3 — Passenger count

**Decision.** Search for **1 adult**, 0 children, 0 infants. Treat `price_eur` as the **total** price, not per-passenger — every threshold in the spec (€150, €250, 20%) is a per-total figure.

**Status.** `DEFAULT (unconfirmed)`

**Rationale.** The spec takes no passenger count as input and its worked examples are single-traveller totals.

**Blast radius.** T7, T31.

**Reversal cost.** **Moderate.** The field itself is trivial, but with N > 1 every absolute threshold changes meaning and the €150/€250 rules need explicit per-total-vs-per-pax semantics before any test can assert them.

---

### D4 — Baggage

**Decision.** Baggage is **not a filter**. Capture `cabin_bag` / `checked_bag` when the provider supplies them, display them, and log explicitly when they are unknown. Never infer an allowance.

**Status.** `DEFAULT (unconfirmed)`

**Rationale.** The spec does not model baggage, and filtering on a field that is frequently absent would silently delete valid itineraries.

**Blast radius.** T12, T15.

**Reversal cost.** **Moderate.** Making baggage a filter pulls it into the validation engine and into the dedup identity (fare brands that currently collapse would have to survive separately), which changes result counts and every golden file.

---

### D5 — Self-transfer / separate-ticket itineraries

**Decision.** **Exclude** self-transfer and separate-ticket itineraries from the valid ranked set. Surface them in a separate "Self-transfer, not protected" appendix table so they are visible but never ranked or recommended.

**Status.** `DEFAULT (unconfirmed)`

**Rationale.** A 3-hour layover on two unconnected tickets is not a protected 3-hour connection — a missed leg is the traveller's loss, so scoring it alongside a through-ticket is misleading.

**Blast radius.** T12, T18, T41.

**Reversal cost.** **Moderate.** Admitting them into the ranked set changes accepted counts, the layover rule's applicability, and the report structure.

---

### D6 — Real credentials vs mock-only as the shipped deliverable

**Decision.** **Mock-only ships.** Build the Amadeus and Duffel adapters interface-complete, with real payload-mapping unit tests against captured fixtures, and have them raise `ProviderNotConfigured` at runtime when credentials are absent. The mock adapter is a **first-class permanent code path**, not a temporary stub.

**Status.** `DEFAULT (unconfirmed)`

**Rationale.** 2027-07-17 sits at the edge of the airline schedule publication window, so live calls may legitimately return nothing; and a full 160-task run consumes a meaningful slice of an Amadeus test-tier monthly quota. The demo path has to be the mock path.

**Blast radius.** T49, Phase 8 scope, CI.

**Reversal cost.** **Moderate.** The adapters already exist; reversing adds credential management, CI secrets, quota budgeting and live-response variability to the test strategy.

---

### D7 — Does ground travel affect ranking

**Decision.** Leave the spec's `score` and `adjusted_score` **untouched**. Ground travel enters in exactly two places: (a) a **hard 150-minute filter** in the validation engine as `GROUND_TRAVEL_EXCEEDED`, evaluated per origin before any task for that origin is planned; and (b) a **parallel** `total_journey_score` shown in a second table:

```
ground_cost_component = w_ground_cost * ground_leg.cost_eur        # w default 1.0
ground_time_component = w_ground_time * ground_leg.duration_hours  # w default 8.0
total_journey_score   = adjusted_score + ground_cost_component + ground_time_component
```

Both weights are config. When the two orderings disagree, the report says so in one line.

**Status.** `DEFAULT (unconfirmed)`

**Rationale.** The spec's formula is tested against its own worked examples, so changing it breaks the acceptance criteria; but the stated motive for searching 10 airports is total travel cost, which the formula ignores entirely. A parallel metric satisfies both.

**Blast radius.** T13, T38, T41, all golden files.

**Reversal cost.** **Expensive.** Folding ground into `adjusted_score` invalidates the spec's worked examples, every ranking, and every golden file.

---

### D8 — Layover inclusivity

**Decision.** Layover validity is the **closed interval `[180, 360]` minutes**, computed on **UTC-elapsed** time. 180 valid, 360 valid, 179 and 361 rejected.

**Status.** `DEFAULT (unconfirmed)`

**Rationale.** The spec writes "3–6 hours" with no inclusivity statement; the closed reading is the natural reading of the phrase and is the only one that does not silently discard exactly-3h and exactly-6h connections.

**Blast radius.** T12, T21.

**Reversal cost.** **Moderate.** One constant plus the parameterized boundary table, but any change to the accepted set regenerates the golden files.

---

### D9 — Penalty bands at exactly 4h / 5h

**Decision.** Layover penalty bands are **lower-inclusive half-open**: `[180,240) → 0`, `[240,300) → +10`, `[300,360] → +20`. **Exactly 4h scores +10. Exactly 5h scores +20.** State this explicitly in the sample artifact, because the spec's own 4-hour sample scores +10 under this reading, not 0.

**Status.** `DEFAULT (unconfirmed)`

**Rationale.** The spec gives bands "3–4h = 0, 4–5h = +10, 5–6h = +20" with overlapping endpoints and then asks for a sample landing exactly on a boundary. Lower-inclusive half-open is the standard resolution and the only one that is total over `[180,360]`.

**Blast radius.** T13, T22, all golden files.

**Reversal cost.** **Expensive.** Every golden file changes, and the committed sample report's headline itinerary changes score.

---

### D10 — Direct tier thresholds

**Decision.** Ship **four tier states** and implement the tier ladder as a **config-driven band table in YAML**, not as inline constants — so a threshold change is a config edit plus a golden regeneration, never a code change. Default the band table to the architecture pass's ladder:

| Tier | Condition | Report column renders as |
|---|---|---|
| `RECOMMENDED` | `diff <= 100` or `rel <= 0.10` | Recommended |
| `GOOD_VALUE` | `diff <= 150` or `rel <= 0.20` | Recommended (good value) |
| `NOT_RECOMMENDED` | otherwise | Optional |
| `NOT_AVAILABLE` | no direct service found | Not available |

Record `tier_reason` carrying which threshold fired and with what numbers. The outer band (`<= 150` or `<= 0.20`) is the spec's own 150/20% rule and is **specified**; the inner band (`100` / `0.10`) is **inferred**.

**Status.** `DEFAULT (unconfirmed)` — and the least settled decision in this register. See [Confirm before Phase 5](#confirm-before-phase-5).

**Rationale.** The spec's prose defines three outcomes, its predicate is binary, and its example table adds a fourth label ("Optional") for a case that fails both rules. Some inner threshold has to be invented; making it config means inventing it costs nothing to correct.

**Blast radius.** T31, T33–T36, all golden files.

**Reversal cost.** **Moderate.** Cheap by construction — the threshold change is a YAML edit — but every golden file containing a Direct Flight Analysis row regenerates and each regeneration needs a human-reviewed diff.

---

### D11 — Timezone for the departure-date check

**Decision.** Check "departure date == 2027-07-17" against the **origin local date**, not the UTC date. A first segment departing 2027-07-18 00:30 CEST is 2027-07-17 22:30 UTC and must be **rejected**. The mirror case (23:30 local on the target day) is accepted.

**Status.** `DEFAULT (unconfirmed)`

**Rationale.** The traveller experiences the departure date in their own local time. A UTC reading would offer them a flight leaving the day after the one they asked for.

**Blast radius.** T12, T21.

**Reversal cost.** **Moderate.** One rule in the validator, but flipping it changes which itineraries exist at all, so every downstream count and golden file moves.

---

### D12 — Early stop: default state and measurement basis

**Decision.** Early stop is **off by default**, so a default run always produces the full 160-cell comparison. Default execution is **full fan-out with post-hoc deterministic replay**: run all 160 tasks, then replay the rule over the complete result set in priority order and report what it *would* have done, as a report annotation rather than a control-flow decision. Ship true sequential execution behind `--search-mode=sequential-priority` for real-quota runs; in that mode **wave 1 is always the three primary airports (AMS, EIN, RTM — 48 tasks)**, never a single airport.

Three definitions are binding in both modes:

1. The rule is evaluable only after **≥2 origins** have completed.
2. The comparison is **per-destination**: for destination D, origin O triggers when its cheapest **valid** fare to D is ≥€250 below the cheapest valid fare to D across all previously-completed origins, compared on **raw `price_eur`**, not adjusted score.
3. `EarlyStopEvaluation.compared_against` lists the exact origin set used.

**Status.** `DEFAULT (unconfirmed)`

**Rationale.** As written the rule is vacuously true on the first airport, defined by completion order (network jitter under concurrent fan-out), and compares non-substitutable trips. The three definitions above remove all three defects; the always-three-airport wave 1 kills the vacuous-truth bug structurally rather than by special-casing.

**Blast radius.** T39, T42.

**Reversal cost.** **Moderate.** Making early stop the default reintroduces sequential barriers into a concurrent design and removes rows the Direct Flight Analysis and origin-comparison tables need.

---

### D13 — Is a direct itinerary valid inside a `max_stops=1` search

**Decision.** **Yes — accept it.** The 3–6h layover rule does **not** apply to it (Addendum 1 scopes that rule to one-stop itineraries). Do not double-count it in the direct pool.

**Status.** `DEFAULT (unconfirmed)`

**Rationale.** `max_stops=1` means "at most one stop". Rejecting a direct flight because it has no layover to validate would delete the best itineraries in the set.

**Blast radius.** T12, T29.

**Reversal cost.** **Moderate.** Changes accepted counts and the composition of the direct pool, and therefore the direct-vs-stop policy output for every destination.

---

### D14 — Non-EUR provider prices

**Decision.** **Never silently convert.** Carry `price_original`, `price_eur`, `fx_rate`, `fx_source` and `fx_as_of` on every itinerary, and render the conversion visibly wherever the converted price appears. A converted price must never be presentable as a quoted one. If no sourced rate with a timestamp is available, **reject the itinerary** rather than estimate.

**Status.** `DEFAULT (unconfirmed)`

**Rationale.** Duffel returns the amount as the airline priced it and Amadeus's own conversion is approximate and says so. Ranking an EUR-quoted fare against an INR-quoted-then-converted one without disclosing the conversion is misleading in a document someone may book from.

**Blast radius.** T11, T49.

**Reversal cost.** **Cheap** mechanically — the fields are additive and reversing means dropping them. Note that dropping them is a safety regression against §8.6, not a neutral simplification.

---

### D15 — Output filenames and top-N

**Decision.** Write exactly `out/flight_report_2027-07-17.md` and `out/flight_results_2027-07-17.json`. Global **top 10** in the main ranking; **top 3 per destination** additionally in the JSON. Truncation at 10 applies to the presentation of the main ranking **only** — every other section reads the full set.

**Status.** `DEFAULT (unconfirmed)`

**Rationale.** The filenames are spec-literal. The truncation scoping exists because Addendum 1 requires a Direct Flight Analysis row per destination including "not available" rows, which a global top-10 cut would delete.

**Blast radius.** T15, T41.

**Reversal cost.** **Cheap.** Config-level, affecting the writer and the report assembly only.

---

### D16 — User-facing sort / filter

**Decision.** **Out of scope.** Ship the spec's single ranking — `adjusted_score` → price → duration → `itinerary_id` — truncated to top 10. No `--sort-by`, no `--filter`, no extra pre-sorted Markdown tables (cheapest / fastest / fewest-stops), no interactive HTML or FastAPI sort surface. Revisit only after the mock e2e run proves the pipeline.

What still ships anyway, because removing it would cost work rather than save it: `ScoredItinerary` keeps `rank_by_adjusted_score`, `rank_by_price` and `rank_by_total_journey_score`; the JSON artifact keeps `price_eur`, `total_duration`, `stop_count`, layover duration, ground minutes and the score component vector per itinerary, so anyone can sort it downstream with `jq`.

Two constraints that must hold **now**, because they are cheap today and annoying to retrofit if this is ever reversed:

1. **Every sort key ends with `itinerary_id` as its final tiebreak.** Without it, ties fall through to Python's stable sort and leak provider arrival order — one new nondeterminism source per sort option, surfacing as intermittently flaky golden tests rather than clean failures.
2. **Filters apply to the ranked table only, never to the per-destination sets.** A global price ceiling turns real direct flights into "Not available" rows in the Direct Flight Analysis and reverses the recommendation. The origin comparison table has the same exposure — it needs all 10 rows or an explicit reason for absence (see A2-5).

**Status.** `CONFIRMED` — decided by the project owner 2026-08-14.

**Rationale.** The spec asks for one ranking. Sort and filter surfaces multiply the golden-file matrix and the determinism surface without changing what the report concludes.

**Blast radius.** — (removes scope).

**Reversal cost.** **Moderate.** The data is all retained and the two-layer cache means re-running under different constraints re-validates from cached raw payloads with no provider quota spent, so the runtime cost is near zero; the work is in CLI surface, report assembly, and the golden-file matrix.

---

### D17 — Fare matrix visual: surface

**Decision.** Ship a **static SVG**. The generator writes `matrix.svg` beside the report and the Markdown references it with an image tag. No sort control, no hover states, no row/column highlight — a control that does nothing is worse than no control. This does not reopen D16. Estimated ~4–5h.

The artifact is a 10 x 8 grid, one cell per origin/destination pair, holding the cheapest **valid** one-stop fare. Reference implementation: `assets/fare-matrix.svg` in the project knowledge base.

**Empty origins stay visible, dimmed. Never drop a row** — A2-5 requires all 10 origins present with either a price or an explicit reason, and hiding them would let the report show 6 rows while claiming it searched 10.

**Status.** `CONFIRMED` — decided by the project owner 2026-08-14.

**Rationale.** A static artifact is diffable, golden-testable, and renders in every Markdown viewer. Interactivity would require a surface D16 has just ruled out.

**Blast radius.** new artifact, T41, T55.

**Reversal cost.** **Moderate.** Going interactive means a new rendering stack and a new class of test, and it reopens D16.

---

### D18 — Fare matrix visual: colour scale

**Decision.** Use a **relative colour ramp, clamped at p5/p95**. Ramp green→red across the 5th–95th percentile of **priced cells only**. **Always print the euro endpoints next to the ramp.** Clamp out-of-range cells to the endpoint colour and mark them with a corner triangle so the reader knows the colour is a floor or ceiling, not a value.

Four cell states, and only one of them sits on the ramp:

| State | Rendering | Meaning | Correct next action |
|---|---|---|---|
| priced | ramp colour + the fare | a valid itinerary exists | book it |
| `NO_OFFERS` | flat neutral, en-dash | provider returned nothing at all | nothing — the route does not exist |
| `ALL_REJECTED` | flat neutral, dashed border, "3–6h" | itineraries existed; every one failed the layover rule | loosen the layover window, re-run |
| `PROVIDER_ERROR` | hatched fill, amber border, error class (`429`, `TIMEOUT`, `5xx`) | provider never answered after 3 retries | **re-run** — the fare is unknown, not absent |

Three propagation rules that must hold in the generator:

1. **Any row or column summary computed over a set containing an error is flagged**, never printed clean — mark the affected "best per city" cell in amber and append `· unreliable`.
2. **A run-level warning states the error count and names the affected cells**, in the report body and not only under the matrix. "Best surviving fare, not necessarily the cheapest" is the honest phrasing.
3. **Error cells never enter the p5/p95 percentile base.** They have no value to contribute.

Two template constraints that look cosmetic in review and are not: **never drop the ramp endpoint labels** (colour with no anchor is meaningless), and **never remove the in-cell numbers to fit more columns** (green-to-red is the hardest ramp for red-green colour deficiency, ~8% of men, and is only safe here because colour is a redundant channel). The ramp is deliberately darkened, green-700 through red-700, so white cell text clears contrast on every band.

**Status.** `CONFIRMED` — decided by the project owner 2026-08-14.

**Rationale.** Anchoring to raw min/max lets one outlier eat the range. Absolute euro bands were rejected because an expensive travel month renders the whole grid orange-red — accurate, but it reads as alarm rather than information. Printing the euro endpoints is what makes the relative scale honest.

**Blast radius.** matrix template, golden file.

**Reversal cost.** **Cheap.** Template-local, one golden file.

---

## Confirm before Phase 5

Two decisions **invalidate every golden file** if they change. Golden files are frozen at T36 and T55; getting these signed off after that point means regenerating and human-reviewing every snapshot.

### D9 — penalty band boundaries at exactly 4h / 5h

Currently `DEFAULT (unconfirmed)`: `[180,240) → 0`, `[240,300) → +10`, `[300,360] → +20`, so exactly 4h scores +10 and exactly 5h scores +20. The spec's own requested sample lands on the 4h boundary, so this reading is directly visible in the committed sample artifact. **Needs owner sign-off before Phase 5 starts.**

### D10 — direct-tier thresholds

Currently `DEFAULT (unconfirmed)`. **The two planning passes disagreed**, and the disagreement is on record:

| Pass | Proposed `RECOMMENDED` condition |
|---|---|
| Architecture pass (`01-architecture-pass.md`) | `diff <= 100` or `rel <= 0.10` |
| Execution plan pass (`02-execution-plan-pass.md`) | `rel <= 0.15` |

**Both readings are inferred, not specified**, and both reproduce all four of the spec's own example rows — which is exactly why the source underdetermines the answer and why two independent passes landed in different places. The register defaults to the architecture pass's ladder (see D10) because it expresses both an absolute and a relative arm, matching the shape of the spec's outer rule.

**Mitigation, and it is already mandated by D10:** implement the tier ladder as a **config-driven band table** so a change is a YAML edit rather than a code change. That costs roughly 1h and removes the schedule risk entirely — Phase 5 no longer blocks on the answer, only the golden files do. Do not skip this on the grounds that the default "looks right".

---

## Restated acceptance criteria

Four of the spec's acceptance criteria cannot be asserted as written — each would leave a human judging pass/fail. Each is restated below as the exact predicate a test asserts. These restatements are binding.

### B-2 — "layovers filtered correctly"

**Original wording.** "Layovers filtered correctly."

**Why it cannot be asserted.** "Correctly" is undefined at the boundaries — the spec never states inclusivity, and the failure mode (an off-by-one that silently deletes valid itineraries) still produces a plausible-looking report.

**Restated as.** The closed interval `[180, 360]` minutes enforced on **UTC-elapsed** time; proved by the parameterized 179 / 180 / 240 / 360 / 361 table.

### B-26 — "sample report with a 4-hour layover"

**Original wording.** "Sample report with a 4-hour layover."

**Why it cannot be asserted.** It does not say what to assert about it.

**Restated as.** The committed sample's recommended AMS→DEL one-stop has `layover_minutes == 240`; the test reads the committed JSON.

### A1-8 — "explains the price-vs-travel-time trade-off"

**Original wording.** "The final recommendation explains the price-vs-travel-time trade-off."

**Why it cannot be asserted.** Prose quality is not assertable.

**Restated as.** The summary matches one of two templates and contains a EUR delta and an hours-saved integer that **both equal the values computed from the ranked data**.

### A2-5 — "minimise total travel cost while keeping airport access reasonable"

**Original wording.** "Minimise total travel cost while keeping airport access reasonable."

**Why it cannot be asserted.** It is not a testable predicate — neither "minimise" nor "reasonable" resolves to a comparison.

**Restated as.** Three checks:

- (a) the global ranking is a total order over all valid itineraries from all 10 origins with **no origin silently dropped**;
- (b) every reported origin has `ground_minutes <= 150`;
- (c) the origin comparison table lists a cheapest-valid price for all 10 origins **or an explicit reason for absence**.

### Also noted

**A2-6** (the truncated multi-origin schema) is **unspecified in the source**. It is resolved by D1 and pinned with a schema snapshot test that fails loudly if D1 changes.

---

## Open questions carried into every report

These ten entries go into `RunEnvelope.open_questions` as **machine-readable entries**, not prose, so that a spec gap survives into every generated report rather than living only here. Each has a stable snake_case `id` — **never renumber or rename an id**; downstream reports are keyed on it.

| `id` | `text` | `relates_to` |
|---|---|---|
| `multi_origin_schema_truncated` | Addendum 2's multi-origin request schema is truncated in the source; `MultiOriginSearchRequest` is a proposal, not a specification. | D1 |
| `one_way_assumed` | One-way travel assumed — no return leg is specified anywhere in the source. | D2 |
| `passenger_count_defaulted` | Passenger count defaulted to 1 adult; the source takes no passenger count as input. | D3 |
| `baggage_not_modelled_dedup_lossy` | Baggage and fare brand are not modelled, so deduplication is lossy — fare brands differing only in baggage collapse together. | D4, finding 0.2 |
| `self_transfer_policy_unspecified` | Self-transfer vs protected-connection policy is unspecified, and it matters a lot for a 3-hour layover on separate tickets. | D5 |
| `direct_tier_thresholds_inferred` | Direct-tier inner thresholds are inferred, not specified, and the two planning passes disagreed on them. | D10 |
| `ground_access_costs_are_estimates` | Ground-access costs are estimates, not measured; only the minutes are spec-sourced. | §6 |
| `parking_cost_not_modelled` | Parking cost for car mode is not modelled, and for a multi-week trip it often exceeds the fuel cost. | §6 |
| `layover_time_double_counted` | Layover time is charged twice — once through elapsed duration and once through the layover penalty. Intentional and documented. | finding 0.8 |
| `schedule_publication_horizon` | The 2027-07-17 departure date sits at the edge of the airline schedule publication window, so an empty result may be correct rather than a failure. | §8.6 |

Serialized shape:

```json
{
  "open_questions": [
    {"id": "multi_origin_schema_truncated", "text": "Addendum 2's multi-origin request schema is truncated in the source; MultiOriginSearchRequest is a proposal, not a specification.", "relates_to": ["D1"]},
    {"id": "one_way_assumed", "text": "One-way travel assumed - no return leg is specified anywhere in the source.", "relates_to": ["D2"]},
    {"id": "passenger_count_defaulted", "text": "Passenger count defaulted to 1 adult; the source takes no passenger count as input.", "relates_to": ["D3"]},
    {"id": "baggage_not_modelled_dedup_lossy", "text": "Baggage and fare brand are not modelled, so deduplication is lossy - fare brands differing only in baggage collapse together.", "relates_to": ["D4", "0.2"]},
    {"id": "self_transfer_policy_unspecified", "text": "Self-transfer vs protected-connection policy is unspecified, and it matters a lot for a 3-hour layover on separate tickets.", "relates_to": ["D5"]},
    {"id": "direct_tier_thresholds_inferred", "text": "Direct-tier inner thresholds are inferred, not specified, and the two planning passes disagreed on them.", "relates_to": ["D10"]},
    {"id": "ground_access_costs_are_estimates", "text": "Ground-access costs are estimates, not measured; only the minutes are spec-sourced.", "relates_to": ["S6"]},
    {"id": "parking_cost_not_modelled", "text": "Parking cost for car mode is not modelled, and for a multi-week trip it often exceeds the fuel cost.", "relates_to": ["S6"]},
    {"id": "layover_time_double_counted", "text": "Layover time is charged twice - once through elapsed duration and once through the layover penalty. Intentional and documented.", "relates_to": ["0.8"]},
    {"id": "schedule_publication_horizon", "text": "The 2027-07-17 departure date sits at the edge of the airline schedule publication window, so an empty result may be correct rather than a failure.", "relates_to": ["S8.6"]}
  ]
}
```

---

## Spec defects this project knowingly works around

Every row below is a real defect in the source spec, found by auditing its own arithmetic — not an ambiguity, and not an oversight in this implementation. If you hit one of these in six months and it looks wrong, it was deliberate. The reasoning behind each is in the master plan's §0.

| # | Defect | Resolution |
|---|---|---|
| 0.1 | The −120 direct bonus and the €150/20% rule imply different thresholds and diverge above a stop fare of ~€755 — the common case for July India fares, so the report would contradict itself two sections apart. | Do not unify them. `adjusted_score` governs ranked-list ordering; the 150/20% ladder governs the per-destination narrative recommendation. Compute `score_policy_divergence: bool` per destination and state it in one sentence when true. Offer `direct_bonus_mode: fixed \| proportional` in config; default `fixed` for spec compliance. |
| 0.2 | The dedup key ("same airline + same flight numbers + same times") is not computable from the specified schema — no flight number, no per-segment carrier — and flight-number keying would keep all four copies of a codeshare anyway. | Extend the segment schema with `marketing_carrier`, `operating_carrier`, `flight_number`. Dedup on the **itinerary shape key** `(tuple((seg.origin, seg.destination, seg.depart_utc, seg.arrive_utc) for seg in segments), cabin, adults)`. Keep `duplicate_count` and `also_offered_by[]` on the survivor. Record the baggage/fare-brand loss as an open question. |
| 0.3 | `score = price + 3*duration + penalty` makes the specified tiebreakers near-dead code, so genuinely distinct itineraries tie on all keys and output order becomes a function of provider response arrival order — nondeterministic under a 160-way fan-out. Separately, §2.6 says rank by price and §5.8 says rank by score. | Add `itinerary_id` (content hash) as a fourth deterministic key; pre-sort the input to `sorted()` by `itinerary_id`; use `Decimal` for scores, never `float`. Rank by `adjusted_score` and additionally publish a price-sorted table. |
| 0.4 | Layover band boundaries have no stated inclusivity, and the spec then requests a sample with a 4-hour layover — exactly on a boundary. | Validity is closed `[180, 360]` minutes (D8); penalty bands are lower-inclusive half-open (D9). The spec's own 4h sample therefore scores +10, not 0, and the sample artifact says so explicitly. |
| 0.5 | `no_results` conflates "nothing matched" with "the API was down", so a provider outage is reported as a layover-rule failure and the user loosens a preference that was never the problem. | Four run statuses: `COMPLETE`, `PARTIAL` (names the failed origin/destination pairs), `NO_RESULTS` (keeps the spec's exact string only when `dominant_rejection_code` is a layover violation), and `FAILED` (zero accepted, every task errored — the case the spec omits entirely). |
| 0.6 | "Booking links included" is an acceptance criterion, but neither Amadeus nor Duffel returns a consumer booking URL, and obtaining one means a booking action forbidden by §7. | A `BookingLinkStrategy` port with three implementations — `ProviderNative`, `DeepLinkTemplate` (config-driven search URLs), `Unavailable`. Every itinerary carries `booking_url_kind` so a search link is never presented as a locked fare. Building a URL is permitted; automating a checkout page is not. |
| 0.7 | The €250 early-stop rule is unimplementable as written: "previously searched" means completion order (network jitter), it is vacuously true on the first airport, its comparison scope is undefined, and it structurally starves the German/Belgian origins the priority list exists to compare. | Default to full fan-out with post-hoc deterministic replay of the rule as a report annotation; ship true sequential-priority mode behind a flag; wave 1 is always three airports; comparison is per-destination over completed origins with `compared_against` recorded. See D12. |
| 0.8 | Five smaller items: "top 10" conflicts with Addendum 1's per-destination rows; Addendum 2's "search if fares available" is unimplementable since availability is only knowable by searching; "direct" by segment count ignores technical stops; the score formula is dimensionally incoherent unless the `3` is €/hour, and at €3/hour it is a price ranking with a rounding error; `total_duration_hours` includes the layover, so connection time is charged twice. | Truncate at 10 for the main ranking's presentation only — every other section reads the full set. Always search MST/GRQ; empty is `NO_OFFERS`, not an error. Track `technical_stops` separately from `connections` and define direct as zero connections. Make the weight configurable (`time_value_eur_per_hour`, default 3.0) and emit the score as a named-component vector, never a scalar. Keep the double-count (waiting in HEL at 03:00 genuinely is worse than sitting on a plane) and document it, or someone will "fix" it later. |
