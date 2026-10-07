"""Maintenance declaration, conflict ruling, peak shifting and recovery.

The lifecycle every request moves through::

    REQUESTED --resolve--> APPROVED --arm--> ARMED --start--> IN_PROGRESS
        |                     |                  |                 |
        +--cancel------------+                  |                 |
                                          cancel |                 |
                                               CANCELLED   complete |
                                                                 DONE

Each transition is a committed journal transaction. The chute is physically
taken only at ``start``, and ``start`` refuses unless the barrier manifest is
clear, so a crew can never arrive to find baggage still sitting in the lane.
"""

from __future__ import annotations

from dataclasses import dataclass

from bagsort.belt.belt import Belt
from bagsort.chute.assigner import ChuteAssigner
from bagsort.clock import Clock
from bagsort.errors import ConflictError, NotFoundError, ValidationError
from bagsort.ids import IdFactory
from bagsort.model.entities import (
    BarrierEntry,
    BarrierManifest,
    Bag,
    MaintenanceRequest,
    RecoveryReport,
    ShiftRecord,
    SortJob,
    YieldDecision,
)
from bagsort.model.enums import (
    BagState,
    JobState,
    MaintenanceState,
    ShiftAction,
    YieldPolicy,
)
from bagsort.ns.chutes import ChuteRegistry
from bagsort.store.store import Store
from .ledger import ArtifactLedger
from .scheduler import Scheduler
from .windows import (
    format as iso_format,
    minutes_between,
    overlaps,
    parse,
    shift as shift_window,
    subtract,
    validate_window,
)

ACTIVE_MAINTENANCE = (
    MaintenanceState.REQUESTED,
    MaintenanceState.APPROVED,
    MaintenanceState.ARMED,
    MaintenanceState.IN_PROGRESS,
)
CANCELLABLE = (
    MaintenanceState.REQUESTED,
    MaintenanceState.APPROVED,
    MaintenanceState.ARMED,
    MaintenanceState.IN_PROGRESS,
)


@dataclass(frozen=True)
class ConflictPreview:
    """What declare() found before any policy is chosen."""

    request: MaintenanceRequest
    conflicting_jobs: tuple[str, ...]
    can_resolve: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "request": self.request.as_dict(),
            "conflicting_jobs": list(self.conflicting_jobs),
            "can_resolve": self.can_resolve,
        }


@dataclass(frozen=True)
class BarrierResult:
    """The manifest plus the shift lines a reviewer can open one by one."""

    barrier: BarrierManifest
    shifts: tuple[ShiftRecord, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "barrier": self.barrier.as_dict(),
            "shifts": [shift.as_dict() for shift in self.shifts],
        }


