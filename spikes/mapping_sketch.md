# Provider -> domain mapping sketch (Phase 0, T3)

Companion to `tests/fixtures/providers/`. Source payloads are the two hand-authored fixtures in that
directory — **not real captures**; see that README for the honesty statement, the doc URLs, and the
list of fields still needing verification.

Target model is MASTER-PLAN §4. `Segment` is fully specified there. `Leg`, `RawOffer` and
`NormalizedItinerary` are named but not spelled out, so where this document assigns them fields it is
**proposing** a shape, not restating one — those rows are marked.

---

## 1. Field-by-field mapping

### 1.1 `Segment`

`Segment` is the model §4 actually pins down, so this is the table that matters most.

| `Segment` field | Amadeus (`data[].itineraries[].segments[]`) | Duffel (`data[].slices[].segments[]`) | Notes |
|---|---|---|---|
| `segment_id` | derive: content hash | derive: content hash | Do **not** reuse the provider's `id`. Amadeus's is a per-response ordinal (`"1"`..`"10"`); Duffel's is a resource id tied to one offer. Neither is stable across calls, and §4 wants a content hash anyway. |
| `origin` | `departure.iataCode` | `origin.iata_code` | Duffel also gives the full airport object; we want the code only. |
| `destination` | `arrival.iataCode` | `destination.iata_code` | |
| `depart_local` | `departure.at` + zone from catalog | `departing_at` + zone from catalog | **Both offsetless.** See §4 of this doc. |
| `arrive_local` | `arrival.at` + zone from catalog | `arriving_at` + zone from catalog | |
| `depart_utc` | `depart_local.astimezone(UTC)` | same | Derived, never sent by either provider. |
| `arrive_utc` | `arrive_local.astimezone(UTC)` | same | |
| `origin_tz` | catalog lookup on `departure.iataCode` | catalog lookup; **cross-check** against `origin.time_zone` | Duffel appears to supply IANA zones inline. Use ours as authority, theirs as an assertion — provider data is untrusted input (§8.1). Mismatch -> log, don't switch. |
| `destination_tz` | catalog lookup | catalog lookup; cross-check `destination.time_zone` | |
| `marketing_carrier` | `carrierCode` | `marketing_carrier.iata_code` | |
| `operating_carrier` | `operating.carrierCode` | `operating_carrier.iata_code` | **Amadeus may omit `operating` when it equals `carrierCode`** (unverified, README item 1). Mapper must default to `carrierCode` on absence. If it defaults to `None` instead, `also_offered_by` output is wrong for every non-codeshare segment. |
| `flight_number` | `number` | `marketing_carrier_flight_number` | Strings. Strip leading zeros before using in any key. Duffel additionally gives `operating_carrier_flight_number`, which Amadeus does not — see §2.1. |
| `cabin` | `travelerPricings[0].fareDetailsBySegment[]` -> match on `segmentId` -> `cabin` | `passengers[0].cabin_class` | **Neither provider puts cabin on the segment object.** Both require a join: Amadeus by `segmentId`, Duffel by position within `segments[].passengers[]`. Amadeus is `ECONOMY` (upper), Duffel is `economy` (lower). |
| `technical_stops` | `numberOfStops`, cross-checked against `len(stops)` | `len(stops)` | Model has this as `int`. Both providers give strictly more — see §3.3. |
| `duration` | `duration` (`PT6H25M`) | `duration` (`PT6H25M`) | Parse, then **assert** it equals `arrive_utc - depart_utc`. In both fixtures it does, for all 20 segments. |

Fields both providers supply that `Segment` has nowhere to put: terminal
(`departure.terminal` / `origin_terminal`), aircraft type (`aircraft.code` /
`aircraft.iata_code`), fare basis (`fareBasis` / `fare_basis_code`), branded-fare name, baggage.
See §3.

### 1.2 `Leg` (proposed — §4 names it, does not define it)

