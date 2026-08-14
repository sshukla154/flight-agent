"""
Phase 0 / T2 de-risking spike: UTC + DST layover arithmetic.

THROWAWAY SPIKE. Not production code, not importable by src/. It exists to prove
one thing: that layover and total-duration arithmetic done exclusively in UTC
survives DST transitions, half-hour offsets and multi-day gaps, and that the
"+1 day" arrival marker must come from LOCAL dates instead.

Both providers (Amadeus "at", Duffel "departing_at") send LOCAL wall-clock times
with NO offset attached. So every event here is modelled the way the providers
actually deliver it: a naive datetime plus an IANA zone key, converted to UTC at
construction time. Doing the subtraction on the naive values is the bug this
spike exists to prevent.

Stdlib only: zoneinfo, datetime, dataclasses. No pip installs.

Run:  C:\\Python314\\python.exe spikes\\tz_arithmetic.py
Exit code 0 = every case passed. Non-zero = at least one failed.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Iterable

UTC = timezone.utc

# Layover validity rule, decision D8 in the master plan: CLOSED interval.
LAYOVER_MIN_MINUTES = 180
LAYOVER_MAX_MINUTES = 360


# ---------------------------------------------------------------------------
# Timezone resolution
#
# Primary path is zoneinfo. On a machine with no IANA database (bare CPython on
# Windows without the `tzdata` PyPI package) zoneinfo raises, and this spike
# switches to a hand-rolled EU-rule fallback so the ARITHMETIC can still be
# proven. That fallback is announced in a loud banner and is NOT acceptable in
# production: pin `tzdata` instead. See the report at the bottom of the output.
# ---------------------------------------------------------------------------

try:
    import zoneinfo

    _ZONEINFO_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - zoneinfo is stdlib since 3.9
    zoneinfo = None  # type: ignore[assignment]
    _ZONEINFO_IMPORT_ERROR = exc


def _last_sunday(year: int, month: int) -> date:
    """Last Sunday of the given month. EU DST transitions land on these."""
    if month == 12:
        d = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        d = date(year, month + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() + 1) % 7)


class _FixedOffsetZone(tzinfo):
    """A zone that never changes offset (Asia/Kolkata is +05:30 year-round)."""

    def __init__(self, key: str, offset: timedelta, abbr: str) -> None:
        self.key = key
        self._offset = offset
        self._abbr = abbr

    def utcoffset(self, dt: datetime | None) -> timedelta:
        return self._offset

    def dst(self, dt: datetime | None) -> timedelta:
        return timedelta(0)

    def tzname(self, dt: datetime | None) -> str:
        return self._abbr

    def __repr__(self) -> str:
        return f"_FixedOffsetZone({self.key!r})"


class _EuRuleZone(tzinfo):
    """
    EU seasonal-clock rule, implemented with PEP 495 `fold` semantics.

    DST starts on the last Sunday of March at 01:00 UTC and ends on the last
    Sunday of October at 01:00 UTC, EU-wide. That makes 02:00-03:00 local
    IMAGINARY in spring and 02:00-03:00 local AMBIGUOUS in autumn.

    fold handling, matching zoneinfo:
      ambiguous local time -> fold=0 picks the FIRST occurrence (still DST),
                              fold=1 picks the second (standard time)
      imaginary local time -> fold=0 uses the offset BEFORE the gap,
                              fold=1 uses the offset AFTER it
    """

    def __init__(self, key: str, std: timedelta, dst_: timedelta, std_abbr: str, dst_abbr: str) -> None:
        self.key = key
        self._std = std
        self._dst = dst_
        self._std_abbr = std_abbr
        self._dst_abbr = dst_abbr

    def _transitions_utc(self, year: int) -> tuple[datetime, datetime]:
        start = datetime.combine(_last_sunday(year, 3), datetime.min.time()) + timedelta(hours=1)
        end = datetime.combine(_last_sunday(year, 10), datetime.min.time()) + timedelta(hours=1)
        return start, end

    def _offset_for(self, dt: datetime) -> timedelta:
        naive = dt.replace(tzinfo=None)
        start, end = self._transitions_utc(naive.year)
        dst_valid = start <= (naive - self._dst) < end
        std_valid = not (start <= (naive - self._std) < end)

        if dst_valid and std_valid:  # ambiguous: the hour that happens twice
            return self._dst if dt.fold == 0 else self._std
        if dst_valid:
            return self._dst
        if std_valid:
            return self._std
        # imaginary: the hour that never happens
        return self._std if dt.fold == 0 else self._dst

    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        if dt is None:
            return None
        return self._offset_for(dt)

    def dst(self, dt: datetime | None) -> timedelta | None:
        if dt is None:
            return None
        return self._dst - self._std if self._offset_for(dt) == self._dst else timedelta(0)

    def tzname(self, dt: datetime | None) -> str | None:
        if dt is None:
            return None
        return self._dst_abbr if self._offset_for(dt) == self._dst else self._std_abbr

    def fromutc(self, dt: datetime) -> datetime:
        """
        UTC -> local. Implemented explicitly: tzinfo.fromutc()'s default walks
        through dst() on a wall clock, which misreads the spring gap once
        `_offset_for` applies PEP 495 fold rules to imaginary times.
        """
        naive = dt.replace(tzinfo=None)
        start, end = self._transitions_utc(naive.year)
        in_dst = start <= naive < end
        offset = self._dst if in_dst else self._std
        fold = 1 if (not in_dst and end <= naive < end + (self._dst - self._std)) else 0
        return (naive + offset).replace(tzinfo=self, fold=fold)

    def __repr__(self) -> str:
        return f"_EuRuleZone({self.key!r})"


_FALLBACK_ZONES: dict[str, tzinfo] = {
    "Europe/Amsterdam": _EuRuleZone(
        "Europe/Amsterdam", timedelta(hours=1), timedelta(hours=2), "CET", "CEST"
    ),
    "Europe/Brussels": _EuRuleZone(
        "Europe/Brussels", timedelta(hours=1), timedelta(hours=2), "CET", "CEST"
    ),
    "Asia/Kolkata": _FixedOffsetZone("Asia/Kolkata", timedelta(hours=5, minutes=30), "IST"),
    "Asia/Dubai": _FixedOffsetZone("Asia/Dubai", timedelta(hours=4), "+04"),
}

TZDB_SOURCE = "unknown"
TZDB_DETAIL = ""


def _probe_tzdb() -> None:
    """Decide once whether zoneinfo has a usable database."""
    global TZDB_SOURCE, TZDB_DETAIL
    if zoneinfo is None:
        TZDB_SOURCE = "fallback"
        TZDB_DETAIL = f"zoneinfo import failed: {_ZONEINFO_IMPORT_ERROR!r}"
        return
    try:
        zoneinfo.ZoneInfo("Europe/Amsterdam")
        zoneinfo.ZoneInfo("Asia/Kolkata")
    except Exception as exc:
        TZDB_SOURCE = "fallback"
        TZDB_DETAIL = f"{type(exc).__name__}: {exc}"
        return
    TZDB_SOURCE = "zoneinfo"
    TZDB_DETAIL = f"TZPATH={zoneinfo.TZPATH}"


def get_zone(key: str) -> tzinfo:
    if TZDB_SOURCE == "zoneinfo":
        return zoneinfo.ZoneInfo(key)  # type: ignore[union-attr]
    try:
        return _FALLBACK_ZONES[key]
    except KeyError:
        raise LookupError(
            f"No fallback rule for IANA zone {key!r}. The normalizer must FAIL the task "
            f"on an unknown zone, never default to UTC."
        ) from None


# ---------------------------------------------------------------------------
# Local-time classification. Providers send naive local times, so an ambiguous
# or imaginary wall clock is a real data condition, not a theoretical one.
# ---------------------------------------------------------------------------


def classify_local(naive: datetime, zone: tzinfo) -> str:
    a = naive.replace(tzinfo=zone, fold=0)
    b = naive.replace(tzinfo=zone, fold=1)
    if a.utcoffset() == b.utcoffset():
        return "normal"
    round_trip = a.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
    return "imaginary" if round_trip != naive else "ambiguous"


# ---------------------------------------------------------------------------
# The minimal domain model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Segment:
    """
    One flight leg exactly as a provider hands it over: naive local times plus
    an IANA zone per airport. UTC instants are DERIVED here, once, and every
    duration downstream reads only those.
    """

    origin: str
    destination: str
    depart_local: datetime  # naive
    arrive_local: datetime  # naive
    origin_tz: str
    destination_tz: str
    depart_fold: int = 0
    arrive_fold: int = 0

    depart_utc: datetime = field(init=False, repr=False)
    arrive_utc: datetime = field(init=False, repr=False)
    depart_local_kind: str = field(init=False, repr=False)
    arrive_local_kind: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        for label, value in (("depart_local", self.depart_local), ("arrive_local", self.arrive_local)):
            if value.tzinfo is not None:
                raise ValueError(f"{label} must be naive local wall time, got tz-aware {value!r}")

        oz = get_zone(self.origin_tz)
        dz = get_zone(self.destination_tz)

        dep_aware = self.depart_local.replace(tzinfo=oz, fold=self.depart_fold)
        arr_aware = self.arrive_local.replace(tzinfo=dz, fold=self.arrive_fold)

        object.__setattr__(self, "depart_utc", dep_aware.astimezone(UTC))
        object.__setattr__(self, "arrive_utc", arr_aware.astimezone(UTC))
        object.__setattr__(self, "depart_local_kind", classify_local(self.depart_local, oz))
        object.__setattr__(self, "arrive_local_kind", classify_local(self.arrive_local, dz))

        if self.arrive_utc <= self.depart_utc:
            raise ValueError(
                f"{self.origin}->{self.destination} arrives at or before it departs in UTC "
                f"({self.depart_utc.isoformat()} -> {self.arrive_utc.isoformat()})"
            )

    @property
    def duration(self) -> timedelta:
        return self.arrive_utc - self.depart_utc

    @property
    def arrival_day_offset(self) -> int:
        """The '+1 day' marker. LOCAL dates. Deriving this from UTC is the trap."""
        return (self.arrive_local.date() - self.depart_local.date()).days


@dataclass(frozen=True)
class Itinerary:
    segments: tuple[Segment, ...]

    def __post_init__(self) -> None:
        if not self.segments:
            raise ValueError("itinerary needs at least one segment")
        for prev, nxt in zip(self.segments, self.segments[1:]):
            if prev.destination != nxt.origin:
                raise ValueError(f"segments do not connect: {prev.destination} != {nxt.origin}")

    @property
    def layovers(self) -> tuple[timedelta, ...]:
        return tuple(
            nxt.depart_utc - prev.arrive_utc for prev, nxt in zip(self.segments, self.segments[1:])
        )

    @property
    def total_duration(self) -> timedelta:
        """From the endpoints, in UTC. Never by summing parts (see plan §4)."""
        return self.segments[-1].arrive_utc - self.segments[0].depart_utc

    @property
    def arrival_day_offset(self) -> int:
        return (self.segments[-1].arrive_local.date() - self.segments[0].depart_local.date()).days

    @property
    def stop_count(self) -> int:
        return len(self.segments) - 1


def minutes(td: timedelta) -> int:
    total = td.total_seconds() / 60
    if total != int(total):
        raise ValueError(f"non-integer minutes: {td!r}")
    return int(total)


def layover_accepted(td: timedelta) -> bool:
    m = minutes(td)
    return LAYOVER_MIN_MINUTES <= m <= LAYOVER_MAX_MINUTES


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

RESULTS: list[tuple[str, bool, str]] = []


def check(case: str, ok: bool, detail: str) -> None:
    RESULTS.append((case, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {case:<9} {detail}")


def expect(case: str, actual: object, expected: object, detail: str) -> None:
    ok = actual == expected
    suffix = "" if ok else f"   <-- expected {expected!r}, got {actual!r}"
    check(case, ok, f"{detail}{suffix}")


def ams_seg(
    origin: str,
    destination: str,
    dep: datetime,
    arr: datetime,
    otz: str = "Europe/Amsterdam",
    dtz: str = "Europe/Amsterdam",
    dep_fold: int = 0,
    arr_fold: int = 0,
) -> Segment:
    return Segment(origin, destination, dep, arr, otz, dtz, dep_fold, arr_fold)


def z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%MZ")


# ---------------------------------------------------------------------------


def case_1_dst_fallback() -> None:
    """
    THE CRITICAL ONE. Amsterdam, 2027-10-31 fall-back.
    Arrive 01:00 CEST, depart 03:30 CET. Local clock reads 2h30m. UTC says 210.
    """
    ams = get_zone("Europe/Amsterdam")

    def build(arrive_fold: int) -> Itinerary:
        inbound = Segment("DXB", "AMS", datetime(2027, 10, 30, 20, 0), datetime(2027, 10, 31, 1, 0),
                          "Asia/Dubai", "Europe/Amsterdam", arrive_fold=arrive_fold)
        outbound = ams_seg("AMS", "BRU", datetime(2027, 10, 31, 3, 30), datetime(2027, 10, 31, 4, 30),
                           dtz="Europe/Brussels", dep_fold=0)
        return Itinerary((inbound, outbound))

    itin = build(0)
    inbound, outbound = itin.segments
    lay = minutes(itin.layovers[0])
    naive_lay = minutes(outbound.depart_local - inbound.arrive_local)

    # NB: EU DST ends at 01:00 UTC = 03:00 CEST, so the hour that repeats in
    # Amsterdam is 02:00-02:59, NOT 01:00. 01:00 local is unambiguously CEST.
    expect("1-kind", classify_local(datetime(2027, 10, 31, 1, 0), ams), "normal",
           "01:00 local on 2027-10-31 is UNambiguous (CEST); the repeated hour is 02:00-02:59")
    expect("1-amb", classify_local(datetime(2027, 10, 31, 2, 30), ams), "ambiguous",
           "02:30 local on 2027-10-31 really is ambiguous (CEST 00:30Z or CET 01:30Z)")
    expect("1-naive", naive_lay, 150, "local-clock subtraction gives 150 min (this is the bug)")
    expect(
        "1-utc",
        lay,
        210,
        f"arrive {inbound.arrive_local:%H:%M} CEST ({z(inbound.arrive_utc)}) -> depart "
        f"{outbound.depart_local:%H:%M} CET ({z(outbound.depart_utc)}) = {lay} min",
    )
    expect("1-fold", minutes(build(1).layovers[0]), 210,
           "same 210 min with arrive_fold=1 - this case does not depend on the fold choice")
    check("1-accept", layover_accepted(itin.layovers[0]), "210 min accepted under closed [180,360]")

    # Where fold DOES decide the answer: an arrival inside the repeated hour.
    amb_in = Segment("DXB", "AMS", datetime(2027, 10, 30, 21, 30), datetime(2027, 10, 31, 2, 30),
                     "Asia/Dubai", "Europe/Amsterdam", arrive_fold=0)
    amb_in_late = Segment("DXB", "AMS", datetime(2027, 10, 30, 21, 30), datetime(2027, 10, 31, 2, 30),
                          "Asia/Dubai", "Europe/Amsterdam", arrive_fold=1)
    d0 = minutes(Itinerary((amb_in, outbound)).layovers[0])
    d1 = minutes(Itinerary((amb_in_late, outbound)).layovers[0])
    expect("1-swing", (d0, d1), (120, 60),
           "an arrival at the ambiguous 02:30 swings the layover by 60 min on fold alone")


def case_2_dst_springforward() -> None:
    """Amsterdam 2027-03-28. Arrive 01:30 CET, depart 05:00 CEST -> 150 min, rejected."""
    inbound = Segment("DXB", "AMS", datetime(2027, 3, 27, 21, 0), datetime(2027, 3, 28, 1, 30),
                      "Asia/Dubai", "Europe/Amsterdam")
    outbound = ams_seg("AMS", "BRU", datetime(2027, 3, 28, 5, 0), datetime(2027, 3, 28, 6, 0),
                       dtz="Europe/Brussels")
    itin = Itinerary((inbound, outbound))

    lay = minutes(itin.layovers[0])
    naive_lay = minutes(outbound.depart_local - inbound.arrive_local)

    expect("2-naive", naive_lay, 210, "local-clock subtraction gives 210 min (would wrongly accept)")
    expect("2-utc", lay, 150, f"UTC elapsed {z(inbound.arrive_utc)} -> {z(outbound.depart_utc)} = {lay} min")
    check("2-reject", not layover_accepted(itin.layovers[0]), "150 min rejected under closed [180,360]")

    imaginary = classify_local(datetime(2027, 3, 28, 2, 30), get_zone("Europe/Amsterdam"))
    expect("2-gap", imaginary, "imaginary", "02:30 local on 2027-03-28 never happens in Amsterdam")


def case_3_half_hour_offset() -> None:
    """AMS 10:00 CEST (08:00Z) -> DEL 00:30 IST next day (19:00Z same day) = 660 min."""
    seg = Segment("AMS", "DEL", datetime(2027, 7, 17, 10, 0), datetime(2027, 7, 18, 0, 30),
                  "Europe/Amsterdam", "Asia/Kolkata")
    itin = Itinerary((seg,))
    total = minutes(itin.total_duration)
    expect("3-dep", z(seg.depart_utc), "2027-07-17T08:00Z", "AMS 10:00 local -> 08:00Z (CEST = UTC+2)")
    expect("3-arr", z(seg.arrive_utc), "2027-07-17T19:00Z", "DEL 00:30 local -> 19:00Z (IST = UTC+5:30)")
    expect("3-total", total, 660, f"total duration exactly {total} min (11h00m)")


def case_4_arrival_day_offset() -> None:
    """UTC dates identical, local dates one apart. The +1 marker must come from local."""
    seg = Segment("AMS", "DEL", datetime(2027, 7, 17, 10, 0), datetime(2027, 7, 18, 0, 30),
                  "Europe/Amsterdam", "Asia/Kolkata")
    utc_offset = (seg.arrive_utc.date() - seg.depart_utc.date()).days
    expect("4-utc", utc_offset, 0, "UTC dates are the SAME day (2027-07-17 both) -> UTC gives +0")
    expect("4-local", seg.arrival_day_offset, 1, "local dates 07-17 -> 07-18 give the correct +1 marker")


def case_5_overnight_layover() -> None:
    """22:30 -> 01:45 next day = 195 min, accepted. No DST anywhere near."""
    inbound = Segment("DXB", "AMS", datetime(2027, 7, 17, 18, 0), datetime(2027, 7, 17, 22, 30),
                      "Asia/Dubai", "Europe/Amsterdam")
    outbound = ams_seg("AMS", "BRU", datetime(2027, 7, 18, 1, 45), datetime(2027, 7, 18, 2, 45),
                       dtz="Europe/Brussels")
    itin = Itinerary((inbound, outbound))
    lay = minutes(itin.layovers[0])
    expect("5-utc", lay, 195, f"22:30 -> 01:45 next day = {lay} min, crosses midnight")
    check("5-accept", layover_accepted(itin.layovers[0]), "195 min accepted")


def _layover_of(mins: int) -> timedelta:
    """Build a real two-segment itinerary whose layover is exactly `mins`."""
    arrive = datetime(2027, 7, 17, 12, 0)
    depart = arrive + timedelta(minutes=mins)
    inbound = Segment("DXB", "AMS", datetime(2027, 7, 17, 7, 30), arrive,
                      "Asia/Dubai", "Europe/Amsterdam")
    outbound = ams_seg("AMS", "BRU", depart, depart + timedelta(hours=1), dtz="Europe/Brussels")
    return Itinerary((inbound, outbound)).layovers[0]


def case_6_boundaries() -> None:
    """Closed interval [180, 360]: 179 out, 180 in, 360 in, 361 out."""
    for mins, want in ((179, False), (180, True), (360, True), (361, False)):
        got = layover_accepted(_layover_of(mins))
        verdict = "accepted" if want else "rejected"
        expect(f"6-{mins}", got, want, f"{mins} min -> {verdict}")


def case_7_multi_day_layover() -> None:
    """26h layover. A same-day clock delta would read 02:00 and wrongly accept."""
    arrive = datetime(2027, 7, 17, 12, 0)
    depart = arrive + timedelta(hours=26)
    inbound = Segment("DXB", "AMS", datetime(2027, 7, 17, 7, 30), arrive,
                      "Asia/Dubai", "Europe/Amsterdam")
    outbound = ams_seg("AMS", "BRU", depart, depart + timedelta(hours=1), dtz="Europe/Brussels")
    itin = Itinerary((inbound, outbound))
    lay = minutes(itin.layovers[0])
    clock_delta = (depart.hour * 60 + depart.minute) - (arrive.hour * 60 + arrive.minute)
    expect("7-clock", clock_delta, 120, "naive same-day clock delta reads 120 min (the wrong answer)")
    expect("7-utc", lay, 1560, f"elapsed layover is {lay} min (26h)")
    check("7-reject", not layover_accepted(itin.layovers[0]), "1560 min rejected")


def case_8_invariant() -> None:
    """sum(segment durations) + sum(layovers) == total_duration, on a DST-crossing 3-leg trip."""
    legs = (
        Segment("AMS", "DXB", datetime(2027, 10, 30, 14, 0), datetime(2027, 10, 30, 23, 30),
                "Europe/Amsterdam", "Asia/Dubai"),
        Segment("DXB", "AMS", datetime(2027, 10, 31, 2, 30), datetime(2027, 10, 31, 1, 0),
                "Asia/Dubai", "Europe/Amsterdam", arrive_fold=0),
        ams_seg("AMS", "BRU", datetime(2027, 10, 31, 3, 30), datetime(2027, 10, 31, 4, 30),
                dtz="Europe/Brussels"),
    )
    itin = Itinerary(legs)
    seg_sum = sum((s.duration for s in itin.segments), timedelta())
    lay_sum = sum(itin.layovers, timedelta())
    expect(
        "8-inv",
        minutes(seg_sum + lay_sum),
        minutes(itin.total_duration),
        f"segments {minutes(seg_sum)} + layovers {minutes(lay_sum)} == total {minutes(itin.total_duration)} min",
    )
    expect("8-legs", itin.stop_count, 2, "3 segments = 2 stops, 2 layovers")


def tzdb_report() -> None:
    print()
    print("-- tz database ------------------------------------------------------")
    print(f"python            : {sys.version.split()[0]} ({sys.executable})")
    print(f"zone source       : {TZDB_SOURCE}")
    print(f"detail            : {TZDB_DETAIL}")
    version = "<not installed>"
    try:
        import importlib.metadata as md

        version = md.version("tzdata")
    except Exception as exc:
        version = f"<unavailable: {type(exc).__name__}>"
    print(f"tzdata pkg version: {version}")
    if zoneinfo is not None:
        try:
            n = len(zoneinfo.available_timezones())
        except Exception:
            n = 0
        print(f"zoneinfo zones    : {n}")
    if TZDB_SOURCE != "zoneinfo":
        print()
        print("!! WARNING: zoneinfo found NO IANA database on this machine.")
        print("!! This spike ran against a hand-rolled EU-rule fallback so the")
        print("!! arithmetic could still be proven. That fallback is spike-only.")
        print("!! Production fix: add `tzdata` to project dependencies with an")
        print("!! exact pin (plan section 4.2), and record tzdata_version in run_meta.")


def main() -> int:
    _probe_tzdb()
    print("=" * 72)
    print("Phase 0 / T2 spike: UTC + DST layover arithmetic")
    print(f"zone source: {TZDB_SOURCE.upper()}   layover rule: closed [{LAYOVER_MIN_MINUTES},{LAYOVER_MAX_MINUTES}] min")
    if TZDB_SOURCE != "zoneinfo":
        print("*** NO IANA DATABASE PRESENT - running on the spike-only EU-rule fallback ***")
    print("=" * 72)

    # Sanity: the fallback's derived transition dates must match the plan's.
    expect("0-dst1", _last_sunday(2027, 3).isoformat(), "2027-03-28", "EU spring-forward date for 2027")
    expect("0-dst2", _last_sunday(2027, 10).isoformat(), "2027-10-31", "EU fall-back date for 2027")

    cases: Iterable = (
        case_1_dst_fallback,
        case_2_dst_springforward,
        case_3_half_hour_offset,
        case_4_arrival_day_offset,
        case_5_overnight_layover,
        case_6_boundaries,
        case_7_multi_day_layover,
        case_8_invariant,
    )
    for fn in cases:
        print()
        print(f"[{fn.__name__}]")
        try:
            fn()
        except Exception as exc:  # a raising case is a failing case
            check(fn.__name__, False, f"raised {type(exc).__name__}: {exc}")

    tzdb_report()

    failed = [name for name, ok, _ in RESULTS if not ok]
    print()
    print("-" * 72)
    print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print(f"FAILED: {', '.join(failed)}")
        return 1
    print("ALL CASES PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