class MaintenancePlanner:
    """Owns the maintenance state machine and the baggage dispositions."""

    def __init__(
        self,
        store: Store,
        clock: Clock,
        scheduler: Scheduler,
        chutes: ChuteAssigner,
        registry: ChuteRegistry,
        belt: Belt,
        ledger: ArtifactLedger,
    ) -> None:
        self._store = store
        self._clock = clock
        self._scheduler = scheduler
        self._chutes = chutes
        self._registry = registry
        self._belt = belt
        self._ledger = ledger
        self._maintenance_ids = IdFactory("mnt")
        self._decision_ids = IdFactory("dec")
        self._shift_ids = IdFactory("shf")
        self._barrier_ids = IdFactory("bar")
        self._recovery_ids = IdFactory("rec")
        self._temp_job_ids = IdFactory("job-tmp")

    # -- read side ---------------------------------------------------------

    def get(self, maintenance_id: str) -> MaintenanceRequest:
        request = self._store.state().maintenance.get(maintenance_id)
        if request is None:
            raise NotFoundError(f"maintenance {maintenance_id!r} does not exist")
        return request

    def list_maintenance(
        self, chute_id: str = "", state: MaintenanceState | None = None
    ) -> list[MaintenanceRequest]:
        requests = list(self._store.state().maintenance.values())
        if chute_id:
            requests = [request for request in requests if request.chute_id == chute_id]
        if state is not None:
            requests = [request for request in requests if request.state is state]
        return sorted(requests, key=lambda request: (request.start_at, request.id))

    def decision(self, maintenance_id: str) -> YieldDecision | None:
        request = self.get(maintenance_id)
        if not request.decision_id:
            return None
        return self._store.state().decisions[request.decision_id]

    def shifts(self, maintenance_id: str = "") -> list[ShiftRecord]:
        shifts = list(self._store.state().shifts.values())
        if maintenance_id:
            shifts = [shift for shift in shifts if shift.maintenance_id == maintenance_id]
        return sorted(shifts, key=lambda shift: (shift.cutoff_at, shift.bag_id))

    def shifts_for_bag(self, bag_id: str) -> list[ShiftRecord]:
        """Every peak-shift line for one bag, oldest first for review."""

        return [
            shift
            for shift in self._store.state().shifts.values()
            if shift.bag_id == bag_id
        ]

    def barrier(self, maintenance_id: str) -> BarrierManifest | None:
        request = self.get(maintenance_id)
        if not request.barrier_id:
            return None
        return self._store.state().barriers[request.barrier_id]

    def recovery(self, maintenance_id: str) -> RecoveryReport | None:
        request = self.get(maintenance_id)
        if not request.recovery_id:
            return None
        return self._store.state().recoveries[request.recovery_id]

    # -- declare -----------------------------------------------------------

    def declare(
        self,
        chute_id: str,
        start_at: str,
        end_at: str,
        reason: str = "",
    ) -> ConflictPreview:
        identifier = self._require_chute(chute_id)
        validate_window(start_at, end_at)
        for request in self.list_maintenance(identifier):
            if request.state in ACTIVE_MAINTENANCE:
                raise ConflictError(
                    f"chute {identifier} already has active maintenance "
                    f"{request.id!r} {request.start_at}/{request.end_at}; "
                    "maintenance windows must not overlap"
                )
        request = MaintenanceRequest(
            id=self._maintenance_ids.next(),
            chute_id=identifier,
            requested_start=start_at,
            requested_end=end_at,
            start_at=start_at,
            end_at=end_at,
            reason=reason.strip(),
            declared_at=self._clock.now_iso(),
        )
        conflicting = self._scheduler.conflicts(identifier, start_at, end_at)
        preview = ConflictPreview(
            request=request,
            conflicting_jobs=tuple(job.id for job in conflicting),
            can_resolve=True,
        )
        self._put_maintenance(request)
        self._ledger.write_request(request)
        return preview

    # -- resolve (choose who yields) ---------------------------------------

    def resolve(
        self, maintenance_id: str, policy: YieldPolicy
    ) -> YieldDecision:
        request = self._require_state(maintenance_id, MaintenanceState.REQUESTED)
        conflicts = self._scheduler.conflicts(
            request.chute_id, request.start_at, request.end_at
        )
        if policy is YieldPolicy.WAIT_SORT:
            decision = self._wait_decision(request, conflicts)
        else:
            decision = self._pause_decision(request, conflicts)
        with self._store.session() as session:
            approved = request.evolved(
                state=MaintenanceState.APPROVED,
                start_at=decision.approved_start,
                end_at=decision.approved_end,
                policy=policy,
                decision_id=decision.id,
            )
            session.state.maintenance[approved.id] = approved
            session.state.decisions[decision.id] = decision
            at = self._clock.now_iso()
            session.record(
                "maintenance.put", {"maintenance": approved.as_dict()}, at
            )
            session.record("decision.put", {"decision": decision.as_dict()}, at)
        self._ledger.write_request(self.get(maintenance_id))
        self._ledger.write_decision(decision)
        return decision

    def _wait_decision(
        self, request: MaintenanceRequest, conflicts: list[SortJob]
    ) -> YieldDecision:
        approved_start = request.start_at
        approved_end = request.end_at
        if conflicts:
            latest_end = max(parse(job.end_at) for job in conflicts)
            if latest_end > parse(approved_start):
                delay = minutes_between(approved_start, iso_format(latest_end))
                approved_start, approved_end = shift_window(
                    request.start_at, request.end_at, delay
                )
        concession = (
            "maintenance waits for sorting: the crew starts "
            f"{minutes_between(request.start_at, approved_start)} minutes late and "
            "the repair finish moves out by the same amount; flight timeliness "
            "and every scheduled sort window are untouched"
        )
        return YieldDecision(
            id=self._decision_ids.next(),
            maintenance_id=request.id,
            chute_id=request.chute_id,
            policy=YieldPolicy.WAIT_SORT,
            conflicts=tuple(job.id for job in conflicts),
            original_start=request.requested_start,
            original_end=request.requested_end,
            approved_start=approved_start,
            approved_end=approved_end,
            delay_minutes=minutes_between(request.start_at, approved_start),
            paused_jobs=(),
            flight_impact={},
            concession=concession,
            created_at=self._clock.now_iso(),
        )

    def _pause_decision(
        self, request: MaintenanceRequest, conflicts: list[SortJob]
    ) -> YieldDecision:
        cutoffs = self._flight_cutoffs_on_chute(request.chute_id)
        impact: dict[str, int] = {}
        for flight_id, cutoff_at in sorted(cutoffs.items()):
            if self._at_risk(cutoff_at, request):
                impact[flight_id] = minutes_between(request.start_at, request.end_at)
        concession = (
            "sorting yields to maintenance: conflicting sort jobs are paused and "
            "their lost work is rescheduled after the window; cutoff-at-risk "
            "bags are peak-shifted (load early, reroute or hold), so flight "
            "timeliness may degrade while the repair starts and finishes on time"
        )
        return YieldDecision(
            id=self._decision_ids.next(),
            maintenance_id=request.id,
            chute_id=request.chute_id,
            policy=YieldPolicy.PAUSE_SORT,
            conflicts=tuple(job.id for job in conflicts),
            original_start=request.requested_start,
            original_end=request.requested_end,
            approved_start=request.start_at,
            approved_end=request.end_at,
            delay_minutes=0,
            paused_jobs=tuple(job.id for job in conflicts),
            flight_impact=impact,
            concession=concession,
            created_at=self._clock.now_iso(),
        )

    # -- arm (execute the yield + peak shifting) ---------------------------

    def arm(self, maintenance_id: str) -> BarrierResult:
        request = self._require_state(maintenance_id, MaintenanceState.APPROVED)
        decision = self._store.state().decisions[request.decision_id]
        shifts: list[ShiftRecord] = []
        if decision.policy is YieldPolicy.WAIT_SORT:
            self._arm_wait(request)
        else:
            self._pause_jobs(request)
        # Early loading drives the real carousel, so it commits in its own
        # transactions before the arm transaction builds the barrier.
        shifts = self._peak_shift(request)
        with self._store.session() as session:
            at = self._clock.now_iso()
            barrier = self._build_barrier(session, request, shifts, at)
            armed = request.evolved(
                state=MaintenanceState.ARMED, barrier_id=barrier.id
            )
            session.state.maintenance[armed.id] = armed
            session.state.barriers[barrier.id] = barrier
            for shift in shifts:
                session.state.shifts[shift.id] = shift
            session.record("maintenance.put", {"maintenance": armed.as_dict()}, at)
            session.record("barrier.put", {"barrier": barrier.as_dict()}, at)
            for shift in shifts:
                session.record("shift.put", {"shift": shift.as_dict()}, at)
        result = BarrierResult(
            barrier=self.barrier(maintenance_id), shifts=tuple(shifts)
        )
        self._ledger.write_request(self.get(maintenance_id))
        self._ledger.write_barrier(result.barrier)
        self._ledger.write_shifts(maintenance_id, self.shifts(maintenance_id))
        return result

    def _arm_wait(self, request: MaintenanceRequest) -> None:
        """The crew waits: every originally conflicting job must be finished."""

        decision = self._store.state().decisions[request.decision_id]
        unfinished = [
            self._scheduler.get(job_id)
            for job_id in decision.conflicts
            if self._scheduler.get(job_id).state
            in (JobState.PLANNED, JobState.RUNNING, JobState.PAUSED)
        ]
        if unfinished:
            raise ConflictError(
                "wait-sort bargain is not met yet; finish jobs "
                + ", ".join(job.id for job in unfinished)
                + " before arming"
            )

    def _pause_jobs(self, request: MaintenanceRequest) -> None:
        window = (request.start_at, request.end_at)
        decision = self._store.state().decisions[request.decision_id]
        for job_id in decision.paused_jobs:
            job = self._scheduler.get(job_id)
            kept = tuple(
                part
                for segment in job.segments
                for part in subtract(segment, window)
            )
            lost_minutes = sum(
                minutes_length(intersection_of(segment, window))
                for segment in job.segments
                if overlaps(segment, window)
            )
            segments = list(kept)
            if lost_minutes:
                comp_start, comp_end = self._scheduler.first_free_slot(
                    job.chute_id, request.end_at, lost_minutes, ignore_job=job.id
                )
                segments.append((comp_start, comp_end))
            self._scheduler.replace_segments(
                job,
                tuple(sorted(segments)),
                state=JobState.PAUSED,
            )

    # -- start / complete / cancel -----------------------------------------

    def start(self, maintenance_id: str) -> MaintenanceRequest:
        request = self._require_state(maintenance_id, MaintenanceState.ARMED)
        barrier = self._store.state().barriers[request.barrier_id]
        if not barrier.clear:
            raise ConflictError(
                f"barrier {barrier.id} is not clear; "
                f"{len(barrier.entries)} bag(s) still occupy {request.chute_id}"
            )
        started = request.evolved(state=MaintenanceState.IN_PROGRESS)
        self._put_maintenance(started)
        self._ledger.write_request(started)
        return started

    def complete(self, maintenance_id: str) -> RecoveryReport:
        request = self._require_state(maintenance_id, MaintenanceState.IN_PROGRESS)
        return self._finish(request, cancelled=False)

    def cancel(self, maintenance_id: str) -> RecoveryReport | None:
        request = self.get(maintenance_id)
        if request.state not in CANCELLABLE:
            raise ConflictError(
                f"maintenance {maintenance_id!r} in state {request.state.value} "
                "cannot be cancelled"
            )
        if request.state in (
            MaintenanceState.REQUESTED,
            MaintenanceState.APPROVED,
        ):
            cancelled = request.evolved(
                state=MaintenanceState.CANCELLED,
                finished_at=self._clock.now_iso(),
            )
            self._put_maintenance(cancelled)
            self._ledger.write_request(cancelled)
            return None
        return self._finish(request, cancelled=True)

    def _finish(
        self, request: MaintenanceRequest, cancelled: bool
    ) -> RecoveryReport:
        at = self._clock.now_iso()
        decision = (
            self._store.state().decisions[request.decision_id]
            if request.decision_id
            else None
        )
        with self._store.session() as session:
            restored_bags: list[str] = []
            for shift in list(session.state.shifts.values()):
                if shift.maintenance_id != request.id or shift.restored:
                    continue
                bag = session.state.bags.get(shift.bag_id)
                if bag is None:
                    closed = shift.evolved(restored=True)
                    session.state.shifts[closed.id] = closed
                    session.record("shift.put", {"shift": closed.as_dict()}, at)
                    continue
                if shift.action in (ShiftAction.REROUTE, ShiftAction.HOLD):
                    updated = bag.evolved(
                        chute_id=request.chute_id, state=BagState.SORTED
                    )
                    session.state.bags[updated.id] = updated
                    session.record("bag.put", {"bag": updated.as_dict()}, at)
                closed = shift.evolved(restored=True)
                session.state.shifts[closed.id] = closed
                session.record("shift.put", {"shift": closed.as_dict()}, at)
                restored_bags.append(shift.bag_id)
            self._restore_assignments(session, request, at)
            resumed_jobs: list[str] = []
            completed_jobs: list[str] = []
            for job in list(session.state.sort_jobs.values()):
                if job.temp_for_maintenance == request.id:
                    stopped = job.evolved(state=JobState.COMPLETED)
                    session.state.sort_jobs[stopped.id] = stopped
                    session.record("sortjob.put", {"job": stopped.as_dict()}, at)
                    completed_jobs.append(stopped.id)
                elif (
                    decision
                    and job.id in decision.paused_jobs
                    and job.state is JobState.PAUSED
                ):
                    resumed = job.evolved(state=JobState.RUNNING)
                    session.state.sort_jobs[resumed.id] = resumed
                    session.record("sortjob.put", {"job": resumed.as_dict()}, at)
                    resumed_jobs.append(resumed.id)
            recovery = RecoveryReport(
                id=self._recovery_ids.next(),
                maintenance_id=request.id,
                chute_id=request.chute_id,
                restored_bags=tuple(sorted(restored_bags)),
                resumed_jobs=tuple(sorted(resumed_jobs)),
                completed_jobs=tuple(sorted(completed_jobs)),
                cancelled=cancelled,
                at=at,
            )
            state = MaintenanceState.CANCELLED if cancelled else MaintenanceState.DONE
            finished = request.evolved(
                state=state, recovery_id=recovery.id, finished_at=at
            )
            session.state.recoveries[recovery.id] = recovery
            session.state.maintenance[finished.id] = finished
            session.record("recovery.put", {"recovery": recovery.as_dict()}, at)
            session.record(
                "maintenance.put", {"maintenance": finished.as_dict()}, at
            )
        self._ledger.write_request(self.get(request.id))
        report = self.recovery(request.id)
        self._ledger.write_recovery(report)
        self._ledger.write_shifts(request.id, self.shifts(request.id))
        return report

    # -- peak shifting core ------------------------------------------------

    def _peak_shift(self, request: MaintenanceRequest) -> list[ShiftRecord]:
        """Move every bag sitting in the lane, urgency first, deterministically."""

        state = self._store.state()
        cutoffs = self._flight_cutoffs_on_chute(request.chute_id)
        bags = [
            state.bags[bag_id]
            for bag_id in sorted(state.bags)
            if self._occupies(state.bags[bag_id], request.chute_id)
        ]
        groups: dict[str, list[Bag]] = {}
        for bag in bags:
            groups.setdefault(bag.flight_id, []).append(bag)
        ordered_flights = sorted(
            groups,
            key=lambda flight_id: (
                cutoff_rank(cutoffs.get(flight_id, "")),
                flight_id,
            ),
        )
        shifts: list[ShiftRecord] = []
        alt_cache: dict[str, str] = {}
        for flight_id in ordered_flights:
            cutoff_at = cutoffs.get(flight_id, "")
            action, target, reason = self._disposition(
                request, flight_id, cutoff_at, alt_cache
            )
            for bag in sorted(groups[flight_id], key=lambda item: item.id):
                shifts.append(
                    self._apply_shift(request, bag, action, target, cutoff_at, reason)
                )
        return shifts

    def _disposition(
        self,
        request: MaintenanceRequest,
        flight_id: str,
        cutoff_at: str,
        alt_cache: dict[str, str],
    ) -> tuple[ShiftAction, str, str]:
        if cutoff_at and parse(cutoff_at) <= parse(request.end_at):
            return (
                ShiftAction.LOAD_EARLY,
                "",
                "cutoff at or before the window ends; load early so the bag is "
                "registered as loaded before the lane is taken",
            )
        alternate = alt_cache.get(flight_id)
        if alternate is None:
            alternate = self._pick_alternate(request, flight_id)
            alt_cache[flight_id] = alternate
        if alternate:
            return (
                ShiftAction.REROUTE,
                alternate,
                "continue sorting on a clear lane; the flight points there "
                "until the home chute returns",
            )
        return (
            ShiftAction.HOLD,
            "",
            "no clear alternate lane before cutoff; registered and held out "
            "of the chute, restored after maintenance",
        )

    def _apply_shift(
        self,
        request: MaintenanceRequest,
        bag: Bag,
        action: ShiftAction,
        target: str,
        cutoff_at: str,
        reason: str,
    ) -> ShiftRecord:
        shift = ShiftRecord(
            id=self._shift_ids.next(),
            maintenance_id=request.id,
            bag_id=bag.id,
            flight_id=bag.flight_id,
            from_chute=request.chute_id,
            to_chute=target,
            action=action,
            cutoff_at=cutoff_at,
            reason=reason,
            at=self._clock.now_iso(),
        )
        if action is ShiftAction.LOAD_EARLY:
            self._load_early(bag)
        elif action is ShiftAction.REROUTE:
            self._reroute(bag, target)
        else:
            self._hold(bag)
        return shift

    def _load_early(self, bag: Bag) -> None:
        """Place on the carousel and switch the batch: a real load record."""

        self._belt.place(bag)
        batch = max(1, self._belt.batch() + 1)
        self._belt.switch(self._flight_carousel(bag.flight_id), batch)

    def _reroute(self, bag: Bag, target_chute: str) -> None:
        at = self._clock.now_iso()
        with self._store.session() as session:
            updated = bag.evolved(chute_id=target_chute, state=BagState.SORTED)
            session.state.bags[updated.id] = updated
            session.record("bag.put", {"bag": updated.as_dict()}, at)
        self._chutes.assign(bag.flight_id, target_chute)

    def _hold(self, bag: Bag) -> None:
        at = self._clock.now_iso()
        with self._store.session() as session:
            updated = bag.evolved(state=BagState.HELD)
            session.state.bags[updated.id] = updated
            session.record("bag.put", {"bag": updated.as_dict()}, at)

    def _flight_carousel(self, flight_id: str) -> str:
        flight = self._store.state().flights.get(flight_id)
        if flight is not None and flight.carousel:
            return flight.carousel
        return "CR1"

    def _pick_alternate(self, request: MaintenanceRequest, flight_id: str) -> str:
        state = self._store.state()
        flight = state.flights.get(flight_id)
        for lane_id in sorted(self._registry.lanes):
            if lane_id == request.chute_id:
                continue
            lane = self._registry.lanes[lane_id]
            if not lane.enabled:
                continue
            if flight and flight.carousel and lane.carousel_id != flight.carousel:
                continue
            if self._scheduler.windows_free(
                lane_id, request.start_at, request.end_at
            ):
                self._ensure_temp_job(request, lane_id)
                return lane_id
        return ""

    def _ensure_temp_job(self, request: MaintenanceRequest, lane_id: str) -> None:
        if any(
            job.temp_for_maintenance == request.id and job.chute_id == lane_id
            for job in self._store.state().sort_jobs.values()
        ):
            return
        self._scheduler.schedule(
            self._temp_job_ids.next(),
            lane_id,
            request.start_at,
            request.end_at,
            note="temporary peak-shift window",
            temp_for_maintenance=request.id,
        )

    def _restore_assignments(self, session, request: MaintenanceRequest, at: str) -> None:
        """Point every rerouted flight back at its home chute for recovery."""

        from bagsort.model.entities import ChuteAssignment

        home = request.chute_id
        affected = {
            shift.flight_id
            for shift in session.state.shifts.values()
            if shift.maintenance_id == request.id
            and shift.action is ShiftAction.REROUTE
        }
        for flight_id in sorted(affected):
            assignment = session.state.assignments.get(flight_id)
            if assignment is None or assignment.chute_id == home:
                continue
            updated = ChuteAssignment(
                flight_id=flight_id,
                chute_id=home,
                generation=assignment.generation + 1,
                updated_at=at,
            )
            session.state.assignments[flight_id] = updated
            session.record("chute.assign", updated.as_dict(), at)

    # -- barrier -----------------------------------------------------------

    def _build_barrier(
        self,
        session,
        request: MaintenanceRequest,
        shifts: list[ShiftRecord],
        at: str,
    ) -> BarrierManifest:
        by_bag = {shift.bag_id: shift for shift in shifts}
        entries: list[BarrierEntry] = []
        for bag_id in sorted(session.state.bags):
            bag = session.state.bags[bag_id]
            if not self._occupies(bag, request.chute_id):
                continue
            shift = by_bag.get(bag_id)
            disposition = shift.action if shift else ShiftAction.HOLD
            detail = shift.reason if shift else "unaccounted bag still in the chute"
            entries.append(
                BarrierEntry(
                    bag_id=bag_id,
                    flight_id=bag.flight_id,
                    state=bag.state,
                    disposition=disposition,
                    detail=detail,
                )
            )
        return BarrierManifest(
            id=self._barrier_ids.next(),
            maintenance_id=request.id,
            chute_id=request.chute_id,
            entries=tuple(entries),
            clear=not entries,
            at=at,
        )

    # -- helpers -----------------------------------------------------------

    def _require_chute(self, chute_id: str) -> str:
        identifier = (chute_id or "").strip()
        if not identifier:
            raise ValidationError("chute id must not be empty")
        if self._registry.known(identifier):
            return identifier
        state = self._store.state()
        referenced = any(
            job.chute_id == identifier for job in state.sort_jobs.values()
        ) or any(
            assignment.chute_id == identifier
            for assignment in state.assignments.values()
        )
        if referenced:
            return identifier
        raise NotFoundError(f"chute {identifier!r} is not registered")

    def _require_state(
        self, maintenance_id: str, state: MaintenanceState
    ) -> MaintenanceRequest:
        request = self.get(maintenance_id)
        if request.state is not state:
            raise ConflictError(
                f"maintenance {maintenance_id!r} is {request.state.value}, "
                f"expected {state.value}"
            )
        return request

    def _flight_cutoffs_on_chute(self, chute_id: str) -> dict[str, str]:
        state = self._store.state()
        result: dict[str, str] = {}
        for flight_id, assignment in state.assignments.items():
            if assignment.chute_id == chute_id and flight_id in state.flights:
                result[flight_id] = state.flights[flight_id].cutoff_at
        for bag in state.bags.values():
            if bag.chute_id == chute_id and bag.flight_id in state.flights:
                result.setdefault(
                    bag.flight_id, state.flights[bag.flight_id].cutoff_at
                )
        return result

    def _at_risk(self, cutoff_at: str, request: MaintenanceRequest) -> bool:
        """A flight is at risk when its cutoff is at or before the window end."""

        if not cutoff_at:
            return False
        return parse(cutoff_at) <= parse(request.end_at)

    def _occupies(self, bag: Bag, chute_id: str) -> bool:
        return bag.state.occupies_chute and bag.chute_id == chute_id

    def _put_maintenance(self, request: MaintenanceRequest) -> None:
        with self._store.session() as session:
            session.state.maintenance[request.id] = request
            session.record(
                "maintenance.put",
                {"maintenance": request.as_dict()},
                self._clock.now_iso(),
            )


def minutes_length(segment: tuple[str, str]) -> int:
    return minutes_between(segment[0], segment[1])


def cutoff_rank(cutoff_at: str) -> tuple[int, str]:
    return (1, "") if not cutoff_at else (0, cutoff_at)


def intersection_of(segment: tuple[str, str], window: tuple[str, str]) -> tuple[str, str]:
    from .windows import intersection

    hit = intersection(segment, window)
    if hit is None:
        return segment
    return hit
