"""Maintenance windows versus live sorting: declaration, yield, stagger, recovery."""

from __future__ import annotations

import pytest

from bagsort.errors import (
    ConflictError,
    NotFoundError,
    OrderingError,
    ValidationError,
)
from bagsort.model.enums import (
    BagState,
    ConflictPolicy,
    MaintenanceWindowState,
    StaggerAction,
    StaggerStatus,
)
from tests.support import check_in, register_flight

CHUTE = "T1-CHA"
OTHER = "T1-CHB"
START = "2024-01-01T01:00:00Z"
END = "2024-01-01T03:00:00Z"


def _flight_with_chute(plant, code="CA1234", chute=CHUTE, carousel="CR1"):
    flight = register_flight(plant, code=code, carousel=carousel, chute="")
    plant.chutes.assign(flight.id, chute)
    return flight


def _bags(plant, flight, total=4, sorted_count=2):
    bags = check_in(plant, flight.id, total)
    for bag in bags[:sorted_count]:
        plant.push_bag(bag.id, CHUTE)
    return bags


def _register(plant, window_id="MW1", chute=CHUTE, start=START, end=END):
    return plant.register_maintenance(window_id, chute, start, end, ticket="T-1")


def _register_lanes(plant):
    plant.registry.register(CHUTE, "T1-CR1")
    plant.registry.register(OTHER, "T1-CR1")


# ---------------------------------------------------------------------------
# declaration and the no-overlap contract
# ---------------------------------------------------------------------------


def test_window_registers_in_requested_state(plant):
    window = _register(plant)
    assert window.state is MaintenanceWindowState.REQUESTED
    assert window.chute_id == CHUTE and window.ticket == "T-1"
    assert plant.maintenance.get("MW1").id == "MW1"


def test_window_rejects_bad_times(plant):
    with pytest.raises(ValidationError):
        plant.register_maintenance("MW1", CHUTE, END, START)
    with pytest.raises(ValidationError):
        plant.register_maintenance("MW1", CHUTE, "not-a-time", END)
    with pytest.raises(ValidationError):
        plant.register_maintenance("MW1", "  ", START, END)


def test_window_ids_are_unique(plant):
    _register(plant)
    with pytest.raises(ConflictError):
        _register(plant)


def test_windows_on_the_same_chute_must_not_overlap(plant):
    _register(plant)
    with pytest.raises(ConflictError):
        plant.register_maintenance(
            "MW2", CHUTE, "2024-01-01T02:00:00Z", "2024-01-01T04:00:00Z"
        )


def test_touching_endpoints_do_not_overlap_and_other_chutes_do_not_conflict(plant):
    _register(plant)
    touching = plant.register_maintenance(
        "MW2", CHUTE, END, "2024-01-01T05:00:00Z"
    )
    assert touching.id == "MW2"
    other_chute = plant.register_maintenance(
        "MW3", OTHER, START, END
    )
    assert other_chute.chute_id == OTHER


def test_get_unknown_window_is_a_not_found(plant):
    with pytest.raises(NotFoundError):
        plant.maintenance.get("GHOST")


# ---------------------------------------------------------------------------
# wait-for-schedule policy
# ---------------------------------------------------------------------------


def test_wait_policy_records_maintenance_delay_and_no_stagger(plant):
    flight = _flight_with_chute(plant)
    plant.revise_schedule(flight.id, "2024-01-01T02:30:00Z")
    _register(plant)
    plan = plant.plan_maintenance_yield("MW1", "wait_schedule")
    assert plan.policy is ConflictPolicy.WAIT_SCHEDULE
    assert plan.maintenance_delay == 5400
    assert plan.effective_start_at == "2024-01-01T02:30:00Z"
    assert plan.affected_flights == (flight.id,)
    assert plan.held_bags == 0 and plan.rerouted_bags == 0 and plan.clear_bags == 0
    assert "maintenance start given up" in plan.tradeoff
    assert plant.planner.entries_for("MW1") == []


