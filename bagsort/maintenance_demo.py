"""Deterministic maintenance/scheduling scenario.

Two conflicts are played out on the same wired plant the console drives:

* one chute where the crew is asked to **wait for sorting** to finish;
* one chute where sorting is asked to **pause and peak-shift** instead.

Every artifact (decision, barrier, shifts, recovery) is written through the
journal and the artifact ledger, so the returned summary points at files an
operator can re-open line by line.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bagsort.model.enums import BagState, YieldPolicy

if TYPE_CHECKING:  # pragma: no cover - typing only
    from bagsort.app import Plant

WINDOW_START = "2024-01-01T09:30:00Z"
WINDOW_END = "2024-01-01T10:30:00Z"


def _prepare(plant: "Plant", code: str, alt_code: str) -> tuple[str, str]:
    home = plant.namespace.chute_id(code)
    alternate = plant.namespace.chute_id(alt_code)
    carousel = plant.namespace.carousel_id("CR1")
    if not plant.registry.known(home):
        plant.registry.register(home, carousel)
    if not plant.registry.known(alternate):
        plant.registry.register(alternate, carousel)
    return home, alternate


def _bags_for(plant: "Plant", flight_id: str, chute: str, count: int, prefix: str):
    bags = [plant.check_in(f"{prefix}{index:02d}", flight_id) for index in range(count)]
    for bag in bags:
        plant.push_bag(bag.id, chute)
    return bags


def run_wait_scenario(plant: "Plant") -> dict[str, object]:
    """Maintenance arrives while sorting runs and, by policy, waits for it."""

    home, _ = _prepare(plant, "C", "D")
    flight = plant.register_flight("CA9001", "CR1", "C")
    plant.schedule_sort(
        "WAIT-JOB", home, "2024-01-01T09:00:00Z", "2024-01-01T10:00:00Z"
    )
    plant.start_sort("WAIT-JOB")
    _bags_for(plant, flight.id, home, 2, "WB")

    # Declared while the job is still live: the wait ruling pushes the crew's
    # window out to the job's end.
    preview = plant.declare_maintenance(
        home, WINDOW_START, WINDOW_END, reason="roller inspection"
    )
    decision = plant.resolve_maintenance(preview.request.id, YieldPolicy.WAIT_SORT.value)

    # Arming is interlocked until the crew's side of the bargain is honoured:
    # every conflicting sort job must actually finish first.
    plant.complete_sort("WAIT-JOB")
    result = plant.arm_maintenance(preview.request.id)
    plant.start_maintenance(preview.request.id)
    recovery = plant.complete_maintenance(preview.request.id)
    return {
        "maintenance_id": preview.request.id,
        "chute": home,
        "policy": "wait_sort",
        "conflicts": list(preview.conflicting_jobs),
        "delay_minutes": decision.delay_minutes,
        "approved_window": [decision.approved_start, decision.approved_end],
        "barrier_clear": result.barrier.clear,
        "restored_bags": list(recovery.restored_bags),
        "concession": decision.concession,
    }


def run_pause_scenario(plant: "Plant") -> dict[str, object]:
    """Sorting is running; the crew takes the lane and bags are peak-shifted."""

    home, alternate = _prepare(plant, "A", "B")
    urgent = plant.register_flight("CA1001", "CR1", "A")
    later = plant.register_flight("CA1002", "CR1", "A")
    plant.revise_schedule(urgent.id, "2024-01-01T09:45:00Z")
    plant.revise_schedule(later.id, "2024-01-01T18:00:00Z")

    plant.schedule_sort(
        "PAUSE-JOB", home, "2024-01-01T09:00:00Z", "2024-01-01T10:00:00Z"
    )
    plant.start_sort("PAUSE-JOB")
    urgent_bags = _bags_for(plant, urgent.id, home, 1, "UB")
    later_bags = _bags_for(plant, later.id, home, 1, "LB")

    preview = plant.declare_maintenance(
        home, WINDOW_START, WINDOW_END, reason="belt wear strip"
    )
    decision = plant.resolve_maintenance(preview.request.id, YieldPolicy.PAUSE_SORT.value)
    result = plant.arm_maintenance(preview.request.id)
    actions = {shift.bag_id: shift.action.value for shift in result.shifts}
    plant.start_maintenance(preview.request.id)
    recovery = plant.complete_maintenance(preview.request.id)

    job = plant.scheduler.get("PAUSE-JOB")
    loaded = all(
        plant.bag(bag.id).state is BagState.LOADED for bag in urgent_bags
    )
    rerouted_back = all(
        plant.bag(bag.id).chute_id == home and plant.bag(bag.id).state is BagState.SORTED
        for bag in later_bags
    )
    return {
        "maintenance_id": preview.request.id,
        "chute": home,
        "alternate": alternate,
        "policy": "pause_sort",
        "conflicts": list(preview.conflicting_jobs),
        "flight_impact": decision.flight_impact,
        "paused_jobs": list(decision.paused_jobs),
        "job_segments_after_pause": [list(segment) for segment in job.segments],
        "barrier_clear": result.barrier.clear,
        "shift_count": len(result.shifts),
        "shift_actions": actions,
        "urgent_bags_loaded": loaded,
        "rerouted_bags_restored": rerouted_back,
        "restored_bags": list(recovery.restored_bags),
        "resumed_jobs": list(recovery.resumed_jobs),
        "completed_jobs": list(recovery.completed_jobs),
        "concession": decision.concession,
    }


def run_maintenance_demo(plant: "Plant") -> dict[str, object]:
    """Exercise both policies and return the counters the CLI prints."""

    wait_summary = run_wait_scenario(plant)
    pause_summary = run_pause_scenario(plant)
    summary = {
        "wait": wait_summary,
        "pause": pause_summary,
        "maintenance_requests": len(plant.list_maintenance()),
        "shift_records": len(plant.maintenance_shifts()),
        "audit_intact": plant.audit.verify(),
        "watermark": plant.store.watermark,
    }
    summary["invariants_hold"] = maintenance_invariants_hold(summary)
    return summary


def maintenance_invariants_hold(summary: dict[str, object]) -> bool:
    wait = summary["wait"]
    pause = summary["pause"]
    return bool(
        wait["barrier_clear"]
        and wait["delay_minutes"] >= 30
        and pause["barrier_clear"]
        and pause["urgent_bags_loaded"]
        and pause["rerouted_bags_restored"]
        and set(pause["shift_actions"].values()) >= {"load_early", "reroute"}
        and pause["resumed_jobs"] == ["PAUSE-JOB"]
        and summary["audit_intact"]
    )
