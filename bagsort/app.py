"""Assembly of the handling line.

Every subsystem is constructed here and every operator visible action is a
method on :class:`Plant`, which is what keeps the console handlers thin and
lets the CLI drive the same operations the HTTP API exposes.
"""

from __future__ import annotations

from dataclasses import dataclass

from bagsort.audit.chain import AuditChain
from bagsort.belt.belt import Belt
from bagsort.checkin.desk import Desk
from bagsort.clock import Clock
from bagsort.errors import ConflictError
from bagsort.events.bus import EventBus
from bagsort.flight.book import FlightBook
from bagsort.flight.close import FlightCloser
from bagsort.flight.mapping import BagMapping
from bagsort.flight.schedule import ScheduleBoard
from bagsort.ids import IdFactory
from bagsort.inject.injector import Injector
from bagsort.maintenance.planner import MaintenancePlanner
from bagsort.maintenance.windows import MaintenanceBoard
from bagsort.model.entities import Bag, Flight, Reading, SortRecord
from bagsort.model.errors import unknown_bag
from bagsort.ns.chutes import ChuteRegistry
from bagsort.ns.namespace import Namespace
from bagsort.ns.topology import handling_line
from bagsort.quota.quota import QuotaBook
from bagsort.recheck.queue import RecheckQueue
from bagsort.reporting.report import PlantReport
from bagsort.sequence.verdict import VerdictTracker
from bagsort.sorter.sorter import Sorter
from bagsort.store.store import Store
from bagsort.tag.history import ReadHistory
from bagsort.tag.reader import TagReader
from bagsort.chute.assigner import ChuteAssigner
from bagsort.chute.history import AssignmentHistory
from bagsort.console.metrics import Metrics
from bagsort.console.telemetry import Telemetry

DEFAULT_TERMINAL = "T1"


