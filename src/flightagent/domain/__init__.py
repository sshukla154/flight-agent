"""Domain models.

Master plan S3: "domain imports nothing internal" — every other layer may
import ``domain``, but ``domain`` itself imports only pydantic and stdlib
(``domain`` submodules import each other freely; the rule is about not
reaching out to ``providers``/``config``/``observability``/etc., not about
intra-package imports).
"""

from __future__ import annotations

from flightagent.domain.airport import CarrierCode, IataCode
from flightagent.domain.enums import (
    CabinClass,
    DirectTier,
    RejectionCode,
    RunStatus,
    StopMode,
    TaskState,
)
from flightagent.domain.ground import GroundLeg
from flightagent.domain.ids import compute_itinerary_id, compute_task_id, generate_run_id
from flightagent.domain.itinerary import (
    CodeshareReference,
    FareOption,
    Leg,
    NormalizedItinerary,
    RawOffer,
)
from flightagent.domain.money import CurrencyCode, Money
from flightagent.domain.policy import DestinationAnalysis, EarlyStopEvaluation
from flightagent.domain.run import (
    MultiOriginSearchRequest,
    OpenQuestion,
    OriginCandidate,
    RunEnvelope,
    RunMeta,
    SearchRequest,
    SearchTask,
    TaskOutcome,
)
from flightagent.domain.scoring import ScoreComponents, ScoredItinerary
from flightagent.domain.segment import Layover, Segment, classify_local_time
from flightagent.domain.validation import Rejection, ValidationResult

__all__ = [
    "CabinClass",
    "CarrierCode",
    "CodeshareReference",
    "CurrencyCode",
    "DestinationAnalysis",
    "DirectTier",
    "EarlyStopEvaluation",
    "FareOption",
    "GroundLeg",
    "IataCode",
    "Layover",
    "Leg",
    "Money",
    "MultiOriginSearchRequest",
    "NormalizedItinerary",
    "OpenQuestion",
    "OriginCandidate",
    "RawOffer",
    "Rejection",
    "RejectionCode",
    "RunEnvelope",
    "RunMeta",
    "RunStatus",
    "ScoreComponents",
    "ScoredItinerary",
    "SearchRequest",
    "SearchTask",
    "Segment",
    "StopMode",
    "TaskOutcome",
    "TaskState",
    "ValidationResult",
    "classify_local_time",
    "compute_itinerary_id",
    "compute_task_id",
    "generate_run_id",
]