One `Leg` = one directional journey = one Amadeus `itineraries[]` entry = one Duffel `slices[]`
entry. D2 keeps it as a list so a return trip is additive.

| `Leg` field (proposed) | Amadeus | Duffel | Notes |
|---|---|---|---|
| `segments` | `itineraries[i].segments[]` | `slices[i].segments[]` | |
| `duration` | `itineraries[i].duration` | `slices[i].duration` | Derive from endpoints per §4, then assert against this. Holds for all 9 legs across both fixtures. |
| `origin` / `destination` | first/last segment | `slices[i].origin` / `.destination` | Duffel states it explicitly; Amadeus does not. Derive from segments in both, so one code path. |
| `connections` | `len(segments) - 1` | `len(segments) - 1` | Per 0.8, "direct" is zero connections, independent of technical stops. |
| `layovers` | computed between consecutive segments | same | UTC only. |

For this one-way search, `len(legs) == 1` everywhere.

### 1.3 `RawOffer` (proposed)

| `RawOffer` field (proposed) | Amadeus (`data[]`) | Duffel (`data[]`) | Notes |
|---|---|---|---|
| `provider` | `"amadeus"` | `"duffel"` | |
| `provider_offer_id` | `id` (`"1"`) | `id` (`"off_0000..."`) | Amadeus's is a per-response ordinal and is **not** globally unique. Never use it as a cache key or a stable identifier. |
| `legs` | `itineraries[]` | `slices[]` | |
| `price_original.amount` | **`price.grandTotal`** | `total_amount` | Strings -> `Decimal`. Amadeus `total` excludes additional services; `grandTotal` is the payable figure. Always read `grandTotal`. |
| `price_original.currency` | `price.currency` | `total_currency` | Duffel may return non-EUR even when EUR was requested — fixture offer `...0003` is USD. |
| `price_base` | `price.base` | `base_amount` + `base_currency` | Duffel carries a currency on the base amount separately; Amadeus does not. |
| `price_tax` | not directly available | `tax_amount` / `tax_currency`, both nullable | Amadeus decomposes into `fees[]` (+ optionally `taxes[]`); the two are not the same decomposition. Don't try to unify. Don't assert `base + tax == total` on Amadeus. |
| `validating_carrier` | `validatingAirlineCodes[0]` | `owner.iata_code` | Different concepts (ticketing carrier vs offer owner) that usually coincide. |
| `seats_remaining` | `numberOfBookableSeats` | **no equivalent** | Must be `int \| None`. See §3.5. |
| `offer_expires_at` | **no equivalent** | `expires_at` | See §3.7 — `lastTicketingDate` is *not* this. |
| `refundable` | `pricingOptions.refundableFare` (bool) | `conditions.refund_before_departure.allowed` (tri-state) | See §3.6. |
| `raw_payload_ref` | pointer to the whole response body | pointer to the whole response body | Amadeus forces whole-body caching because of `dictionaries` — see §2.6. |
| `retrieved_at` | client clock | client clock | §8.6 requires this on every rendered price. |

### 1.4 `NormalizedItinerary` (proposed)

Post-normalization, provider-agnostic. Everything from `RawOffer` plus `price_eur` / `fx_rate` /
`fx_source` / `fx_as_of` (D14), `itinerary_id` (content hash, 0.3), `duplicate_count` and
`also_offered_by[]` (0.2), `booking_url_kind` (0.6), and the validation outcome.

The dedup shape key from finding 0.2 is computable from both providers' payloads:
`(tuple((seg.origin, seg.destination, seg.depart_utc, seg.arrive_utc) for seg in segments), cabin, adults)`.
Verified against the fixtures — the codeshare pair collapses correctly in both.

---

## 2. Where the providers disagree

Each of these has to be absorbed by the adapter, not leaked upward.

### 2.1 Codeshare is expressible on Duffel and only partly on Amadeus

