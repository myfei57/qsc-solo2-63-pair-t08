"""Assembly of the handling line.

Every subsystem is constructed here and every operator visible action is a
method on :class:`Plant`, which is what keeps the console handlers thin and
lets the CLI drive the same operations the HTTP API exposes.
"""

from __future__ import annotations

import os
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
from bagsort.ids import IdFactory
from bagsort.maintenance.ledger import ArtifactLedger
from bagsort.maintenance.planner import MaintenancePlanner
from bagsort.maintenance.scheduler import Scheduler
from bagsort.model.entities import Bag, Flight, Reading, SortRecord
from bagsort.model.enums import MaintenanceState, YieldPolicy

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
    audit: AuditChain
    report: PlantReport
    scheduler: Scheduler
    planner: MaintenancePlanner
    ledger: ArtifactLedger

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

        ledger_root = os.path.join(os.path.dirname(store.path), "maintenance")
        ledger = ArtifactLedger(ledger_root)
        scheduler = Scheduler(store, clock)
        planner = MaintenancePlanner(
            store, clock, scheduler, chutes, registry, belt, ledger
        )

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
            "sortjob.scheduled",
            "maintenance.declared",
            "maintenance.resolved",
            "maintenance.armed",
            "maintenance.started",
            "maintenance.finished",
            "maintenance.cancelled",
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
            audit=audit,
            report=report,
            scheduler=scheduler,
            planner=planner,
            ledger=ledger,
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
        self._require_chute_sorting(chute_id)
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

    # -- sorting schedule ---------------------------------------------------

    def _require_chute_sorting(self, chute_id: str) -> None:
        """Reject dispatch into an armed/in-progress maintenance window."""

        from bagsort.maintenance.windows import overlaps

        for request in self.planner.list_maintenance(chute_id):
            if request.state is MaintenanceState.IN_PROGRESS:
                raise ConflictError(
                    f"chute {chute_id} is under maintenance {request.id!r}"
                )
            if request.state in (
                MaintenanceState.APPROVED,
                MaintenanceState.ARMED,
            ) and overlaps(
                (request.start_at, request.end_at),
                (self.clock.now_iso(), self.clock.now_iso()),
            ):
                raise ConflictError(
                    f"chute {chute_id} has an approved maintenance window "
                    f"{request.id!r}; sorting must yield"
                )

    def schedule_sort(
        self,
        job_id: str,
        chute_id: str,
        start_at: str,
        end_at: str,
        note: str = "",
    ):
        job = self.scheduler.schedule(job_id, chute_id, start_at, end_at, note=note)
        self.bus.publish("sortjob.scheduled", job_id=job.id, chute_id=job.chute_id)
        return job

    def start_sort(self, job_id: str):
        return self.scheduler.start(job_id)

    def pause_sort(self, job_id: str):
        return self.scheduler.pause(job_id)

    def resume_sort(self, job_id: str):
        return self.scheduler.resume(job_id)

    def complete_sort(self, job_id: str):
        return self.scheduler.complete(job_id)

    def cancel_sort(self, job_id: str):
        return self.scheduler.cancel(job_id)

    def list_sort_jobs(self, chute_id: str = ""):
        return self.scheduler.list_jobs(chute_id)

    # -- maintenance --------------------------------------------------------

    def declare_maintenance(
        self, chute_id: str, start_at: str, end_at: str, reason: str = ""
    ):
        preview = self.planner.declare(chute_id, start_at, end_at, reason)
        self.bus.publish(
            "maintenance.declared",
            maintenance_id=preview.request.id,
            chute_id=chute_id,
        )
        return preview

    def resolve_maintenance(self, maintenance_id: str, policy: str):
        ruling = YieldPolicy(policy)
        decision = self.planner.resolve(maintenance_id, ruling)
        self.bus.publish(
            "maintenance.resolved",
            maintenance_id=maintenance_id,
            policy=ruling.value,
        )
        return decision

    def arm_maintenance(self, maintenance_id: str):
        result = self.planner.arm(maintenance_id)
        self.bus.publish(
            "maintenance.armed",
            maintenance_id=maintenance_id,
            clear=result.barrier.clear,
        )
        return result

    def start_maintenance(self, maintenance_id: str):
        request = self.planner.start(maintenance_id)
        self.bus.publish(
            "maintenance.started", maintenance_id=maintenance_id
        )
        return request

    def complete_maintenance(self, maintenance_id: str):
        recovery = self.planner.complete(maintenance_id)
        self.bus.publish(
            "maintenance.finished", maintenance_id=maintenance_id
        )
        return recovery

    def cancel_maintenance(self, maintenance_id: str):
        recovery = self.planner.cancel(maintenance_id)
        self.bus.publish(
            "maintenance.cancelled", maintenance_id=maintenance_id
        )
        return recovery

    def list_maintenance(self, chute_id: str = "", state: MaintenanceState | None = None):
        return self.planner.list_maintenance(chute_id, state)

    def maintenance_shifts(self, maintenance_id: str = ""):
        return self.planner.shifts(maintenance_id)

    def verify(self) -> bool:
        return self.audit.verify()


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
        "sortjob.scheduled": ("sort_schedule", "sorter"),
        "maintenance.declared": ("maintenance_declare", "sorter"),
        "maintenance.resolved": ("maintenance_resolve", "sorter"),
        "maintenance.armed": ("maintenance_arm", "chute"),
        "maintenance.started": ("maintenance_start", "chute"),
        "maintenance.finished": ("maintenance_finish", "chute"),
        "maintenance.cancelled": ("maintenance_cancel", "chute"),
    }

    def __init__(self, audit: AuditChain) -> None:
        self._audit = audit

    def __call__(self, event) -> None:
        mapping = self._STAGE.get(event.topic)
        if mapping is None:
            raise ConflictError(f"no audit mapping for topic {event.topic!r}")
        kind, stage = mapping
        subject = str(
            event.payload.get("bag_id")
            or event.payload.get("flight_id")
            or event.payload.get("maintenance_id")
            or event.payload.get("job_id")
            or event.payload.get("carousel")
            or ""
        )
        detail = {
            key: value
            for key, value in event.payload.items()
            if key not in {"bag_id", "flight_id"}
        }
        self._audit.record(kind, subject, stage, detail)
