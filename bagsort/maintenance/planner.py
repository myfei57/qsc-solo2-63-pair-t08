"""Conflict resolution between chute maintenance and live sorting.

Two policies with opposite costs are offered:

* ``wait_schedule`` lets the running schedule finish before the chute is
  surrendered. Flight time integrity is preserved; maintenance gives up its
  start time.
* ``yield_to_maintenance`` pauses sorting at the window start. Maintenance
  keeps its duration; the schedule gives up time (held bags, reroutes and
  possible cut-off risk).

Every consequence is committed in a single journal transaction together with
the bag states it produces, so a bag can never be half moved. Per-bag stagger
entries are the auditable give-way record; per-bag release entries are the
recovery path written before maintenance even starts.
"""

from __future__ import annotations

from bagsort.chute.assigner import ChuteAssigner
from bagsort.clock import Clock
from bagsort.errors import ConflictError, OrderingError, ValidationError
from bagsort.ids import IdFactory
from bagsort.model.entities import ChuteAssignment, LoadRecord, StaggerEntry, YieldPlan
from bagsort.model.enums import (
    BagState,
    ConflictPolicy,
    FlightState,
    MaintenanceWindowState,
    StaggerAction,
    StaggerStatus,
)
from bagsort.model.validate import is_open_state
from bagsort.ns.chutes import ChuteRegistry
from bagsort.store.store import Store
from .windows import (
    MaintenanceBoard,
    format_delta,
    parse_instant,
    plus_seconds,
    seconds_between,
)

_HOLD_STATES = (BagState.CHECKED_IN, BagState.INJECTED, BagState.RECHECKED)


