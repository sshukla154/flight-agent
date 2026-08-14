"""Segment and Layover — the models the tz spike exists to get right.

Timezone rule (master plan S4): one canonical UTC instant per event, plus
the original local wall time and its IANA zone, stored side by side. Never
reconstruct local from UTC and a cached offset.

``depart_fold`` / ``arrive_fold`` and the computed ``ambiguous_local_time``
are NOT in the master plan's original ``Segment`` sketch. They exist
because spikes/tz_arithmetic.py proved that sketch cannot represent an
ambiguous (DST fall-back) or nonexistent (DST spring-forward) local
wall-clock reading, and that this is not a rare edge case at 10 European
origins. Python's ``datetime.fold`` attribute (PEP 495) carries this at
construction time, but it does not survive an ISO-8601 round trip —
``isoformat()`` drops it, and re-parsing a serialized local time always
comes back ``fold=0`` — so the fold actually used to resolve a local
reading into UTC must be captured as its own explicit, serializable field,
or it is silently lost across every cache write and JSON round trip.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, computed_field, model_validator

from flightagent.domain.airport import CarrierCode, IataCode
from flightagent.domain.enums import CabinClass


def classify_local_time(
    naive: datetime, zone: ZoneInfo
) -> Literal["normal", "ambiguous", "imaginary"]:
    """Classify a naive wall-clock reading against fold=0/fold=1 in `zone`.

    Ported from spikes/tz_arithmetic.py's ``classify_local`` (Phase 0 / T2
    spike). Exposed here (not module-private) so Phase 3's validation
    rules can reuse it to choose between ``RejectionCode.AMBIGUOUS_LOCAL_TIME``
    and ``.NONEXISTENT_LOCAL_TIME`` instead of re-implementing this logic.

    - "normal": the reading occurs exactly once (the common case).
    - "ambiguous": DST fall-back — the reading occurs twice; fold picks which.
    - "imaginary": DST spring-forward — the reading never occurs at all;
      fold picks which side of the gap to treat it as.
    """
    a = naive.replace(tzinfo=zone, fold=0)
    b = naive.replace(tzinfo=zone, fold=1)
    if a.utcoffset() == b.utcoffset():
        return "normal"
    round_trip = a.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
    return "imaginary" if round_trip != naive else "ambiguous"


def _resolve_zone(zone_key: str) -> ZoneInfo:
    try:
        return ZoneInfo(zone_key)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(
            f"unknown IANA zone {zone_key!r} — the normalizer must fail the task on an "
            f"unknown zone, never default to UTC (master plan S4)"
        ) from exc


def _assert_utc(label: str, value: datetime) -> None:
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{label} must be UTC (zero offset), got offset {value.utcoffset()}")


class Segment(BaseModel):
    """One flight leg, exactly as normalized from a provider's raw payload."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    segment_id: str
    origin: IataCode
    destination: IataCode
    depart_utc: AwareDatetime
    arrive_utc: AwareDatetime
    depart_local: AwareDatetime
    arrive_local: AwareDatetime
    origin_tz: str
    destination_tz: str
    marketing_carrier: CarrierCode
    operating_carrier: CarrierCode | None = None
    flight_number: str
    operating_flight_number: str | None = None
    """Amadeus cannot express an operating carrier's flight number, only its
    carrier code (mapping_sketch.md S2.1) — nullable, never assume known."""
    cabin: CabinClass
    technical_stops: int = Field(default=0, ge=0)
    duration: timedelta
    depart_fold: int = Field(default=0, ge=0, le=1)
    arrive_fold: int = Field(default=0, ge=0, le=1)

    @model_validator(mode="after")
    def _validate_consistency(self) -> Self:
        _assert_utc("depart_utc", self.depart_utc)
        _assert_utc("arrive_utc", self.arrive_utc)

        if self.arrive_utc <= self.depart_utc:
            raise ValueError(
                f"{self.origin}->{self.destination} arrives at or before it departs in UTC "
                f"({self.depart_utc.isoformat()} -> {self.arrive_utc.isoformat()})"
            )

        expected_duration = self.arrive_utc - self.depart_utc
        if self.duration != expected_duration:
            raise ValueError(
                f"duration {self.duration} does not match arrive_utc - depart_utc "
                f"({expected_duration})"
            )

        origin_zone = _resolve_zone(self.origin_tz)
        destination_zone = _resolve_zone(self.destination_tz)

        depart_resolved = self._resolve_utc(self.depart_local, origin_zone, self.depart_fold)
        if depart_resolved != self.depart_utc:
            raise ValueError(
                f"depart_local {self.depart_local.isoformat()} with fold={self.depart_fold} in "
                f"{self.origin_tz} resolves to {depart_resolved.isoformat()}, not depart_utc "
                f"{self.depart_utc.isoformat()} — depart_local/origin_tz/depart_fold/depart_utc "
                f"must be mutually consistent"
            )

        arrive_resolved = self._resolve_utc(self.arrive_local, destination_zone, self.arrive_fold)
        if arrive_resolved != self.arrive_utc:
            raise ValueError(
                f"arrive_local {self.arrive_local.isoformat()} with fold={self.arrive_fold} in "
                f"{self.destination_tz} resolves to {arrive_resolved.isoformat()}, not "
                f"arrive_utc {self.arrive_utc.isoformat()} — arrive_local/destination_tz/"
                f"arrive_fold/arrive_utc must be mutually consistent"
            )
        return self

    @staticmethod
    def _resolve_utc(local: datetime, zone: ZoneInfo, fold: int) -> datetime:
        naive = local.replace(tzinfo=None)
        return naive.replace(tzinfo=zone, fold=fold).astimezone(UTC)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ambiguous_local_time(self) -> bool:
        """True if depart_local or arrive_local is a genuinely ambiguous
        (DST fall-back) or nonexistent (DST spring-forward) wall-clock
        reading in its own zone.

        Computed, never a settable input — a caller cannot silently
        default this to False while still supplying a fold that only
        matters because the underlying reading was ambiguous. See
        ``RejectionCode.AMBIGUOUS_LOCAL_TIME`` / ``.NONEXISTENT_LOCAL_TIME``,
        which Phase 3's validation rules assign based on this flag plus
        ``classify_local_time`` for the finer-grained distinction.
        """
        origin_zone = _resolve_zone(self.origin_tz)
        destination_zone = _resolve_zone(self.destination_tz)
        depart_kind = classify_local_time(self.depart_local.replace(tzinfo=None), origin_zone)
        arrive_kind = classify_local_time(self.arrive_local.replace(tzinfo=None), destination_zone)
        return depart_kind != "normal" or arrive_kind != "normal"

    @property
    def arrival_day_offset(self) -> int:
        """The "+1 day" arrival marker, from LOCAL dates only.

        Master plan S4's breaking case: AMS depart 10:00 CEST = 08:00Z; DEL
        arrive 00:30 IST next day = 19:00Z the SAME day. UTC dates match;
        local dates differ by one. Deriving this from UTC dates gives the
        wrong answer while looking entirely plausible.
        """
        return (self.arrive_local.date() - self.depart_local.date()).days