Duffel gives **four** fields: `marketing_carrier`, `marketing_carrier_flight_number`,
`operating_carrier`, `operating_carrier_flight_number`. Amadeus gives three: `carrierCode`,
`number`, `operating.carrierCode` — there is **no operating flight number**. So on Amadeus you can
know that QF 8148 is operated by EK, but not that it is EK **148**.

This is exactly why finding 0.2's resolution is right to key on itinerary shape rather than flight
numbers: the shape key is computable identically from both providers, and the operating flight
number is not. It also means `also_offered_by[]` display text will be richer on Duffel than on
Amadeus, and the report template must tolerate that.

Compounding it: Amadeus may omit `operating` entirely when it matches the marketing carrier
(unverified — README item 1). Absence therefore means "same carrier", not "unknown".

### 2.2 Cabin lives in a different place, in a different case

Amadeus: offer-level, under `travelerPricings[].fareDetailsBySegment[]`, joined to the segment by
`segmentId`, uppercase (`ECONOMY`). Duffel: segment-level, under `segments[].passengers[]`, joined
by passenger, lowercase (`economy`). Both need a join; neither puts it where `Segment` wants it.
Normalize to the `CabinClass` enum at the adapter boundary and never propagate the provider's casing.

Both models also permit a **mixed-cabin itinerary** (cabin is per-segment in both). The request
carries a single cabin, so the mapper must decide: reject mixed-cabin offers, or record the set. I
would reject with a distinct rejection code — a "premium economy on the long leg" offer priced in an
economy search is a real thing and silently labelling it economy is misleading.

### 2.3 Pagination is structurally different

Amadeus: offset-based, `page[offset]` / `page[limit]`, with `meta.count` and `meta.links`
(`self`, possibly `next`/`last` — README item 4). You can compute how many pages remain.
Duffel: opaque cursor, `meta.after` / `meta.before` / `meta.limit` (1-200, default 50), terminating
when `after` is `null`. You **cannot** know the total. `meta.after` must be treated as opaque and
never parsed.

The `truncated` flag in the plan's §5 protocol is what absorbs this: with a page cap of 3, "we
stopped early" is the only fact both providers can report identically.

### 2.4 Currency control

Amadeus accepts `currencyCode=EUR` and converts, approximately, and says so. Duffel returns the
currency the airline priced in. Fixture offer `...0003` is USD in an otherwise-EUR result set — a
single result set can be **mixed-currency**. D14's `price_original` + `fx_rate` + `fx_source` +
`fx_as_of` is therefore not optional polish; without it the ranking silently compares a quoted EUR
fare against a converted USD one.

Worth noting the asymmetry is doubled: an Amadeus EUR figure may *itself* already be a conversion
performed by Amadeus, with no rate disclosed. That is arguably worse than Duffel's honest USD,
because it looks native. Record `fx_source: "amadeus_internal"` for Amadeus EUR quotes rather than
treating them as natively priced.

### 2.5 Timestamp shapes are inconsistent *within* each provider

Duffel: `departing_at` / `arriving_at` are offsetless local; `created_at` / `expires_at` /
`payment_required_by` are UTC with `Z`. Amadeus: `departure.at` / `arrival.at` are offsetless local;
`lastTicketingDate` is a bare date. A mapper with one datetime-parsing helper will get one of these
categories wrong. Use two distinct parsers with different return types — a naive-local parser that
*requires* a zone argument, and a UTC parser that *requires* a `Z`.

### 2.6 Amadeus has response-scoped dictionaries; Duffel inlines everything

Carrier names, aircraft names and city/country codes live in a top-level `dictionaries` block that is
scoped to the whole response. Duffel repeats the full airline and airport objects inside every
segment.

Two consequences. First, `dictionaries` is the **only** source of the carrier name — if you cache
per-offer JSON slices you lose it and every Amadeus offer becomes an unnamed carrier code. The raw
cache layer in §5 must store whole response bodies. Second, Duffel's inline repetition means the raw
payload is much larger for the same information, which affects the `response_bytes` metric and the
cache row size but nothing else.

