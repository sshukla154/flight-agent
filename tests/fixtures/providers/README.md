# Provider response fixtures

## These are hand-authored. They are not real API captures.

Both JSON files in this directory were **written by hand from the providers' published schema
documentation**. No Amadeus or Duffel credentials existed in the environment where they were
produced, no HTTP request was ever sent to either provider, and nothing here was recorded from a
live response.

Concretely, that means:

- The **structure** (key names, nesting, types, which fields are strings vs numbers vs null) is
  taken from the vendor docs listed below and should be close to correct.
- The **values** — prices, flight numbers, times, seat counts, fare basis codes, resource ids,
  pagination cursors, distances, emissions — are plausible inventions. **No price in these files is
  a real fare.** Do not quote them, do not compare them to real market fares, do not use them to
  sanity-check a live adapter's output.
- Anything the docs did not pin down exactly is listed under
  [Needs verification against a real response](#needs-verification-against-a-real-response). That
  list is the point of this whole exercise: it is the checklist to run through the first time a real
  API call succeeds.

Each file also carries a `_fixture_note` key at the top level saying the same thing, so the warning
survives being copied out of this directory.

Both files parse under Python 3.14.3 (`json.load`), and every duration, layover and technical-stop
interval in them has been checked for internal arithmetic consistency (see
[Internal consistency](#internal-consistency)).

## Scenario

| | |
|---|---|
| Route | AMS -> DEL (Amsterdam Schiphol to Delhi Indira Gandhi) |
| Departure date | 2027-07-17 |
| Cabin | economy |
| Passengers | 1 adult |
| Stops | one stop (Amadeus `nonStop=false` + max 1 connection; Duffel `max_connections: 1`) |
| Currency requested | EUR |

Northern-hemisphere summer, so on 2027-07-17 the relevant UTC offsets are AMS +02:00 (CEST),
DXB +04:00, DEL +05:30, IST +03:00, DOH +03:00, BUD +02:00 (CEST), TBS +04:00.

## Doc URLs consulted

- Amadeus Flight Offers Search v2 OpenAPI specification (the authoritative field list, since the
  rendered API-reference page is client-side rendered and returns no content to a fetcher):
  <https://raw.githubusercontent.com/amadeus4dev/amadeus-open-api-specification/main/spec/json/FlightOffersSearch_v2_swagger_specification.json>
- Amadeus API reference page for the same endpoint (attempted; returned an empty body):
  <https://developers.amadeus.com/self-service/category/flights/api-doc/flight-offers-search/api-reference>
- Amadeus pagination guide, for the `meta.count` / `meta.links` shape and the
  `page[offset]` parameter encoding: <https://amadeus4dev.github.io/developer-guides/pagination/>
- Duffel v2 Get Offer by ID, for the full offer object:
  <https://duffel.com/docs/api/v2/offers/get-offer-by-id>
- Duffel v2 Offer schema, for the complete top-level field list including `partial`,
  `ngs_shelf`, `comparison_key`, `private_fares` and the identity-document fields:
  <https://duffel.com/docs/api/v2/offers/schema>
- Duffel v2 Create Offer Request, for the offer-request body and the `return_offers` /
  `supplier_timeout` query parameters:
  <https://duffel.com/docs/api/v2/offer-requests/create-offer-request>
- Duffel pagination overview, for the cursor `meta` envelope:
  <https://duffel.com/docs/api/overview/pagination>

## What each file contains

### `amadeus_offers_sample.json`

One `GET /v2/shopping/flight-offers` response body: `meta`, `data[]` (5 offers), `dictionaries`.
Plus the extra `_fixture_note` key described above.

| Offer id | Routing | Carrier | Price | Why it is in the fixture |
|---|---|---|---|---|
| `1` | AMS-DXB-DEL, EK 148 / EK 512 | EK marketing, EK operating | `684.31` EUR | Baseline. 250-minute layover, so penalty band `[240,300)` -> +10 under D9. |
| `2` | identical times to offer 1 | **QF marketing, EK operating** (QF 8148 / QF 8512) | `702.87` EUR | **The codeshare case finding 0.2 depends on.** Same metal, different marketing carrier and flight numbers, byte-identical times. Flight-number keying keeps both; the itinerary shape key collapses them. |
| `3` | AMS-IST-DEL, TK 1954 / TK 716 | TK | `612.44` EUR | Weight-based baggage (`weight`/`weightUnit`) instead of `quantity`. Also the **local-date rollover trap**: arrival local date is 2027-07-18 but arrival UTC date is still 2027-07-17, so a "+1 day" marker computed from UTC dates is wrong. 280-minute layover. |
| `4` | AMS-DOH-DEL, QR 274 / QR 578 | QR | `731.90` EUR | Carries a **technical stop inside a single segment** (`numberOfStops: 1`, `stops[]` at BUD, 1h15). 215-minute layover, so penalty band `[180,240)` -> 0. |
| `5` | identical routing and times to offer 1 | EK | `1038.60` EUR | **Same journey, different fare brand** (`ECOFLEX` vs `ECOSAVER`, `refundableFare: true`, 2 checked bags, `isUpsellOffer: true`). Collapses into offer 1 under the shape key. |

Segment `id` values run `1`-`10`, unique across the response and referenced by
`travelerPricings[].fareDetailsBySegment[].segmentId`.

### `duffel_offers_sample.json`

Duffel's search is two-step, so this file wraps **two** response bodies under one root:

- `offer_request_response` — the body of `POST /air/offer_requests?return_offers=false`.
- `offers_list_response` — the body of `GET /air/offers?offer_request_id=...&limit=50`, i.e.
  `data[]` (4 offers) plus the cursor `meta` (`limit` / `before` / `after`).

**A real capture of either endpoint is only the inner object.** The wrapper exists so one file can
carry both halves of the two-step flow; a mapper test should read
`fixture["offers_list_response"]`, not the root.

| Offer id suffix | Routing | Carrier | Price | Why it is in the fixture |
|---|---|---|---|---|
| `...0001` | AMS-DXB-DEL, EK 148 / EK 512 | EK / EK | `689.00` **EUR** | Baseline, `fare_brand_name: "Economy Saver"`, 1 checked bag. |
| `...0002` | identical times to `0001` | **QF marketing / EK operating** | `707.55` **EUR** | The codeshare case. Note both `marketing_carrier_flight_number` (`8148`) and `operating_carrier_flight_number` (`148`) are present, which Amadeus does not give you. |
| `...0003` | AMS-IST-DEL, TK 1954 / TK 716 | TK / TK | `664.20` **USD** | Three things at once: a **non-EUR quote** (D14 / FX disclosure), a **hand-baggage-only fare** (`checked` quantity `0`), and a **technical stop** at TBS inside the IST-DEL segment. Also the only offer with a populated `available_services[]`. |
| `...0004` | identical routing and times to `0001` | EK / EK | `1042.00` **EUR** | `fare_brand_name: "Economy Flex"`, refundable with a `75.00 EUR` penalty, 2 checked bags. Same `comparison_key` as `0001` and `0002`. |

Offers `0001`, `0002` and `0004` deliberately share the same `comparison_key`
(`6ee6b0a8d2bd06e2f3b0e0e2c2e6a6d1`) because they are the same journey; `0003` differs. If Duffel's
real `comparison_key` semantics match that, it is a ready-made cross-check on our own shape key.

### Deliberately constructed, not observed

Two things in these fixtures are shaped by the schema but are **not** routings I have any evidence
actually operate. They exist to exercise fields the domain model has to survive, and they are called
out here so nobody later mistakes them for observed reality:

- The technical stop at **BUD** on Amadeus offer 4's AMS-DOH segment.
- The technical stop at **TBS** on Duffel offer `...0003`'s IST-DEL segment.

Technical stops are rare on this market. Both are geographically coherent as fuel stops and both are
internally consistent in their timings, but treat them as synthetic test material.

The rest of the flight numbers and hub routings are modelled on real published services. The times
and prices attached to them are still invented.

## Internal consistency

Verified by script over both files, using fixed July-2027 UTC offsets:

- Every `duration` / segment matches `arrival_utc - departure_utc` exactly.
- Every itinerary/slice `duration` matches `last_arrival_utc - first_departure_utc` exactly.
- Both technical-stop `duration` values match their own `arrivalAt`/`departureAt` (Amadeus) and
  `arriving_at`/`departing_at` (Duffel).
- Layovers: 250 min (EK, both files), 280 min (TK, both files), 215 min (QR, Amadeus only). All three
  are inside the closed `[180, 360]` validity interval from D8, and they land in two different
  penalty bands under D9, which is intentional.
- The codeshare pairs are byte-identical on all four time fields, in both files.
- Amadeus offer 3 has local day offset `+1` and UTC day offset `0` — the §4 breaking case, present on
  purpose.

## Needs verification against a real response

This is the actual deliverable of this spike. Every item below is something I could not confirm from
the published docs and therefore either guessed at or included speculatively. **Check each one the
first time a real call succeeds**, before trusting the mappers.

### Amadeus — high risk (these change mapper behaviour)

1. **Is `operating` omitted entirely when marketing == operating?** The docs describe `operating`
   as present when the operating carrier differs. My fixture always emits it. If real responses
   **omit** it for non-codeshare segments, the mapper must default
   `operating_carrier = carrierCode` on absence — and getting this wrong silently breaks codeshare
   detection, which is the whole point of finding 0.2. **Check this first.**
2. **Element shape of `segments[].stops[]`.** I used `iataCode` / `duration` / `arrivalAt` /
   `departureAt`, which is what the `FlightStop` schema name implies, but the spec extract did not
   enumerate the element fields. Confirm the exact key names and whether the stop times are also
   offsetless local.
3. **Does a segment `duration` include its own technical stop ground time?** I assumed yes
   (offer 4's `PT9H35M` spans the BUD stop). If Amadeus reports flight time only, every
   duration-vs-endpoint invariant in the normalizer will fail on stopping segments.
4. **Does `meta.links` on flight-offers actually carry `next`/`last`?** The `next`/`last` example in
   the pagination guide is from a reference-data endpoint. Flight Offers Search may only ever return
   `self`, with result count governed by `max`. The paginating iterator depends on this.
5. **`total` vs `grandTotal` divergence.** I set them equal on every offer. Confirm a real case where
   they differ (they do when additional services are priced) so the "always read `grandTotal`" rule
   is actually exercised by a fixture.

### Amadeus — medium risk (fields I included on partial evidence)

6. `isUpsellOffer` — included on all offers; the spec extract did not list it. Confirm it exists and
   whether it is always present or only on upsell offers.
7. `includedCabinBags` — included on offers 1 and 5; not in the spec extract's
   `FareDetailsBySegment` field list. Confirm presence and shape.
8. `amenities[]` on `fareDetailsBySegment` — included on offer 1 only. Confirm the element shape
   (`description` / `isChargeable` / `amenityType` / `amenityProvider.name`) and the full
   `amenityType` enum.
9. `brandedFareLabel` — confirm it is a sibling of `brandedFare` rather than nested, and whether
   either can be absent on unbranded fares.
10. `pricingOptions.refundableFare` / `noRestrictionFare` / `noPenaltyFare` — confirm these are
    actually returned in the search response rather than only accepted as request filters.
11. `fees[]` types — I used `SUPPLIER` and `TICKETING` with `0.00`. Confirm which types appear and
    whether `taxes[]` and `refundableTaxes` show up on this endpoint.
12. `lastTicketingDate` vs `lastTicketingDateTime` — I set both to the same bare date. The spec says
    the latter is a date-time. Confirm the real format, and specifically whether it carries any
    offset (it matters, see the mapping sketch: this is **not** an offer-expiry instant).
13. `dictionaries.locations` entries — I emitted `cityCode` + `countryCode` only. Confirm nothing
    else appears (some Amadeus endpoints add `type`/`subType`).
14. `numberOfBookableSeats` — confirm that `9` means "9 or more" rather than exactly 9.
15. Zero-padding of `segments[].number`. I used `148`. Confirm whether Amadeus ever returns `0148`,
    because a dedup display key that treats those as different is wrong.

### Duffel — high risk

16. **Does the airport object really carry `time_zone` (IANA)?** I included it on every airport
    object. If it is genuinely there, it is a useful cross-check against
    `config/airports.yaml` — but see the mapping sketch: it must be a *check*, not the authority,
    because provider fields are untrusted input under §8.1.
17. **Are `departing_at` / `arriving_at` truly offsetless local?** I wrote them as
    `2027-07-17T14:55:00` with no `Z` and no offset, matching the plan's §4 assertion. Confirm.
    Duffel's *metadata* timestamps (`created_at`, `expires_at`) are UTC with `Z` in my fixture —
    confirm the two really do differ in shape, because a mapper that treats them alike will be
    wrong about one of them.
18. **`available_services[].metadata` shape.** I invented
    `{type, maximum_weight_kg, maximum_height_cm, maximum_length_cm, maximum_depth_cm}` for a
    baggage service. Confirm against a real offer with services.
19. **`comparison_key` semantics.** I made same-journey offers share it. Confirm what Duffel actually
    keys on — if it already means "same journey", our shape key gets a free oracle; if it includes
    fare attributes, it does not.

### Duffel — medium risk

20. `ngs_shelf` — included as an integer (`1`/`2`/`4`). Confirm type, range, and nullability.
21. Segment-level `conditions` — the schema list mentions it "if present"; I omitted it from every
    segment and only populated slice-level and offer-level. Confirm whether it appears.
22. `total_emissions_kg` as a **string** (`"812"`) rather than a number.
23. `distance` as a string with four decimal places (`"5162.4408"`) and its unit (km assumed).
24. `intended_services` and `intended_payment_methods` — listed in the schema but omitted from my
    fixture entirely. Confirm whether they are always present (possibly as `[]`) on a fresh offer.
25. `supported_loyalty_programmes` — I used bare IATA codes. Confirm.
26. `owner` vs `marketing_carrier` — confirm whether the full airline object (with the three URL
    fields) is repeated on every segment carrier or only on `owner`. I abbreviated the segment-level
    carrier objects on purpose to see whether that matters.
27. `passengers[].given_name` / `family_name` / `age` as `null` on a pre-booking offer. Confirm — and
    see §8.3 note in the mapping sketch about not modelling these at all.
28. `payment_requirements.price_guarantee_expires_at` being `null` when
    `requires_instant_payment: true`. I assumed that pairing.
29. Cursor format for `meta.after`. I used a base64-looking Erlang term
    (`g2wAAAACbQAAABZ...`) copied in shape from the docs' example. It must be treated as an opaque
    string and never parsed.

### Cross-cutting

30. **Are technical stops ever actually returned on AMS-DEL one-stop economy inventory?** Both
    technical stops here are constructed. If real inventory never shows one, the
    `technical_stops` handling stays untested by live data and the fixture is the only coverage —
    which is fine, but know that it is the only coverage.
31. **Whether either provider returns a consumer booking URL.** From the documented schemas, neither
    does — see the mapping sketch section 3 for the field-by-field check that refutes it. Confirm on
    a real response that no undocumented URL field appears.
32. **The `_fixture_note` key itself.** It does not exist in real responses. If the mappers use
    strict/`extra="forbid"` parsing at the top level, the loader must pop it. Prefer popping it in
    the test helper over loosening the model.

## Using these in tests

```python
import json
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "providers"

def load_amadeus() -> dict:
    payload = json.loads((FIXTURES / "amadeus_offers_sample.json").read_text(encoding="utf-8"))
    payload.pop("_fixture_note", None)          # not present in real responses
    return payload

def load_duffel_offers() -> dict:
    payload = json.loads((FIXTURES / "duffel_offers_sample.json").read_text(encoding="utf-8"))
    return payload["offers_list_response"]      # the real GET /air/offers body
```

Because the values are invented, these fixtures are only valid for testing **mapping and
normalization** — field extraction, duration parsing, UTC conversion, layover arithmetic, dedup,
currency handling. They prove nothing about pricing accuracy or provider availability.

See `spikes/mapping_sketch.md` for the field-by-field mapping onto the domain model, the places the
two providers disagree, and the model gaps these payloads exposed.
