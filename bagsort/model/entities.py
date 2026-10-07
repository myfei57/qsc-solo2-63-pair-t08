"""Immutable-by-convention domain records and their JSON codecs."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from .enums import (
    BagState,
    ConflictPolicy,
    FlightState,
    MaintenanceWindowState,
    StaggerAction,
    StaggerStatus,
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
class MaintenanceWindow:
    """A reserved interval during which one chute is taken for maintenance."""

    id: str
    chute_id: str
    start_at: str
    end_at: str
    state: MaintenanceWindowState = MaintenanceWindowState.REQUESTED
    ticket: str = ""
    plan_id: str = ""
    opened_at: str = ""
    completed_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "chute_id": self.chute_id,
            "start_at": self.start_at,
            "end_at": self.end_at,
            "state": self.state.value,
            "ticket": self.ticket,
            "plan_id": self.plan_id,
            "opened_at": self.opened_at,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "MaintenanceWindow":
        return cls(
            id=str(raw["id"]),
            chute_id=str(raw.get("chute_id", "")),
            start_at=str(raw.get("start_at", "")),
            end_at=str(raw.get("end_at", "")),
            state=MaintenanceWindowState(raw.get("state", MaintenanceWindowState.REQUESTED.value)),
            ticket=str(raw.get("ticket", "")),
            plan_id=str(raw.get("plan_id", "")),
            opened_at=str(raw.get("opened_at", "")),
            completed_at=str(raw.get("completed_at", "")),
        )

    def evolved(self, **changes: Any) -> "MaintenanceWindow":
        return replace(self, **changes)


@dataclass(frozen=True)
class YieldPlan:
    """The committed decision about who gives way and what it costs."""

    id: str
    window_id: str
    chute_id: str
    policy: ConflictPolicy
    decided_at: str
    affected_flights: tuple[str, ...] = ()
    held_bags: int = 0
    rerouted_bags: int = 0
    clear_bags: int = 0
    maintenance_delay: int = 0
    schedule_delay: int = 0
    effective_start_at: str = ""
    resume_at: str = ""
    tradeoff: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "window_id": self.window_id,
            "chute_id": self.chute_id,
            "policy": self.policy.value,
            "decided_at": self.decided_at,
            "affected_flights": list(self.affected_flights),
            "held_bags": self.held_bags,
            "rerouted_bags": self.rerouted_bags,
            "clear_bags": self.clear_bags,
            "maintenance_delay": self.maintenance_delay,
            "schedule_delay": self.schedule_delay,
            "effective_start_at": self.effective_start_at,
            "resume_at": self.resume_at,
            "tradeoff": self.tradeoff,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "YieldPlan":
        return cls(
            id=str(raw["id"]),
            window_id=str(raw.get("window_id", "")),
            chute_id=str(raw.get("chute_id", "")),
            policy=ConflictPolicy(raw.get("policy", ConflictPolicy.WAIT_SCHEDULE.value)),
            decided_at=str(raw.get("decided_at", "")),
            affected_flights=tuple(str(item) for item in raw.get("affected_flights", ())),
            held_bags=int(raw.get("held_bags", 0)),
            rerouted_bags=int(raw.get("rerouted_bags", 0)),
            clear_bags=int(raw.get("clear_bags", 0)),
            maintenance_delay=int(raw.get("maintenance_delay", 0)),
            schedule_delay=int(raw.get("schedule_delay", 0)),
            effective_start_at=str(raw.get("effective_start_at", "")),
            resume_at=str(raw.get("resume_at", "")),
            tradeoff=str(raw.get("tradeoff", "")),
        )

    def evolved(self, **changes: Any) -> "YieldPlan":
        return replace(self, **changes)


@dataclass(frozen=True)
class StaggerEntry:
    """One bag's time stamped movement in the staggered plan, fully auditable."""

    sequence: int
    window_id: str
    bag_id: str
    flight_id: str
    action: StaggerAction
    status: StaggerStatus = StaggerStatus.PLANNED
    from_chute: str = ""
    to_chute: str = ""
    planned_at: str = ""
    done_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "window_id": self.window_id,
            "bag_id": self.bag_id,
            "flight_id": self.flight_id,
            "action": self.action.value,
            "status": self.status.value,
            "from_chute": self.from_chute,
            "to_chute": self.to_chute,
            "planned_at": self.planned_at,
            "done_at": self.done_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "StaggerEntry":
        return cls(
            sequence=int(raw.get("sequence", 0)),
            window_id=str(raw.get("window_id", "")),
            bag_id=str(raw.get("bag_id", "")),
            flight_id=str(raw.get("flight_id", "")),
            action=StaggerAction(raw.get("action", StaggerAction.HOLD.value)),
            status=StaggerStatus(raw.get("status", StaggerStatus.PLANNED.value)),
            from_chute=str(raw.get("from_chute", "")),
            to_chute=str(raw.get("to_chute", "")),
            planned_at=str(raw.get("planned_at", "")),
            done_at=str(raw.get("done_at", "")),
        )

    def evolved(self, **changes: Any) -> "StaggerEntry":
        return replace(self, **changes)