### 2.7 Stop filters, already covered by the plan

Amadeus `nonStop=true` plus a connection restriction in the POST body; Duffel `max_connections` on
the offer request. §5 already resolves this and already mandates a client-side re-check. The fixtures
support that re-check: every offer is genuinely one-stop, so a test asserting the validator passes
them all is meaningful.

---

## 3. Model gaps — things in the payloads §4 cannot represent

**This is the section T3 exists for.** Each item is a real field in a documented payload with no home
in the current model.

### 3.1 Fare brands / branded fares — GAP, and it interacts badly with dedup

Amadeus: `fareDetailsBySegment[].brandedFare` (`"ECOSAVER"`, `"ECOFLEX"`) and `brandedFareLabel`
(`"ECONOMY SAVER"`, `"ECONOMY FLEX"`), plus `isUpsellOffer`. Duffel:
`slices[].fare_brand_name` (`"Economy Saver"`, `"Economy Flex"`), plus per-segment
`cabin_class_marketing_name`, plus `ngs_shelf` (an integer fare-quality tier).

`Segment` has no field for any of it, and neither does anything else in §4.

The consequence is concrete and visible in the fixtures. Duffel offers `...0001` (€689, Economy
Saver, 1 bag, non-refundable) and `...0004` (€1042, Economy Flex, 2 bags, refundable) are the **same
metal at the same times**. The shape key collapses them, the cheapest survives, and the report shows
"€689" with a `duplicate_count` of 3 — with no way to say the other two were a codeshare and a
€353-more-expensive flexible product. Amadeus offers `1` and `5` are the identical situation.