@dataclass
class Plant:
    """The wired plant plus the operator level operations the console calls."""

    store: Store
    clock: Clock
    namespace: Namespace
    registry: ChuteRegistry
    bus: EventBus
    telemetry: Telemetry
    metrics: Metrics
    reader: TagReader
    reads: ReadHistory
    book: FlightBook
    mapping: BagMapping
    schedule: ScheduleBoard
    closer: FlightCloser
    chutes: ChuteAssigner
    chute_history: AssignmentHistory
    sorter: Sorter
    injector: Injector
    desk: Desk
    recheck: RecheckQueue
    belt: Belt
    quota: QuotaBook
    maintenance: MaintenanceBoard
    planner: MaintenancePlanner
    audit: AuditChain
    report: PlantReport

    @property
    def topology(self):
        return handling_line()

    # -- construction ------------------------------------------------------

    @classmethod
    def build(
        cls,
        store: Store,
        terminal: str = DEFAULT_TERMINAL,
        clock: Clock | None = None,
    ) -> "Plant":
        clock = clock or Clock()
        namespace = Namespace(terminal)
        registry = ChuteRegistry()
        bus = EventBus()
        telemetry = Telemetry()
        metrics = Metrics()
        bus.subscribe("*", lambda event: telemetry.observe(event.topic))

        reader = TagReader(store, namespace, clock)
        reads = ReadHistory(store)
        book = FlightBook(store, clock)
        mapping = BagMapping(store, clock)
        schedule = ScheduleBoard(store, clock)
        chutes = ChuteAssigner(store, clock)
        chute_history = AssignmentHistory(store)
        sorter = Sorter(store, mapping, chutes, clock, VerdictTracker())
        injector = Injector(store, book, sorter, clock)
        quota = QuotaBook(store, clock)
        desk = Desk(store, reader, book, mapping, injector, quota, clock, handling_line())
        closer = FlightCloser(book)
        recheck = RecheckQueue(store, book, clock)
        belt = Belt(store, clock, IdFactory("cmd"))
        maintenance = MaintenanceBoard(store, clock)
        planner = MaintenancePlanner(store, clock, maintenance, chutes, registry)
        gate = _MaintenanceGate(maintenance, store)
        sorter.set_gate(maintenance)
        desk.set_gate(gate)

        audit = AuditChain(store, clock, handling_line())
        audit_subscriber = _AuditSubscriber(audit)
        for topic in (
            "bag.checked_in",
            "bag.sorted",
            "bag.diverted",
            "bag.recheck_enqueued",
            "flight.closed",
            "chute.assigned",
            "belt.switched",
            "bag.remapped",
            "maintenance.registered",
            "maintenance.started",
            "maintenance.completed",
            "maintenance.cancelled",
            "maintenance.yield_planned",
            "maintenance.stagger_executed",
            "maintenance.sorting_resumed",
        ):
            bus.subscribe(topic, audit_subscriber)

        report = PlantReport(
            store, book, chutes, belt, quota, audit, namespace, handling_line()
        )
        return cls(
            store=store,
            clock=clock,
            namespace=namespace,
            registry=registry,
            bus=bus,
            telemetry=telemetry,
            metrics=metrics,
            reader=reader,
            reads=reads,
            book=book,
            mapping=mapping,
            schedule=schedule,
            closer=closer,
            chutes=chutes,
            chute_history=chute_history,
            sorter=sorter,
            injector=injector,
            desk=desk,
            recheck=recheck,
            belt=belt,
            quota=quota,
            maintenance=maintenance,
            planner=planner,
            audit=audit,
            report=report,
        )

    # -- operator operations ----------------------------------------------

    def register_flight(self, code: str, carousel: str = "", chute: str = "") -> Flight:
        flight = Flight(
            id=self.namespace.flight_id(code),
            code=code.strip().upper(),
            terminal=self.namespace.terminal,
            carousel=self.namespace.carousel_id(carousel) if carousel else "",
        )
        self.book.register(flight)
        if chute:
            self.assign_chute(flight.id, self.namespace.chute_id(chute))
        return self.book.get(flight.id)

    def assign_chute(self, flight_id: str, chute_id: str):
        assignment = self.chutes.assign(flight_id, chute_id)
        self.bus.publish("chute.assigned", flight_id=flight_id, chute_id=chute_id)
        return assignment

    def read_barcode(self, barcode: str) -> Reading:
        return self.reader.read(barcode)

    def check_in(self, barcode: str, flight_id: str) -> Bag:
        bag = self.desk.check_in(barcode, flight_id)
        self.bus.publish("bag.checked_in", bag_id=bag.id, flight_id=flight_id)
        return bag

    def cancel_check_in(self, bag_id: str) -> Bag:
        bag = self.desk.cancel(bag_id)
        self.bus.publish("bag.diverted", bag_id=bag.id, reason="checkin_cancelled")
        return bag

    def bag(self, bag_id: str) -> Bag:
        bag = self.store.state().bags.get(bag_id)
        if bag is None:
            raise unknown_bag(bag_id)
        return bag

    def push_bag(self, bag_id: str, chute_id: str) -> SortRecord:
        bag = self.bag(bag_id)
        record = self.injector.push(bag, chute_id)
        self.bus.publish("bag.sorted", bag_id=bag.id, chute_id=record.chute_id)
        return record

    def retry_bag(self, bag_id: str) -> bool:
        bag = self.bag(bag_id)
        sorted_now = self.injector.retry(bag)
        if sorted_now:
            self.bus.publish("bag.sorted", bag_id=bag.id, retry=True)
        return sorted_now

    def divert_bag(self, bag_id: str) -> Bag:
        bag = self.bag(bag_id)
        updated = self.sorter.divert(bag)
        self.bus.publish("bag.diverted", bag_id=bag.id)
        return updated

    def remap_bag(self, bag_id: str, flight_id: str) -> Bag:
        self.book.require_open(flight_id)
        self.mapping.remap(bag_id, flight_id)
        self.bus.publish("bag.remapped", bag_id=bag_id, flight_id=flight_id)
        return self.bag(bag_id)

    def revise_schedule(self, flight_id: str, cutoff_at: str):
        return self.schedule.revise(flight_id, cutoff_at)

    def close_flight(self, flight_id: str) -> Flight:
        flight = self.closer.close(flight_id, self.injector)
        self.bus.publish("flight.closed", flight_id=flight_id)
        return flight

    def enqueue_recheck(self, bag_id: str) -> Bag:
        bag = self.bag(bag_id)
        self.recheck.enqueue(bag)
        self.bus.publish("bag.recheck_enqueued", bag_id=bag_id)
        return bag

    def process_recheck(self) -> Bag | None:
        return self.recheck.dequeue()

    def switch_belt(self, carousel: str, batch: int) -> int:
        cleared = self.belt.switch(carousel, batch)
        self.bus.publish("belt.switched", carousel=carousel, batch=batch)
        return cleared

    def rotate_belt(self) -> int:
        return self.belt.rotate()

    def append_transfer(self, flight_id: str, carousel: str, batch: int):
        return self.belt.append_transfer(flight_id, carousel, batch)

    def set_quota(self, flight_id: str, limit: int) -> int:
        return self.quota.set_limit(flight_id, limit)

    def verify(self) -> bool:
        return self.audit.verify()

    # -- maintenance windows ----------------------------------------------

    def register_maintenance(
        self,
        window_id: str,
        chute_id: str,
        start_at: str,
        end_at: str,
        ticket: str = "",
    ):
        window = self.maintenance.register(window_id, chute_id, start_at, end_at, ticket)
        self.bus.publish(
            "maintenance.registered",
            window_id=window.id,
            chute_id=window.chute_id,
            start_at=window.start_at,
            end_at=window.end_at,
        )
        return window

    def plan_maintenance_yield(self, window_id: str, policy: str, **options):
        plan = self.planner.plan_yield(window_id, policy, **options)
        self.bus.publish(
            "maintenance.yield_planned",
            window_id=window_id,
            plan_id=plan.id,
            policy=plan.policy.value,
        )
        return plan

    def clear_stagger_entry(self, sequence: int):
        entry = self.planner.clear_entry(sequence)
        self.bus.publish(
            "maintenance.stagger_executed",
            window_id=entry.window_id,
            bag_id=entry.bag_id,
            action=entry.action.value,
            sequence=entry.sequence,
        )
        return entry

    def clear_maintenance_chute(self, window_id: str) -> int:
        cleared = self.planner.clear_chute(window_id)
        self.bus.publish(
            "maintenance.stagger_executed",
            window_id=window_id,
            action="clear_batch",
            cleared=cleared,
        )
        return cleared

    def start_maintenance(self, window_id: str):
        window = self.planner.start_maintenance(window_id)
        self.bus.publish(
            "maintenance.started",
            window_id=window.id,
            chute_id=window.chute_id,
        )
        return window

    def complete_maintenance(self, window_id: str):
        window = self.planner.complete_maintenance(window_id)
        self.bus.publish(
            "maintenance.completed",
            window_id=window.id,
            chute_id=window.chute_id,
        )
        return window

    def cancel_maintenance(self, window_id: str):
        result = self.planner.cancel_window(window_id)
        self.bus.publish(
            "maintenance.cancelled",
            window_id=window_id,
            released=result["released"],
        )
        return result

    def resume_sorting(self, window_id: str, as_of: str = ""):
        result = self.planner.resume_sorting(window_id, as_of=as_of)
        self.bus.publish(
            "maintenance.sorting_resumed",
            window_id=window_id,
            released=result["released"],
        )
        return result