class MaintenancePlanner:
    """Computes, commits and recovers the yield/stagger plan for a window."""

    def __init__(
        self,
        store: Store,
        clock: Clock,
        board: MaintenanceBoard,
        chutes: ChuteAssigner,
        registry: ChuteRegistry | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._board = board
        self._chutes = chutes
        self._registry = registry
        self._plan_ids = IdFactory("plan")

    # -- read side ---------------------------------------------------------

    def plan_for(self, window_id: str):
        state = self._store.state()
        window = self._board.get(window_id)
        plan = state.yield_plans.get(window.plan_id) if window.plan_id else None
        if plan is None:
            raise ConflictError(f"maintenance window {window_id!r} has no yield plan yet")
        return plan

    def plans(self) -> list:
        plans = self._store.state().yield_plans
        return [plans[key] for key in sorted(plans)]

    def entries_for(self, window_id: str) -> list[StaggerEntry]:
        return [
            entry
            for entry in self._store.state().stagger_entries
            if entry.window_id == window_id
        ]

    def entries(self) -> list[StaggerEntry]:
        return list(self._store.state().stagger_entries)

    def entry(self, sequence: int) -> StaggerEntry:
        for item in self._store.state().stagger_entries:
            if item.sequence == sequence:
                return item
        raise ConflictError(f"stagger entry {sequence} does not exist")

    def occupying_bags(self, chute_id: str) -> list:
        """Sorted bags physically sitting in the chute (start gate blockers)."""

        return [
            bag
            for bag in self._store.state().bags.values()
            if bag.state is BagState.SORTED and bag.chute_id == chute_id
        ]

    # -- decision ----------------------------------------------------------

    def plan_yield(
        self,
        window_id: str,
        policy: str,
        *,
        reroute_unsorted: bool = True,
        stagger_interval: int = 30,
        stagger_batch: int = 10,
        preferred_alternate: str = "",
    ):
        """Decide who gives way and persist the decision and stagger entries."""

        if stagger_interval < 0 or stagger_batch < 1:
            raise ValidationError("stagger interval must be >= 0 and batch >= 1")
        chosen = ConflictPolicy(policy)
        window = self._board.get(window_id)
        if window.state is not MaintenanceWindowState.REQUESTED:
            raise OrderingError(
                f"maintenance window {window_id!r} is {window.state.value}; plan it first"
            )
        if window.plan_id:
            raise ConflictError(f"maintenance window {window_id!r} already has plan {window.plan_id}")

        affected_flights = self._affected_flights(window.chute_id)
        unsorted, sorted_here = self._partition_bags(window.chute_id, affected_flights)

        plan_id = self._plan_ids.next()
        at = self._clock.now_iso()

        if chosen is ConflictPolicy.WAIT_SCHEDULE:
            plan, entries = self._wait_plan(
                plan_id, window, affected_flights, sorted_here, unsorted, at
            )
        else:
            next_sequence = self._next_sequence()
            plan, entries = self._yield_plan(
                plan_id,
                window,
                affected_flights,
                unsorted,
                sorted_here,
                at,
                next_sequence,
                reroute_unsorted=reroute_unsorted,
                stagger_interval=stagger_interval,
                stagger_batch=stagger_batch,
                preferred_alternate=preferred_alternate,
            )

        planned = window.evolved(plan_id=plan.id)
        with self._store.session() as session:
            session.state.yield_plans[plan.id] = plan
            for entry in entries:
                session.state.stagger_entries.append(entry)
                session.record("stagger.put", {"entry": entry.as_dict()}, at)
            session.state.maintenance_windows[window.id] = planned
            session.record("maintenance.put", {"window": planned.as_dict()}, at)
            session.record("yieldplan.put", {"plan": plan.as_dict()}, at)
        return plan

    def _wait_plan(self, plan_id, window, flights, sorted_here, unsorted, at):
        delay, effective_start, cutoff_note = self._work_finish(window, flights)
        tradeoff = (
            "policy=wait_schedule: maintenance waits for the running schedule to finish; "
            f"maintenance start given up by {format_delta(delay)} to {effective_start}; "
            f"{len(unsorted)} unsorted bags keep their chute and {len(sorted_here)} sorted "
            "bags keep flight time integrity; nothing is held or rerouted; "
            f"cost surrendered: maintenance duration. {cutoff_note}".strip()
        )
        plan = YieldPlan(
            id=plan_id,
            window_id=window.id,
            chute_id=window.chute_id,
            policy=ConflictPolicy.WAIT_SCHEDULE,
            decided_at=at,
            affected_flights=tuple(flights),
            maintenance_delay=delay,
            effective_start_at=effective_start,
            resume_at=window.end_at,
            tradeoff=tradeoff,
        )
        return plan, []

    def _yield_plan(
        self,
        plan_id,
        window,
        flights,
        unsorted,
        sorted_here,
        at,
        next_sequence,
        *,
        reroute_unsorted,
        stagger_interval,
        stagger_batch,
        preferred_alternate,
    ):
        rerouted_flights: dict[str, str] = {}
        if reroute_unsorted:
            for flight_id in flights:
                flight = self._store.state().flights[flight_id]
                if not is_open_state(flight.state):
                    continue
                alternate = self._alternate_chute(window.chute_id, flight, preferred_alternate)
                if alternate:
                    rerouted_flights[flight_id] = alternate

        entries: list[StaggerEntry] = []
        held_index = 0
        rerouted_bags = 0
        for bag in self._ordered(unsorted):
            if bag.flight_id in rerouted_flights:
                entries.append(
                    self._entry(
                        next_sequence,
                        window.id,
                        bag,
                        StaggerAction.REROUTE,
                        from_chute=window.chute_id,
                        to_chute=rerouted_flights[bag.flight_id],
                        planned_at=window.start_at,
                    )
                )
                next_sequence += 1
                rerouted_bags += 1
            else:
                slot = plus_seconds(
                    window.end_at,
                    stagger_interval * (held_index // max(1, stagger_batch)),
                )
                entries.append(
                    self._entry(
                        next_sequence,
                        window.id,
                        bag,
                        StaggerAction.HOLD,
                        from_chute=window.chute_id,
                        planned_at=window.start_at,
                    )
                )
                next_sequence += 1
                entries.append(
                    self._entry(
                        next_sequence,
                        window.id,
                        bag,
                        StaggerAction.RELEASE,
                        from_chute=window.chute_id,
                        planned_at=slot,
                    )
                )
                next_sequence += 1
                held_index += 1
        held_bags = held_index
        for bag in self._ordered(sorted_here):
            entries.append(
                self._entry(
                    next_sequence,
                    window.id,
                    bag,
                    StaggerAction.CLEAR,
                    from_chute=window.chute_id,
                    planned_at=window.start_at,
                )
            )
            next_sequence += 1

        duration = seconds_between(window.start_at, window.end_at)
        at_risk = self._cutoff_risks(flights, window.end_at)
        tradeoff = (
            "policy=yield_to_maintenance: sorting pauses and surrenders the chute at "
            f"{window.start_at}; {held_bags} bags held, {rerouted_bags} bags rerouted, "
            f"{len(sorted_here)} sorted bags cleared; schedule gives up to "
            f"{format_delta(duration)} of time with resume from {window.end_at}; "
            f"cut-off risk: {at_risk}; cost surrendered: flight time integrity; "
            "maintenance keeps its full duration."
        )
        plan = YieldPlan(
            id=plan_id,
            window_id=window.id,
            chute_id=window.chute_id,
            policy=ConflictPolicy.YIELD_TO_MAINTENANCE,
            decided_at=at,
            affected_flights=tuple(flights),
            held_bags=held_bags,
            rerouted_bags=rerouted_bags,
            clear_bags=len(sorted_here),
            schedule_delay=duration,
            effective_start_at=window.start_at,
            resume_at=window.end_at,
            tradeoff=tradeoff,
        )
        return plan, entries

    # -- execution: drain, start, complete, cancel -------------------------

    def clear_entry(self, sequence: int) -> StaggerEntry:
        """Move one sorted bag off the chute: load it, or divert if closed."""

        entry = self.entry(sequence)
        if entry.action is not StaggerAction.CLEAR:
            raise OrderingError(f"stagger entry {sequence} is {entry.action.value}, not clear")
        if entry.status is StaggerStatus.DONE:
            return entry
        at = self._clock.now_iso()
        with self._store.session() as session:
            done = self._apply_clear(session, entry, at)
            session.state.stagger_entries = self._replace_entry(session, done)
            session.record("stagger.put", {"entry": done.as_dict()}, at)
        return self.entry(sequence)

    def clear_chute(self, window_id: str) -> int:
        """Execute every pending CLEAR entry so the crew never meets live bags."""

        cleared = 0
        for entry in self.entries_for(window_id):
            if entry.action is StaggerAction.CLEAR and entry.status is StaggerStatus.PLANNED:
                self.clear_entry(entry.sequence)
                cleared += 1
        return cleared

    def start_maintenance(self, window_id: str):
        """Open the window once the chute is empty; apply hold/reroute entries."""

        window = self._board.get(window_id)
        if window.state is MaintenanceWindowState.ACTIVE:
            return window
        if window.state is not MaintenanceWindowState.REQUESTED:
            raise OrderingError(
                f"maintenance window {window_id!r} is {window.state.value} and cannot start"
            )
        blockers = self.occupying_bags(window.chute_id)
        if blockers:
            raise ConflictError(
                f"chute {window.chute_id!r} still holds {len(blockers)} sorted bag(s); "
                "clear the stagger CLEAR entries before starting maintenance"
            )
        pending_clear = [
            entry
            for entry in self.entries_for(window_id)
            if entry.action is StaggerAction.CLEAR and entry.status is StaggerStatus.PLANNED
        ]
        if pending_clear:
            raise ConflictError(
                f"{len(pending_clear)} CLEAR stagger entries are not executed; "
                "the chute is not proven empty"
            )

        at = self._clock.now_iso()
        active = window.evolved(state=MaintenanceWindowState.ACTIVE, opened_at=at)
        entries = self.entries_for(window_id)
        with self._store.session() as session:
            for entry in entries:
                if entry.status is StaggerStatus.DONE:
                    continue
                if entry.action is StaggerAction.HOLD:
                    self._apply_hold(session, entry, at)
                    done = entry.evolved(status=StaggerStatus.DONE, done_at=at)
                    session.state.stagger_entries = self._replace_entry(session, done)
                    session.record("stagger.put", {"entry": done.as_dict()}, at)
                elif entry.action is StaggerAction.REROUTE:
                    self._apply_reroute(session, entry)
                    done = entry.evolved(status=StaggerStatus.DONE, done_at=at)
                    session.state.stagger_entries = self._replace_entry(session, done)
                    session.record("stagger.put", {"entry": done.as_dict()}, at)
            session.state.maintenance_windows[window.id] = active
            session.record("maintenance.put", {"window": active.as_dict()}, at)
        return active

    def complete_maintenance(self, window_id: str):
        return self._board.complete(window_id)

    def cancel_window(self, window_id: str) -> dict[str, object]:
        """Cancel a not-started window and release every bag it was holding."""

        window = self._board.get(window_id)
        if window.state is not MaintenanceWindowState.REQUESTED:
            raise OrderingError(
                f"maintenance window {window_id!r} is {window.state.value}; only requested "
                "windows can be cancelled"
            )
        at = self._clock.now_iso()
        cancelled = window.evolved(
            state=MaintenanceWindowState.CANCELLED, completed_at=at
        )
        released = 0
        with self._store.session() as session:
            for entry in self.entries_for(window_id):
                if entry.action is StaggerAction.HOLD and entry.status is StaggerStatus.PLANNED:
                    bag = session.state.bags.get(entry.bag_id)
                    if bag is not None and bag.state is BagState.HELD:
                        updated = bag.evolved(state=BagState.INJECTED)
                        session.state.bags[bag.id] = updated
                        session.record("bag.put", {"bag": updated.as_dict()}, at)
                        released += 1
            session.state.maintenance_windows[window.id] = cancelled
            session.record("maintenance.put", {"window": cancelled.as_dict()}, at)
        return {"window": cancelled, "released": released}

    # -- recovery ----------------------------------------------------------

    def resume_sorting(self, window_id: str, *, as_of: str = "") -> dict[str, object]:
        """Restore rerouted flights and release held bags whose slot is due.

        The RELEASE entries were persisted when the yield plan was decided, so
        the recovery path exists before maintenance starts. Every entry whose
        slot is not later than ``as_of`` executes now; calling again as the
        clock advances releases the next wave, which is how bags come back in
        peaks rather than all at once.
        """

        window = self._board.get(window_id)
        if window.state is not MaintenanceWindowState.COMPLETED:
            raise OrderingError(
                f"maintenance window {window_id!r} is {window.state.value}; complete it "
                "before resuming sorting"
            )
        moment = as_of or self._clock.now_iso()
        parse_instant(moment)
        if parse_instant(moment) < parse_instant(window.end_at):
            moment = window.end_at

        at = self._clock.now_iso()
        self._restore_reroutes(window, at)

        released_bags: list[str] = []
        with self._store.session() as session:
            due = [
                entry
                for entry in session.state.stagger_entries
                if entry.window_id == window_id
                and entry.action is StaggerAction.RELEASE
                and entry.status is StaggerStatus.PLANNED
                and parse_instant(entry.planned_at) <= parse_instant(moment)
            ]
            for entry in due:
                bag = session.state.bags.get(entry.bag_id)
                if bag is None or bag.state is not BagState.HELD:
                    done = entry.evolved(status=StaggerStatus.DONE, done_at=at)
                else:
                    updated = bag.evolved(state=BagState.INJECTED)
                    session.state.bags[bag.id] = updated
                    session.record("bag.put", {"bag": updated.as_dict()}, at)
                    done = entry.evolved(status=StaggerStatus.DONE, done_at=at)
                    released_bags.append(bag.id)
                session.state.stagger_entries = self._replace_entry(session, done)
                session.record("stagger.put", {"entry": done.as_dict()}, at)
        return {
            "window_id": window_id,
            "as_of": moment,
            "released": len(released_bags),
            "released_bags": released_bags,
            "still_deferred": len(
                [
                    entry
                    for entry in self.entries_for(window_id)
                    if entry.action is StaggerAction.RELEASE
                    and entry.status is StaggerStatus.PLANNED
                ]
            ),
        }

    # -- helpers -----------------------------------------------------------

    def _affected_flights(self, chute_id: str) -> list[str]:
        state = self._store.state()
        flights = []
        for flight_id, assignment in state.assignments.items():
            flight = state.flights.get(flight_id)
            if flight is None or flight.state is FlightState.CLOSED:
                continue
            if assignment.chute_id == chute_id:
                flights.append(flight_id)
        return sorted(flights)

    def _partition_bags(self, chute_id: str, flights: list[str]):
        routed = set(flights)
        unsorted, sorted_here = [], []
        for bag in self._store.state().bags.values():
            if bag.state is BagState.SORTED and bag.chute_id == chute_id:
                sorted_here.append(bag)
            elif bag.state in _HOLD_STATES and bag.flight_id in routed:
                unsorted.append(bag)
        return unsorted, sorted_here

    def _ordered(self, bags) -> list:
        state = self._store.state()

        def cutoff_rank(bag):
            flight = state.flights.get(bag.flight_id)
            cutoff = flight.cutoff_at if flight else ""
            return (cutoff or "9999", bag.epoch, bag.sequence, bag.id)

        return sorted(bags, key=cutoff_rank)

    def _entry(self, sequence, window_id, bag, action, *, from_chute, to_chute="", planned_at):
        return StaggerEntry(
            sequence=sequence,
            window_id=window_id,
            bag_id=bag.id,
            flight_id=bag.flight_id,
            action=action,
            from_chute=from_chute,
            to_chute=to_chute,
            planned_at=planned_at,
        )

    def _next_sequence(self) -> int:
        entries = self._store.state().stagger_entries
        return (max((entry.sequence for entry in entries), default=0)) + 1

    def _work_finish(self, window, flights):
        """Estimate when the live schedule finishes: latest open flight cut-off."""

        cutoffs = []
        state = self._store.state()
        for flight_id in flights:
            cutoff = state.flights[flight_id].cutoff_at
            if cutoff:
                cutoffs.append(cutoff)
        if not cutoffs:
            return 0, window.start_at, "no flight cut-off on record; start stays gated by chute emptiness"
        latest = max(cutoffs)
        if parse_instant(latest) <= parse_instant(window.start_at):
            return 0, window.start_at, ""
        delay = seconds_between(window.start_at, latest)
        return delay, latest, f"schedule estimated finished by {latest}"

    def _cutoff_risks(self, flights, resume_at) -> str:
        risks = []
        state = self._store.state()
        for flight_id in flights:
            cutoff = state.flights[flight_id].cutoff_at
            if cutoff and parse_instant(cutoff) < parse_instant(resume_at):
                risks.append(f"{flight_id}@{cutoff}")
        return ", ".join(risks) if risks else "none"

    def _alternate_chute(self, blocked_chute: str, flight, preferred: str) -> str:
        if preferred:
            if preferred == blocked_chute:
                return ""
            if self._registry is not None and self._registry.lanes:
                lane = self._registry.lanes.get(preferred)
                if lane is None or not lane.enabled:
                    return ""
                if self._board.is_chute_blocked(preferred):
                    return ""
            return preferred
        if self._registry is None or not self._registry.lanes:
            return ""
        candidates = []
        for lane in self._registry.lanes.values():
            if not lane.enabled or lane.chute_id == blocked_chute:
                continue
            if self._board.is_chute_blocked(lane.chute_id):
                continue
            candidates.append(lane)
        if not candidates:
            return ""
        same_carousel = [lane for lane in candidates if lane.carousel_id == flight.carousel]
        pool = same_carousel or candidates
        return sorted(lane.chute_id for lane in pool)[0]

    def _replace_entry(self, session, entry: StaggerEntry) -> list[StaggerEntry]:
        return sorted(
            [item for item in session.state.stagger_entries if item.sequence != entry.sequence]
            + [entry],
            key=lambda item: item.sequence,
        )

    def _apply_clear(self, session, entry: StaggerEntry, at: str) -> StaggerEntry:
        bag = session.state.bags.get(entry.bag_id)
        if bag is None:
            raise ConflictError(f"bag {entry.bag_id!r} vanished before clearing")
        if bag.state is not BagState.SORTED:
            return entry.evolved(status=StaggerStatus.DONE, done_at=at)
        flight = session.state.flights.get(entry.flight_id)
        open_flight = flight is not None and is_open_state(flight.state)
        if open_flight:
            updated = bag.evolved(state=BagState.LOADED)
            record = LoadRecord(
                bag_id=bag.id,
                carousel=flight.carousel,
                batch=flight.batch,
                at=at,
            )
            session.state.bags[bag.id] = updated
            session.state.load_records.append(record)
            session.record("bag.put", {"bag": updated.as_dict()}, at)
            session.record("load.append", {"record": record.as_dict()}, at)
        else:
            updated = bag.evolved(state=BagState.DIVERTED)
            session.state.bags[bag.id] = updated
            session.record("bag.put", {"bag": updated.as_dict()}, at)
        return entry.evolved(status=StaggerStatus.DONE, done_at=at)

    def _apply_hold(self, session, entry: StaggerEntry, at: str) -> None:
        bag = session.state.bags.get(entry.bag_id)
        if bag is None:
            raise ConflictError(f"bag {entry.bag_id!r} vanished before holding")
        if bag.state in _HOLD_STATES:
            held = bag.evolved(state=BagState.HELD)
            session.state.bags[bag.id] = held
            session.record("bag.put", {"bag": held.as_dict()}, at)

    def _apply_reroute(self, session, entry: StaggerEntry) -> None:
        assignment = session.state.assignments.get(entry.flight_id)
        if assignment is None or assignment.chute_id != entry.to_chute:
            generation = (assignment.generation + 1) if assignment else 1
            updated_assignment = ChuteAssignment(
                flight_id=entry.flight_id,
                chute_id=entry.to_chute,
                generation=generation,
                updated_at=self._clock.now_iso(),
            )
            session.state.assignments[entry.flight_id] = updated_assignment
            session.record("chute.assign", updated_assignment.as_dict(), self._clock.now_iso())

    def _restore_reroutes(self, window, at: str) -> None:
        groups: dict[str, str] = {}
        for entry in self.entries_for(window.id):
            if entry.action is StaggerAction.REROUTE:
                groups[entry.flight_id] = entry.from_chute
        for flight_id, original in sorted(groups.items()):
            assignment = self._store.state().assignments.get(flight_id)
            if assignment is not None and assignment.chute_id != original:
                self._chutes.assign(flight_id, original)