Finding 0.2 anticipated this ("fare brands differing only in baggage collapse together — record it as
an open question"). Having now looked at the payloads, I would go further than recording it: the
survivor should carry a `fare_options: tuple[FareOption, ...]` where `FareOption` holds
`(brand_name, price_original, price_eur, refundable, checked_bags, provider_offer_id)`. It costs one
model and one dedup change now; retrofitting it after the report templates exist is much worse.
Without it, the "cheapest" number in a financial report is systematically the most restrictive
product available, which is a real, defensible bias — but only if the report *says* so, and it
currently cannot.

`ngs_shelf` is worth capturing too if it is real (README item 20): it is the only cross-airline
comparable fare-quality signal either provider offers.

### 3.2 Baggage allowance — GAP, with two incompatible shapes on one provider

D4 says capture when supplied, display only. There is nowhere to put it.

Amadeus gives it per segment per traveler, in **two mutually exclusive shapes**:
`includedCheckedBags: {"quantity": 1}` (offers 1, 2, 4, 5) or
`includedCheckedBags: {"weight": 30, "weightUnit": "KG"}` (offer 3, TK). Plus `includedCabinBags`.
Duffel gives `baggages: [{"type": "carry_on", "quantity": 1}, {"type": "checked", "quantity": 0}]`.

Three distinct states must be representable and are currently conflated: piece-based, weight-based,
and **unknown**. Note Duffel offer `...0003` has `checked` quantity `0` — a hand-baggage-only fare
that, under the current model, renders identically to a fare including a 23kg bag. For a 9,000km
trip to India that is not a cosmetic difference; a checked bag is €68 on that very offer's
`available_services`. A `BaggageAllowance` type with an explicit `UNKNOWN` variant is the minimum,
and D4's "log when unknown" only works if unknown is a value the model can hold.

### 3.3 Technical stops — PARTIAL GAP (`int` is too weak)

`Segment.technical_stops: int = 0` exists, so 0.8 was already caught. But both providers give more
than a count:

- Amadeus offer 4: `numberOfStops: 1` **and** `stops: [{iataCode: "BUD", duration: "PT1H15M", arrivalAt, departureAt}]`.
- Duffel offer `...0003`: `stops: [{id, airport: {...}, arriving_at, departing_at, duration: "PT1H0M"}]`.

Three things the `int` cannot express, in descending order of how much they matter:

1. **The segment `duration` is not flight time.** Amadeus offer 4's AMS-DOH segment is `PT9H35M`
   spanning a 1h15 ground stop. Any code that treats segment duration as air time — a future
   emissions estimate, a comfort model, anything — is wrong on stopping segments. (Whether Amadeus
   really includes the stop is README item 3; my fixture assumes it does.)
2. **The technical stop is invisible to the layover validator**, and correctly so under 0.8's
   definition, but that means an itinerary with a 3h35 layover plus a 1h15 technical stop passes the
   3-6h rule while actually involving two ground stops. The report should be able to say so.
3. **The stop airport is lost.** A fuel stop where nobody deplanes and a stop where everyone clears
   security are different products, and the airport code is the only hint available.

Recommend `technical_stops: tuple[TechnicalStop, ...]` with `(iata, arrive_utc, depart_utc,
duration)`, and let `len()` serve the count. Cheap now; both fixtures already exercise it.

### 3.4 Multiple fare options on one itinerary — GAP

Covered by 3.1 for the branded case. The general statement: **both providers model "the same journey
at different prices" as separate top-level offers**, and the domain model treats each top-level offer
as an independent itinerary. Dedup then discards the difference. Amadeus signals it with
`isUpsellOffer: true` (offer 5); Duffel signals it only by identical segments (and possibly a shared
`comparison_key`).

### 3.5 Seat availability — GAP, and asymmetric

Amadeus: `numberOfBookableSeats`, integer 1-9, where 9 conventionally means "9 or more"
(README item 14). Duffel: **no equivalent field at all.**

So it must be `int | None`, and the report must distinguish "2 seats left" from "we don't know" —
never render the absence as an implied plenty. Worth capturing despite being one-adult-only (D3),
because `numberOfBookableSeats: 2` on a fare is a genuine signal about how long that price survives,
and the 24-hour cache TTL for a 349-day-out departure (§5) makes fare staleness a live concern.

### 3.6 Refundability and change conditions — GAP, and the shapes are wildly asymmetric

Amadeus: `pricingOptions.refundableFare` — one offer-level boolean, plus `noPenaltyFare` and
`noRestrictionFare`.

Duffel: structured objects at **three levels** (offer, slice, and possibly segment), each
`{allowed, penalty_amount, penalty_currency}`, for both `refund_before_departure` and
`change_before_departure` — plus `priority_boarding`, `priority_check_in`,
`advance_seat_selection`.

Two traps. First, Duffel's `allowed` is genuinely **tri-state**: `true`, `false`, and `null` meaning
"the airline didn't tell us". Mapping `null` to `false` invents a restriction that may not exist.
Second, the penalty amount carries its own currency, so a refundability display can need FX
conversion independently of the fare (fixture offer `...0004`: refundable with a `75.00 EUR`
penalty). Any normalized `refundable` field should be a tri-state enum, not `bool`, and the penalty
should not be silently dropped.

### 3.7 Offer expiry — GAP on Amadeus, and the near-miss field is a trap

§5 specifies `offer_expires_at` hard-invalidating the cache. Duffel supplies `expires_at` (fixture:
30 minutes, and 15 for the TK offer). **Amadeus supplies nothing equivalent.**

The trap: Amadeus's `lastTicketingDate` / `lastTicketingDateTime` looks like the right field and is
not. It is the airline's ticketing deadline — a date, days or weeks out (fixture: `2027-07-10`) — not
a "this quote is stale after" instant. Mapping it to `offer_expires_at` would mark Amadeus fares as
valid for eleven months. `offer_expires_at` must be `datetime | None`, and `None` must fall back to
the TTL ladder, not to "never expires".

Also note Duffel's expiry is *shorter than the >180-day cache TTL of 24 hours*, so for Duffel the
TTL is effectively dead — `expires_at` always wins. Worth asserting in a test, since a cache that
serves a 12-hour-old Duffel offer is serving a fare the provider has already withdrawn.

### 3.8 Booking URL — finding 0.6 CONFIRMED, with one caveat

I checked every URL-shaped field in both documented schemas.

Amadeus Flight Offers Search: the only URLs anywhere in the response are `meta.links.self` /
`next` / `last`, which are API endpoints carrying the search query, not consumer booking pages.
There is no per-offer URL field. Confirmed: **no consumer booking URL**.

Duffel: three URL fields, all on the airline object — `logo_symbol_url`, `logo_lockup_url`,
`conditions_of_carriage_url`. A logo image and a terms-and-conditions page. Booking a Duffel offer
means `POST /air/orders` through the API, which is a booking action forbidden by §7. Confirmed:
**no consumer booking URL**.

So 0.6 stands and `BookingLinkStrategy` with `ProviderNative` / `DeepLinkTemplate` / `Unavailable`
is the right design. In practice `ProviderNative` has **no implementation on either provider** and
should probably not exist yet — an empty strategy that can never be selected is a strictly worse
form of documentation than a comment saying why.

The caveat, and it matters under §8.2: `conditions_of_carriage_url` is a **provider-controlled,
link-shaped string**. It is the one field in either payload that could plausibly get rendered as a
clickable link ("see the airline's conditions"), and it would arrive from the provider without ever
passing through the booking-URL allowlist, because it is not a booking URL. If it is ever rendered,
it must go through the same `validate_booking_url()`. Safest default: drop all three URL fields at
the adapter boundary and never carry them into the domain.

### 3.9 Passenger identity fields — a §8.3 CI collision, not a model gap

Duffel offers carry `supported_passenger_identity_document_types: ["passport"]` and
`passenger_identity_documents_required: bool`. §8.3 mandates a CI grep denylist over schema/model
files for the literal string `passport`.

That denylist will fire on a faithful Duffel DTO. The right resolution is **not** to loosen the
denylist: it is to **drop both fields at the adapter boundary** and never define them in any model.
They are irrelevant to search — they describe booking requirements — so dropping them costs nothing
and keeps the §8.3 control sharp. Worth deciding now, because the alternative (an exclusion path in
the denylist) is exactly the kind of small concession that makes the control decay.

Same reasoning for `passengers[].given_name` / `family_name` / `age`: `null` on every search offer,
never needed, and §8.3 explicitly flags a passenger-name field as a scope-creep signal. Drop at the
boundary.

### 3.10 Smaller gaps, listed for completeness

- **Terminal** (`departure.terminal`, `origin_terminal`). Not in the model. Affects whether a
  250-minute layover is comfortable or tight, and both providers supply it. Also nullable on both
  (Duffel offer `...0003` has `destination_terminal: null` on the AMS-IST segment).
- **Aircraft type** (`aircraft.code` / `aircraft.iata_code`). Both supply it; the model has no field.
  Relevant to comfort and to the A380-vs-A321 distinction a reader will notice.
- **CO2**. Duffel: `total_emissions_kg`, offer-level, a string. Amadeus: `segments[].co2Emissions[]`,
  per segment per cabin with weight + unit. Different granularity, different units, both absent from
  the model. Not required by the spec — noting it so nobody assumes it is free later.
- **Distance**. Duffel `segment.distance` (string). Amadeus: nothing.
- **`Duffel partial: true`**. Multi-step-search offers with incomplete data that are not directly
  bookable. Must be filtered at the adapter; the model has no concept of an incomplete offer.
- **Duffel city-level origins.** `origin_type` / `destination_type` can be `"city"`, in which case
  the code is a city code (`LON`) not an airport code. The IATA->IANA catalog is keyed on airports
  and would fail — correctly, per §4's "fail the task on unknown IATA code", but with a confusing
  message. Reject explicitly on `origin_type != "airport"`.
- **`Amadeus oneWay: false` on a one-way search.** This field does not mean what its name suggests
  (it relates to fare construction, not trip type). Do not map it to `trip_type`.

---

## 4. Parsing hazards

### 4.1 ISO-8601 durations need a real parser

`PT6H25M`, `PT3H20M`, `PT13H55M`, `PT1H0M`. Both providers, on segments and on itineraries/slices.
The stdlib has no ISO-8601 duration parser — `timedelta` does not parse strings and
`datetime.fromisoformat` does not handle `P`-prefixed durations. Pydantic v2 will coerce these into
`timedelta` for a `timedelta`-annotated field, which is the cheapest correct route; otherwise
`isodate`.

Specific traps:

- **Never string-compare durations.** `PT17H0M` and `PT17H` are the same duration. My Amadeus offer 4
  emits `PT17H0M`; a real response might emit `PT17H`. Parse, then compare `timedelta`s.
- **Day components.** `P1DT2H` is legal and a >24h itinerary could produce it. A regex of
  `PT(\d+H)?(\d+M)?` — which is what I used to verify these fixtures — would silently fail on it.
  Verify what Amadeus emits for a long multi-stop itinerary before shipping a hand-rolled regex.
  Ideally: do not hand-roll one.
- **Always assert against the endpoints.** §4 already mandates deriving totals from endpoints and
  asserting the sum matches. Extend that to segments: `duration == arrive_utc - depart_utc`. Both
  fixtures satisfy this for all 20 segments and all 9 legs, so the assertion is testable today, and
  a violation on real data means either a mapper bug or a technical stop being handled differently
  than assumed (§3.3, README item 3).

### 4.2 Prices are strings and must become `Decimal`, never `float`

`"684.31"`, `"1038.60"`, `"664.20"`. Both providers, every monetary field, including penalty amounts
and service prices.

- `Decimal("684.31")`, constructed **from the string directly**. `Decimal(float("684.31"))` is
  already wrong before you start.
- 0.3 requires `Decimal` for scores too, because float addition is not associative and different
  accumulation orders produce different last bits, which reorders ties and makes the ranking
  nondeterministic under concurrent fan-out.
- Quantize to `0.01` with `ROUND_HALF_UP` per §4 — but quantize at the *presentation* boundary, not
  on ingestion. Quantizing an FX-converted intermediate loses precision that the next multiplication
  needs.
- Read `price.grandTotal`, not `price.total`. They are equal in this fixture only because I set them
  equal (README item 5).
- Nullable monetary fields exist: Duffel `tax_amount` / `tax_currency` are nullable, and
  `penalty_amount` is `null` whenever `allowed` is `false` or `null`. `Decimal(None)` raises.
- Do not assert `base + tax == total` on Amadeus. It holds on Duffel (offer `...0003`:
  381.00 + 283.20 = 664.20) but Amadeus's `base` + `fees[]` is a different decomposition that does
  not have to reconcile to `grandTotal`.

### 4.3 Offsetless local times need the IATA->IANA catalog before anything else

Every flight time in both payloads is a naive local wall-clock string:
`"2027-07-17T14:55:00"`. `datetime.fromisoformat` returns a **naive** datetime that silently
compares, subtracts and sorts as if it were UTC. Nothing downstream will raise; the arithmetic will
just be wrong by hours.

This makes `config/airports.yaml` a hard dependency of normalization, exactly as §4 says. Two
concrete demonstrations from these fixtures:

**The layover.** EK 148 arrives DXB at local `23:20`, EK 512 departs DXB at local `03:30` next day.
Naive subtraction gives 4h10 — which is right, by luck, because both times are in the same zone.
Now the TK offer: arrive IST local `15:15`, depart IST local `19:55`, naive gives 4h40, also right.
Layovers are the *safe* case, since both sides share a zone. **Total duration is the unsafe case.**

**The total.** Amadeus offer 1: depart AMS `14:55` local, arrive DEL `08:20` local next day. Naive
subtraction gives 17h25. The true elapsed time is **13h55** — 3h30 of error, from the +02:00 to
+05:30 offset change. That error feeds the `3 * duration_hours` score term and the direct-vs-stop
comparison. It is exactly the class of bug 0.4 warns about: the output still looks plausible.

**The local-date rollover.** Amadeus offer 3 (TK) is the §4 breaking case, present on purpose:
departs AMS 2027-07-17 local, arrives DEL 2027-07-18 local, but in UTC both instants fall on
2027-07-17. A "+1 day" arrival marker computed from UTC dates shows nothing; computed from local
dates it correctly shows +1. §4 already mandates
`arrive_local.date() - depart_local.date()` — this fixture makes it testable.

**`tzdata` is not optional, and this machine proves it.** Running
`ZoneInfo("Europe/Amsterdam")` under the local Python 3.14.3 (`C:\Python314\python.exe`) raises
`ZoneInfoNotFoundError: 'No time zone found with key Europe/Amsterdam'` — Windows ships no system
IANA database, and the `tzdata` PyPI package was not installed. §4 already says to pin it and record
`tzdata_version` in the run envelope. Add one more thing: a startup check that resolves one known
zone and fails loudly, because the failure mode without it is an exception thrown 40 seconds into a
160-task fan-out, inside a `TaskGroup`, per task.

**Do not trust Duffel's inline `time_zone`.** It appears to be there (README item 16) and it is
tempting to use it and skip the catalog for Duffel. Don't: it makes normalization depend on
untrusted provider input (§8.1), and it makes the two adapters behave differently on a bad zone.
Use the catalog as the authority and Duffel's value as a free consistency check — a mismatch is a
signal worth logging, in either direction.

### 4.4 Smaller parsing notes

- **Two datetime shapes per provider.** Flight times are offsetless local; metadata timestamps
  (`created_at`, `expires_at`, `payment_required_by`) are UTC with `Z`. Use two separate parsers with
  different signatures so the type system stops you mixing them (§2.5).
- **Flight numbers are strings.** `"148"`, `"8148"`. Do not `int()` them — leading zeros are possible
  and the value is an identifier, not a quantity. Strip leading zeros for key purposes only.
- **Amadeus segment `id` is a per-response ordinal**, reused across responses. It is a join key
  within one payload (to `fareDetailsBySegment.segmentId`) and nothing more.
- **The `_fixture_note` key** exists only in the fixtures. Pop it in the test loader rather than
  loosening a strict model to accept it.
- **`total_emissions_kg` and `distance` are strings too** on Duffel, despite being numeric.

---

## 5. What I would change in the domain model before writing it

Ordered by how expensive they are to retrofit.

1. `Segment.technical_stops: int` -> `tuple[TechnicalStop, ...]`. Both fixtures already exercise it.
2. Add `fare_options: tuple[FareOption, ...]` to the dedup survivor, holding brand, price,
   refundability and baggage per collapsed offer. Otherwise finding 0.2's "known loss" is a
   permanent, invisible bias toward the most restrictive product.
3. Add `BaggageAllowance` with piece / weight / **unknown** variants. D4 requires "log when unknown",
   which needs unknown to be representable.
4. `refundable` as a tri-state enum, not `bool` — Duffel's `null` means "not told", not "no".
5. `seats_remaining: int | None`, with the report distinguishing unknown from plentiful.
6. `offer_expires_at: datetime | None`, explicitly **not** fed from Amadeus `lastTicketingDate`.
7. Drop `supported_passenger_identity_document_types`, `passenger_identity_documents_required`, and
   all passenger name/age fields at the adapter boundary — never define them in a model, so the §8.3
   CI denylist stays a hard control.
8. Drop `logo_symbol_url`, `logo_lockup_url`, `conditions_of_carriage_url` at the boundary, or route
   them through `validate_booking_url()` if they are ever rendered.
9. Consider `terminal` and `aircraft_type` on `Segment` — cheap, both providers supply them, and a
   reader will ask.