def test_wait_policy_does_not_delay_when_schedule_finishes_first(plant):
    _flight_with_chute(plant)
    _register(plant, start="2024-01-01T05:00:00Z", end="2024-01-01T07:00:00Z")
    plant.revise_schedule(plant.book.list()[0].id, "2024-01-01T02:30:00Z")
    plan = plant.plan_maintenance_yield("MW1", "wait_schedule")
    assert plan.maintenance_delay == 0


def test_wait_policy_start_is_gated_until_sorted_bags_are_manually_cleared(plant):
    flight = _flight_with_chute(plant)
    _bags(plant, flight, total=3, sorted_count=2)
    _register(plant)
    plant.plan_maintenance_yield("MW1", "wait_schedule")
    # Sorted bags physically occupy the chute: the crew must not walk into them.
    with pytest.raises(ConflictError):
        plant.start_maintenance("MW1")
    for bag in plant.planner.occupying_bags(CHUTE):
        plant.belt.place(bag)
        plant.switch_belt("T1-CR1", 1)
    window = plant.start_maintenance("MW1")
    assert window.state is MaintenanceWindowState.ACTIVE


# ---------------------------------------------------------------------------
# yield-to-maintenance policy: hold, reroute, clear and the recovery path
# ---------------------------------------------------------------------------


def test_yield_policy_writes_per_bag_hold_clear_and_release_entries(plant):
    flight = _flight_with_chute(plant)
    _bags(plant, flight, total=4, sorted_count=2)
    _register(plant)
    plan = plant.plan_maintenance_yield(
        "MW1", "yield_to_maintenance", reroute_unsorted=False
    )
    assert plan.policy is ConflictPolicy.YIELD_TO_MAINTENANCE
    assert plan.held_bags == 2 and plan.clear_bags == 2 and plan.rerouted_bags == 0
    entries = plant.planner.entries_for("MW1")
    actions = sorted(entry.action for entry in entries)
    assert actions == sorted(
        [StaggerAction.CLEAR, StaggerAction.CLEAR, StaggerAction.HOLD, StaggerAction.HOLD,
         StaggerAction.RELEASE, StaggerAction.RELEASE]
    )
    assert all(entry.status is StaggerStatus.PLANNED for entry in entries)
    assert "schedule gives up" in plan.tradeoff
    assert "flight time integrity" in plan.tradeoff


def test_yield_start_is_refused_until_clear_entries_are_executed(plant):
    flight = _flight_with_chute(plant)
    _bags(plant, flight, total=2, sorted_count=2)
    _register(plant)
    plant.plan_maintenance_yield("MW1", "yield_to_maintenance", reroute_unsorted=False)
    with pytest.raises(ConflictError):
        plant.start_maintenance("MW1")
    cleared = plant.clear_maintenance_chute("MW1")
    assert cleared == 2
    # Open flight bags are loaded off the chute, so the crew finds it empty.
    assert plant.planner.occupying_bags(CHUTE) == []
    window = plant.start_maintenance("MW1")
    assert window.state is MaintenanceWindowState.ACTIVE


def test_clear_loads_open_flight_bags_and_diverts_closed_ones(plant):
    flight = _flight_with_chute(plant)
    bags = _bags(plant, flight, total=2, sorted_count=1)
    _register(plant)
    plant.plan_maintenance_yield("MW1", "yield_to_maintenance", reroute_unsorted=False)
    clear_entry = next(
        e for e in plant.planner.entries_for("MW1") if e.action is StaggerAction.CLEAR
    )
    entry = plant.clear_stagger_entry(clear_entry.sequence)
    assert entry.status is StaggerStatus.DONE and entry.done_at
    assert plant.bag(bags[0].id).state is BagState.LOADED
    # idempotent
    assert plant.clear_stagger_entry(clear_entry.sequence).status is StaggerStatus.DONE


def test_start_holds_unsorted_bags_and_blocks_dispatch(plant):
    flight = _flight_with_chute(plant)
    bags = _bags(plant, flight, total=4, sorted_count=2)
    _register(plant)
    plant.plan_maintenance_yield("MW1", "yield_to_maintenance", reroute_unsorted=False)
    plant.clear_maintenance_chute("MW1")
    plant.start_maintenance("MW1")

    held = [bags[2], bags[3]]
    assert all(plant.bag(b.id).state is BagState.HELD for b in held)
    assert CHUTE in plant.maintenance.blocked_chutes()
    # A held bag cannot be forced through.
    with pytest.raises(ConflictError):
        plant.retry_bag(held[0].id)
    # A brand new bag for another flight routed to the blocked chute is refused.
    with pytest.raises(ConflictError):
        plant.push_bag(bags[0].id, CHUTE)