class Layover(BaseModel):
    """The gap between two consecutive segments.

    Duration is computed EXCLUSIVELY in UTC (master plan S4) — this is the
    exact bug spikes/tz_arithmetic.py exists to prevent: naive local
    wall-clock subtraction across a DST boundary is wrong by up to an hour,
    in either direction, while still looking like a plausible number.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    airport: IataCode
    arrive_utc: AwareDatetime
    depart_utc: AwareDatetime
    duration: timedelta
    local_window: tuple[AwareDatetime, AwareDatetime]
    """(arrival local time, departure local time) at ``airport`` — display
    only, never used in any arithmetic."""
    requires_airport_change: bool
    requires_terminal_change: bool

    @model_validator(mode="after")
    def _validate_consistency(self) -> Self:
        _assert_utc("arrive_utc", self.arrive_utc)
        _assert_utc("depart_utc", self.depart_utc)

        if self.depart_utc <= self.arrive_utc:
            raise ValueError(
                f"layover at {self.airport} departs at or before it arrives in UTC "
                f"({self.arrive_utc.isoformat()} -> {self.depart_utc.isoformat()})"
            )

        expected = self.depart_utc - self.arrive_utc
        if self.duration != expected:
            raise ValueError(
                f"duration {self.duration} does not match depart_utc - arrive_utc ({expected}) "
                f"— never derive layover duration from local wall-clock subtraction"
            )
        return self