class _MaintenanceGate:
    """Refuses new check-ins for flights routed to an active maintenance chute."""

    def __init__(self, board: MaintenanceBoard, store: Store) -> None:
        self._board = board
        self._store = store

    def refuse_checkin_for_blocked_flight(self, flight_id: str) -> None:
        assignment = self._store.state().assignments.get(flight_id)
        if assignment is None:
            return
        if self._board.is_chute_blocked(assignment.chute_id):
            raise ConflictError(
                f"chute {assignment.chute_id!r} is under active maintenance; "
                f"flight {flight_id!r} is not accepting new bags"
            )


class _AuditSubscriber:
    """Turns published operations into linked audit entries."""

    _STAGE = {
        "bag.checked_in": ("checkin", "checkin"),
        "bag.sorted": ("sort", "sorter"),
        "bag.diverted": ("diversion", "sorter"),
        "bag.recheck_enqueued": ("recheck", "recheck"),
        "flight.closed": ("flight_close", "audit"),
        "chute.assigned": ("chute_assign", "chute"),
        "belt.switched": ("belt_switch", "belt"),
        "bag.remapped": ("remap", "checkin"),
        "maintenance.registered": ("maintenance_register", "chute"),
        "maintenance.started": ("maintenance_start", "chute"),
        "maintenance.completed": ("maintenance_complete", "chute"),
        "maintenance.cancelled": ("maintenance_cancel", "chute"),
        "maintenance.yield_planned": ("maintenance_yield_plan", "sorter"),
        "maintenance.stagger_executed": ("maintenance_stagger", "sorter"),
        "maintenance.sorting_resumed": ("maintenance_resume", "sorter"),
    }

    def __init__(self, audit: AuditChain) -> None:
        self._audit = audit

    def __call__(self, event) -> None:
        mapping = self._STAGE.get(event.topic)
        if mapping is None:
            raise ConflictError(f"no audit mapping for topic {event.topic!r}")
        kind, stage = mapping
        subject = str(
            event.payload.get("window_id")
            or event.payload.get("bag_id")
            or event.payload.get("flight_id")
            or event.payload.get("carousel")
            or ""
        )
        detail = {
            key: value
            for key, value in event.payload.items()
            if key not in {"bag_id", "flight_id", "window_id"}
        }
        self._audit.record(kind, subject, stage, detail)