def test_reroute_moves_the_flight_to_an_enabled_alternate_at_start(plant):
    _register_lanes(plant)
    flight = _flight_with_chute(plant)
    bags = _bags(plant, flight, total=3, sorted_count=1)
    _register(plant)
    plant.plan_maintenance_yield("MW1", "yield_to_maintenance", reroute_unsorted=True)
    plant.clear_maintenance_chute("MW1")
    plant.start_maintenance("MW1")
    assert plant.chutes.require(flight.id).chute_id == OTHER
    reroute = [
        e for e in plant.planner.entries_for("MW1")
        if e.action is StaggerAction.REROUTE
    ]
    assert len(reroute) == 2
    assert all(e.to_chute == OTHER for e in reroute)


def test_resume_releases_held_bags_in_staggered_waves(plant):
    flight = _flight_with_chute(plant)
    bags = _bags(plant, flight, total=4, sorted_count=0)
    _register(plant)
    plant.plan_maintenance_yield(
        "MW1",
        "yield_to_maintenance",
        reroute_unsorted=False,
        stagger_interval=60,
        stagger_batch=2,
    )
    plant.start_maintenance("MW1")
    plant.complete_maintenance("MW1")

    # The persisted recovery path is two release slots: end, end+60.
    releases = [
        e for e in plant.planner.entries_for("MW1") if e.action is StaggerAction.RELEASE
    ]
    slots = sorted({e.planned_at for e in releases})
    assert slots == ["2024-01-01T03:00:00Z", "2024-01-01T03:01:00Z"]

    first = plant.resume_sorting("MW1", as_of="2024-01-01T03:00:00Z")
    assert first["released"] == 2 and first["still_deferred"] == 2
    assert all(plant.bag(b.id).state is BagState.INJECTED for b in bags[:2])
    assert all(plant.bag(b.id).state is BagState.HELD for b in bags[2:])

    second = plant.resume_sorting("MW1", as_of="2024-01-01T03:01:00Z")
    assert second["released"] == 2 and second["still_deferred"] == 0
    assert CHUTE not in plant.maintenance.blocked_chutes()


def test_resume_restores_rerouted_flights_to_their_original_chute(plant):
    _register_lanes(plant)
    flight = _flight_with_chute(plant)
    _bags(plant, flight, total=2, sorted_count=0)
    _register(plant)
    plant.plan_maintenance_yield("MW1", "yield_to_maintenance", reroute_unsorted=True)
    plant.start_maintenance("MW1")
    assert plant.chutes.require(flight.id).chute_id == OTHER
    plant.complete_maintenance("MW1")
    plant.resume_sorting("MW1", as_of=END)
    assert plant.chutes.require(flight.id).chute_id == CHUTE


def test_resume_requires_a_completed_window(plant):
    _flight_with_chute(plant)
    _register(plant)
    plant.plan_maintenance_yield("MW1", "yield_to_maintenance", reroute_unsorted=False)
    with pytest.raises(OrderingError):
        plant.resume_sorting("MW1")


# ---------------------------------------------------------------------------
# consistency and lifecycle guards
# ---------------------------------------------------------------------------


def test_plan_can_only_be_made_once_and_only_when_requested(plant):
    _flight_with_chute(plant)
    _register(plant)
    plant.plan_maintenance_yield("MW1", "wait_schedule")
    with pytest.raises(ConflictError):
        plant.plan_maintenance_yield("MW1", "wait_schedule")
    plant.start_maintenance("MW1")
    with pytest.raises(OrderingError):
        plant.plan_maintenance_yield("MW1", "wait_schedule")


