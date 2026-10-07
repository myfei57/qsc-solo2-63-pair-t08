"""Immutable-by-convention domain records and their JSON codecs."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from .enums import (
    BagState,
    FlightState,
    JobState,
    MaintenanceState,
    ShiftAction,
    YieldPolicy,
)


def _clean(raw: Any) -> Any:
    """Normalise nested containers so codecs round-trip deterministically."""

    if isinstance(raw, dict):
        return {str(key): _clean(value) for key, value in raw.items()}
    if isinstance(raw, (list, tuple)):
        return [_clean(item) for item in raw]
    return raw


@dataclass(frozen=True)
class Flight:
    id: str
    code: str
    terminal: str = "T1"
    carousel: str = ""
    chute: str = ""
    state: FlightState = FlightState.OPEN
    batch: int = 0
    schedule_generation: int = 1
    cutoff_at: str = ""
    closed_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "code": self.code,
            "terminal": self.terminal,
            "carousel": self.carousel,
            "chute": self.chute,
            "state": self.state.value,
            "batch": self.batch,
            "schedule_generation": self.schedule_generation,
            "cutoff_at": self.cutoff_at,
            "closed_at": self.closed_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Flight":
        return cls(
            id=str(raw["id"]),
            code=str(raw.get("code", "")),
            terminal=str(raw.get("terminal", "T1")),
            carousel=str(raw.get("carousel", "")),
            chute=str(raw.get("chute", "")),
            state=FlightState(raw.get("state", FlightState.OPEN.value)),
            batch=int(raw.get("batch", 0)),
            schedule_generation=int(raw.get("schedule_generation", 1)),
            cutoff_at=str(raw.get("cutoff_at", "")),
            closed_at=str(raw.get("closed_at", "")),
        )

    def evolved(self, **changes: Any) -> "Flight":
        return replace(self, **changes)


@dataclass(frozen=True)
class Bag:
    id: str
    barcode: str
    sequence: int
    epoch: int = 0
    flight_id: str = ""
    chute_id: str = ""
    state: BagState = BagState.CHECKED_IN

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "barcode": self.barcode,
            "sequence": self.sequence,
            "epoch": self.epoch,
            "flight_id": self.flight_id,
            "chute_id": self.chute_id,
            "state": self.state.value,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Bag":
        return cls(
            id=str(raw["id"]),
            barcode=str(raw.get("barcode", "")),
            sequence=int(raw.get("sequence", 0)),
            epoch=int(raw.get("epoch", 0)),
            flight_id=str(raw.get("flight_id", "")),
            chute_id=str(raw.get("chute_id", "")),
            state=BagState(raw.get("state", BagState.CHECKED_IN.value)),
        )

    def evolved(self, **changes: Any) -> "Bag":
        return replace(self, **changes)


@dataclass(frozen=True)
class Reading:
    barcode: str
    bag_id: str
    sequence: int
    epoch: int
    committed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "barcode": self.barcode,
            "bag_id": self.bag_id,
            "sequence": self.sequence,
            "epoch": self.epoch,
            "committed": self.committed,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Reading":
        return cls(
            barcode=str(raw.get("barcode", "")),
            bag_id=str(raw.get("bag_id", "")),
            sequence=int(raw.get("sequence", 0)),
            epoch=int(raw.get("epoch", 0)),
            committed=bool(raw.get("committed", False)),
        )


@dataclass(frozen=True)
class ChuteAssignment:
    flight_id: str
    chute_id: str
    generation: int = 1
    updated_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "flight_id": self.flight_id,
            "chute_id": self.chute_id,
            "generation": self.generation,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ChuteAssignment":
        return cls(
            flight_id=str(raw["flight_id"]),
            chute_id=str(raw.get("chute_id", "")),
            generation=int(raw.get("generation", 1)),
            updated_at=str(raw.get("updated_at", "")),
        )


@dataclass(frozen=True)
class SortRecord:
    bag_id: str
    flight_id: str
    chute_id: str
    sequence: int
    epoch: int
    at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "bag_id": self.bag_id,
            "flight_id": self.flight_id,
            "chute_id": self.chute_id,
            "sequence": self.sequence,
            "epoch": self.epoch,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SortRecord":
        return cls(
            bag_id=str(raw["bag_id"]),
            flight_id=str(raw.get("flight_id", "")),
            chute_id=str(raw.get("chute_id", "")),
            sequence=int(raw.get("sequence", 0)),
            epoch=int(raw.get("epoch", 0)),
            at=str(raw.get("at", "")),
        )


@dataclass(frozen=True)
class TransferCommand:
    id: str
    flight_id: str
    carousel: str
    batch: int
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "flight_id": self.flight_id,
            "carousel": self.carousel,
            "batch": self.batch,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TransferCommand":
        return cls(
            id=str(raw["id"]),
            flight_id=str(raw.get("flight_id", "")),
            carousel=str(raw.get("carousel", "")),
            batch=int(raw.get("batch", 0)),
            created_at=str(raw.get("created_at", "")),
        )


@dataclass(frozen=True)
class LoadRecord:
    bag_id: str
    carousel: str
    batch: int
    at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "bag_id": self.bag_id,
            "carousel": self.carousel,
            "batch": self.batch,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "LoadRecord":
        return cls(
            bag_id=str(raw["bag_id"]),
            carousel=str(raw.get("carousel", "")),
            batch=int(raw.get("batch", 0)),
            at=str(raw.get("at", "")),
        )


@dataclass(frozen=True)
class Sequence:
    epoch: int = 0
    next_value: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"epoch": self.epoch, "next": self.next_value}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Sequence":
        return cls(epoch=int(raw.get("epoch", 0)), next_value=int(raw.get("next", 0)))


@dataclass(frozen=True)
class AuditEntry:
    sequence: int
    kind: str
    subject: str
    detail: dict[str, Any] = field(default_factory=dict)
    prev_digest: str = ""
    digest: str = ""
    at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "kind": self.kind,
            "subject": self.subject,
            "detail": _clean(self.detail),
            "prev_digest": self.prev_digest,
            "digest": self.digest,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AuditEntry":
        return cls(
            sequence=int(raw.get("sequence", 0)),
            kind=str(raw.get("kind", "")),
            subject=str(raw.get("subject", "")),
            detail=_clean(raw.get("detail", {})),
            prev_digest=str(raw.get("prev_digest", "")),
            digest=str(raw.get("digest", "")),
            at=str(raw.get("at", "")),
        )


@dataclass(frozen=True)
class SortJob:
    """One scheduled sorting window on one chute.

    ``segments`` holds the windows the job actually runs in. A job paused for
    maintenance has its segment list split and a compensation segment appended,
    so the pause is a reschedule, never lost work.
    """

    id: str
    chute_id: str
    start_at: str
    end_at: str
    state: JobState = JobState.PLANNED
    generation: int = 1
    segments: tuple[tuple[str, str], ...] = ()
    note: str = ""
    temp_for_maintenance: str = ""

    def __post_init__(self) -> None:
        if not self.segments:
            object.__setattr__(self, "segments", ((self.start_at, self.end_at),))

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "chute_id": self.chute_id,
            "start_at": self.start_at,
            "end_at": self.end_at,
            "state": self.state.value,
            "generation": self.generation,
            "segments": [[start, end] for start, end in self.segments],
            "note": self.note,
            "temp_for_maintenance": self.temp_for_maintenance,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SortJob":
        segments_raw = raw.get("segments") or []
        segments = tuple((str(item[0]), str(item[1])) for item in segments_raw)
        return cls(
            id=str(raw["id"]),
            chute_id=str(raw.get("chute_id", "")),
            start_at=str(raw.get("start_at", "")),
            end_at=str(raw.get("end_at", "")),
            state=JobState(raw.get("state", JobState.PLANNED.value)),
            generation=int(raw.get("generation", 1)),
            segments=segments,
            note=str(raw.get("note", "")),
            temp_for_maintenance=str(raw.get("temp_for_maintenance", "")),
        )

    def evolved(self, **changes: Any) -> "SortJob":
        return replace(self, **changes)


@dataclass(frozen=True)
class MaintenanceRequest:
    """A declared maintenance window on one chute."""

    id: str
    chute_id: str
    requested_start: str
    requested_end: str
    start_at: str
    end_at: str
    state: MaintenanceState = MaintenanceState.REQUESTED
    reason: str = ""
    policy: YieldPolicy | None = None
    decision_id: str = ""
    barrier_id: str = ""
    recovery_id: str = ""
    declared_at: str = ""
    finished_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "chute_id": self.chute_id,
            "requested_start": self.requested_start,
            "requested_end": self.requested_end,
            "start_at": self.start_at,
            "end_at": self.end_at,
            "state": self.state.value,
            "reason": self.reason,
            "policy": self.policy.value if self.policy else "",
            "decision_id": self.decision_id,
            "barrier_id": self.barrier_id,
            "recovery_id": self.recovery_id,
            "declared_at": self.declared_at,
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "MaintenanceRequest":
        policy_raw = str(raw.get("policy", ""))
        return cls(
            id=str(raw["id"]),
            chute_id=str(raw.get("chute_id", "")),
            requested_start=str(raw.get("requested_start", "")),
            requested_end=str(raw.get("requested_end", "")),
            start_at=str(raw.get("start_at", "")),
            end_at=str(raw.get("end_at", "")),
            state=MaintenanceState(raw.get("state", MaintenanceState.REQUESTED.value)),
            reason=str(raw.get("reason", "")),
            policy=YieldPolicy(policy_raw) if policy_raw else None,
            decision_id=str(raw.get("decision_id", "")),
            barrier_id=str(raw.get("barrier_id", "")),
            recovery_id=str(raw.get("recovery_id", "")),
            declared_at=str(raw.get("declared_at", "")),
            finished_at=str(raw.get("finished_at", "")),
        )

    def evolved(self, **changes: Any) -> "MaintenanceRequest":
        return replace(self, **changes)


@dataclass(frozen=True)
class YieldDecision:
    """The recorded ruling of a maintenance/sorting conflict.

    ``concession`` states, in operator language, what the chosen side gave up:
    flight timeliness for pause-sort, maintenance duration for wait-sort.
    """

    id: str
    maintenance_id: str
    chute_id: str
    policy: YieldPolicy
    conflicts: tuple[str, ...]
    original_start: str
    original_end: str
    approved_start: str
    approved_end: str
    delay_minutes: int
    paused_jobs: tuple[str, ...]
    flight_impact: dict[str, int]
    concession: str
    created_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "maintenance_id": self.maintenance_id,
            "chute_id": self.chute_id,
            "policy": self.policy.value,
            "conflicts": list(self.conflicts),
            "original_start": self.original_start,
            "original_end": self.original_end,
            "approved_start": self.approved_start,
            "approved_end": self.approved_end,
            "delay_minutes": self.delay_minutes,
            "paused_jobs": list(self.paused_jobs),
            "flight_impact": _clean(self.flight_impact),
            "concession": self.concession,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "YieldDecision":
        return cls(
            id=str(raw["id"]),
            maintenance_id=str(raw.get("maintenance_id", "")),
            chute_id=str(raw.get("chute_id", "")),
            policy=YieldPolicy(raw.get("policy", YieldPolicy.WAIT_SORT.value)),
            conflicts=tuple(str(item) for item in raw.get("conflicts", [])),
            original_start=str(raw.get("original_start", "")),
            original_end=str(raw.get("original_end", "")),
            approved_start=str(raw.get("approved_start", "")),
            approved_end=str(raw.get("approved_end", "")),
            delay_minutes=int(raw.get("delay_minutes", 0)),
            paused_jobs=tuple(str(item) for item in raw.get("paused_jobs", [])),
            flight_impact={
                str(key): int(value) for key, value in raw.get("flight_impact", {}).items()
            },
            concession=str(raw.get("concession", "")),
            created_at=str(raw.get("created_at", "")),
        )


@dataclass(frozen=True)
class ShiftRecord:
    """One bag's peak-shifting disposition, reviewable line by line."""

    id: str
    maintenance_id: str
    bag_id: str
    flight_id: str
    from_chute: str
    to_chute: str
    action: ShiftAction
    cutoff_at: str
    reason: str
    restored: bool = False
    at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "maintenance_id": self.maintenance_id,
            "bag_id": self.bag_id,
            "flight_id": self.flight_id,
            "from_chute": self.from_chute,
            "to_chute": self.to_chute,
            "action": self.action.value,
            "cutoff_at": self.cutoff_at,
            "reason": self.reason,
            "restored": self.restored,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ShiftRecord":
        return cls(
            id=str(raw["id"]),
            maintenance_id=str(raw.get("maintenance_id", "")),
            bag_id=str(raw.get("bag_id", "")),
            flight_id=str(raw.get("flight_id", "")),
            from_chute=str(raw.get("from_chute", "")),
            to_chute=str(raw.get("to_chute", "")),
            action=ShiftAction(raw.get("action", ShiftAction.HOLD.value)),
            cutoff_at=str(raw.get("cutoff_at", "")),
            reason=str(raw.get("reason", "")),
            restored=bool(raw.get("restored", False)),
            at=str(raw.get("at", "")),
        )

    def evolved(self, **changes: Any) -> "ShiftRecord":
        return replace(self, **changes)


@dataclass(frozen=True)
class BarrierEntry:
    """One accounted-for bag in the maintenance barrier manifest."""

    bag_id: str
    flight_id: str
    state: BagState
    disposition: ShiftAction
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "bag_id": self.bag_id,
            "flight_id": self.flight_id,
            "state": self.state.value,
            "disposition": self.disposition.value,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "BarrierEntry":
        return cls(
            bag_id=str(raw["bag_id"]),
            flight_id=str(raw.get("flight_id", "")),
            state=BagState(raw.get("state", BagState.HELD.value)),
            disposition=ShiftAction(raw.get("disposition", ShiftAction.HOLD.value)),
            detail=str(raw.get("detail", "")),
        )


@dataclass(frozen=True)
class BarrierManifest:
    """The signed-off inventory proving the chute is clear before work starts."""

    id: str
    maintenance_id: str
    chute_id: str
    entries: tuple[BarrierEntry, ...]
    clear: bool
    at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "maintenance_id": self.maintenance_id,
            "chute_id": self.chute_id,
            "entries": [entry.as_dict() for entry in self.entries],
            "clear": self.clear,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "BarrierManifest":
        return cls(
            id=str(raw["id"]),
            maintenance_id=str(raw.get("maintenance_id", "")),
            chute_id=str(raw.get("chute_id", "")),
            entries=tuple(BarrierEntry.from_dict(item) for item in raw.get("entries", [])),
            clear=bool(raw.get("clear", False)),
            at=str(raw.get("at", "")),
        )


@dataclass(frozen=True)
class RecoveryReport:
    """The way back: what the plant restored when the lane came back."""

    id: str
    maintenance_id: str
    chute_id: str
    restored_bags: tuple[str, ...]
    resumed_jobs: tuple[str, ...]
    completed_jobs: tuple[str, ...]
    cancelled: bool
    at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "maintenance_id": self.maintenance_id,
            "chute_id": self.chute_id,
            "restored_bags": list(self.restored_bags),
            "resumed_jobs": list(self.resumed_jobs),
            "completed_jobs": list(self.completed_jobs),
            "cancelled": self.cancelled,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RecoveryReport":
        return cls(
            id=str(raw["id"]),
            maintenance_id=str(raw.get("maintenance_id", "")),
            chute_id=str(raw.get("chute_id", "")),
            restored_bags=tuple(str(item) for item in raw.get("restored_bags", [])),
            resumed_jobs=tuple(str(item) for item in raw.get("resumed_jobs", [])),
            completed_jobs=tuple(str(item) for item in raw.get("completed_jobs", [])),
            cancelled=bool(raw.get("cancelled", False)),
            at=str(raw.get("at", "")),
        )