def test_active_window_refuses_new_checkins_for_its_flight(plant):
    flight = _flight_with_chute(plant)
    _register(plant)
    plant.plan_maintenance_yield("MW1", "wait_schedule")
    plant.start_maintenance("MW1")
    with pytest.raises(ConflictError):
        plant.check_in("LATE01", flight.id)
    plant.complete_maintenance("MW1")
    accepted = plant.check_in("LATE02", flight.id)
    assert accepted.state is BagState.INJECTED


def test_cancel_releases_a_requested_window(plant):
    _flight_with_chute(plant)
    _register(plant)
    plant.plan_maintenance_yield(
        "MW1", "yield_to_maintenance", reroute_unsorted=False
    )
    result = plant.cancel_maintenance("MW1")
    assert result["window"].state is MaintenanceWindowState.CANCELLED
    with pytest.raises(OrderingError):
        plant.start_maintenance("MW1")
    # The cancelled window no longer blocks a fresh overlapping claim.
    overlapping = plant.register_maintenance(
        "MW9", CHUTE, START, END
    )
    assert overlapping.id == "MW9"


def test_cannot_start_or_complete_out_of_order(plant):
    _register(plant)
    with pytest.raises(OrderingError):
        plant.complete_maintenance("MW1")


def test_start_is_atomic_no_bag_left_half_moved(plant, monkeypatch):
    flight = _flight_with_chute(plant)
    bags = _bags(plant, flight, total=3, sorted_count=1)
    _register(plant)
    plant.plan_maintenance_yield("MW1", "yield_to_maintenance", reroute_unsorted=False)
    plant.clear_maintenance_chute("MW1")

    # Force the start transaction to fail halfway through applying entries:
    # the whole buffer must roll back, leaving every bag in its old state.
    original = plant.planner._apply_hold
    calls = {"n": 0}

    def flapping(session, entry, at):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom during stagger")
        return original(session, entry, at)

    monkeypatch.setattr(plant.planner, "_apply_hold", flapping)
    with pytest.raises(RuntimeError):
        plant.start_maintenance("MW1")
    states = sorted(plant.bag(b.id).state for b in bags[1:])
    assert BagState.HELD not in states
    assert plant.maintenance.get("MW1").state is MaintenanceWindowState.REQUESTED
    assert plant.store.pending() == []
    # A clean retry then moves everyone in one committed transaction.
    monkeypatch.undo()
    plant.start_maintenance("MW1")
    assert all(plant.bag(b.id).state is BagState.HELD for b in bags[1:])


def test_audit_chain_records_every_maintenance_decision(plant):
    flight = _flight_with_chute(plant)
    _bags(plant, flight, total=2, sorted_count=1)
    _register(plant)
    plant.plan_maintenance_yield("MW1", "yield_to_maintenance", reroute_unsorted=False)
    plant.clear_maintenance_chute("MW1")
    plant.start_maintenance("MW1")
    plant.complete_maintenance("MW1")
    plant.resume_sorting("MW1", as_of=END)
    assert plant.audit.verify() is True
    kinds = {entry.kind for entry in plant.audit.entries()}
    assert {
        "maintenance_register",
        "maintenance_yield_plan",
        "maintenance_start",
        "maintenance_complete",
        "maintenance_resume",
    } <= kinds


def test_everything_survives_replay_from_the_journal(tmp_path):
    from bagsort.app import Plant
    from bagsort.clock import Clock
    from bagsort.store.store import Store

    path = str(tmp_path / "state.json")
    store = Store(path)
    plant = Plant.build(store, clock=Clock())
    flight = _flight_with_chute(plant)
    _bags(plant, flight, total=3, sorted_count=1)
    _register(plant)
    plant.plan_maintenance_yield("MW1", "yield_to_maintenance", reroute_unsorted=False)
    plant.clear_maintenance_chute("MW1")
    plant.start_maintenance("MW1")
    digest = store.persist()

    reopened = Plant.build(Store.open(path), clock=Clock())
    window = reopened.maintenance.get("MW1")
    assert window.state is MaintenanceWindowState.ACTIVE
    assert len(reopened.planner.entries_for("MW1")) == 5
    assert reopened.store.persist() == digest
    assert reopened.audit.verify() is True
