"""Maintenance console routes: declaration, planning, lifecycle and stagger review."""

from __future__ import annotations

from tests.test_console_api import call


def _flight(server):
    status, flight = call(
        server,
        "POST",
        "/api/flights",
        {"code": "CA1234", "carousel": "CR1", "chute": "A"},
    )
    assert status == 201
    return flight["id"]


def _bags(server, flight_id, total=3, sorted_count=1):
    status, created = call(
        server,
        "POST",
        "/api/bags/checkin-batch",
        [
            {"barcode": f"BAG{i:04d}", "flight_id": flight_id}
            for i in range(total)
        ],
    )
    assert status == 201 and len(created) == total
    for bag in created[:sorted_count]:
        status, _ = call(
            server,
            "POST",
            "/api/sort",
            {"bag_id": bag["id"], "chute_id": "T1-CHA"},
        )
        assert status == 201
    return created


def test_declare_plan_start_complete_resume_flow(server):
    flight_id = _flight(server)
    _bags(server, flight_id)

    status, window = call(
        server,
        "POST",
        "/api/maintenance/windows",
        {
            "window_id": "MW1",
            "chute_id": "T1-CHA",
            "start_at": "2024-01-01T01:00:00Z",
            "end_at": "2024-01-01T03:00:00Z",
            "ticket": "CREW-7",
        },
    )
    assert status == 201 and window["state"] == "requested"

    status, body = call(server, "GET", "/api/maintenance/windows")
    assert status == 200 and [w["id"] for w in body] == ["MW1"]

    status, plan = call(
        server,
        "POST",
        "/api/maintenance/windows/MW1/plan",
        {"policy": "yield_to_maintenance", "reroute_unsorted": False},
    )
    assert status == 201
    assert plan["held_bags"] == 2 and plan["clear_bags"] == 1
    assert "schedule gives up" in plan["tradeoff"]

    status, plan_get = call(server, "GET", "/api/maintenance/windows/MW1/plan")
    assert status == 200 and plan_get["id"] == plan["id"]

    status, stagger = call(server, "GET", "/api/maintenance/windows/MW1/stagger")
    assert status == 200 and len(stagger) == 5
    assert sorted(row["action"] for row in stagger) == [
        "clear",
        "hold",
        "hold",
        "release",
        "release",
    ]

    status, cleared = call(server, "POST", "/api/maintenance/windows/MW1/clear")
    assert status == 200 and cleared["cleared"] == 1

    status, started = call(server, "POST", "/api/maintenance/windows/MW1/start")
    assert status == 200 and started["state"] == "active"

    status, blocked = call(server, "GET", "/api/maintenance/chutes")
    assert blocked == {"blocked_chutes": ["T1-CHA"]}

    status, completed = call(server, "POST", "/api/maintenance/windows/MW1/complete")
    assert status == 200 and completed["state"] == "completed"

    status, resumed = call(
        server,
        "POST",
        "/api/maintenance/windows/MW1/resume",
        {"as_of": "2024-01-01T03:00:00Z"},
    )
    assert status == 200 and resumed["released"] == 2


def test_wait_policy_tradeoff_is_returned(server):
    _flight(server)
    call(
        server,
        "POST",
        "/api/maintenance/windows",
        {
            "window_id": "MW1",
            "chute_id": "T1-CHA",
            "start_at": "2024-01-01T01:00:00Z",
            "end_at": "2024-01-01T03:00:00Z",
        },
    )
    call(
        server,
        "POST",
        "/api/flights/T1-CA1234/cutoff-time",
        {"cutoff": "2024-01-01T02:00:00Z"},
    )
    status, plan = call(
        server, "POST", "/api/maintenance/windows/MW1/plan", {"policy": "wait_schedule"}
    )
    assert status == 201
    assert plan["maintenance_delay"] == 3600
    assert "maintenance start given up" in plan["tradeoff"]


def test_start_with_sorted_bags_is_a_conflict(server):
    flight_id = _flight(server)
    _bags(server, flight_id, total=1, sorted_count=1)
    call(
        server,
        "POST",
        "/api/maintenance/windows",
        {
            "window_id": "MW1",
            "chute_id": "T1-CHA",
            "start_at": "2024-01-01T01:00:00Z",
            "end_at": "2024-01-01T03:00:00Z",
        },
    )
    call(
        server,
        "POST",
        "/api/maintenance/windows/MW1/plan",
        {"policy": "yield_to_maintenance", "reroute_unsorted": False},
    )
    status, body = call(server, "POST", "/api/maintenance/windows/MW1/start")
    assert status == 409 and "CLEAR" in body["error"]


def test_overlapping_window_is_a_conflict(server):
    payload = {
        "window_id": "MW1",
        "chute_id": "T1-CHA",
        "start_at": "2024-01-01T01:00:00Z",
        "end_at": "2024-01-01T03:00:00Z",
    }
    call(server, "POST", "/api/maintenance/windows", payload)
    status, body = call(
        server, "POST", "/api/maintenance/windows", {**payload, "window_id": "MW2",
        "start_at": "2024-01-01T02:00:00Z"}
    )
    assert status == 409 and "overlaps" in body["error"]


def test_stagger_entries_are_listed_for_review(server):
    flight_id = _flight(server)
    _bags(server, flight_id, total=2, sorted_count=0)
    call(
        server,
        "POST",
        "/api/maintenance/windows",
        {
            "window_id": "MW1",
            "chute_id": "T1-CHA",
            "start_at": "2024-01-01T01:00:00Z",
            "end_at": "2024-01-01T03:00:00Z",
        },
    )
    call(
        server,
        "POST",
        "/api/maintenance/windows/MW1/plan",
        {"policy": "yield_to_maintenance", "reroute_unsorted": False},
    )
    status, all_entries = call(server, "GET", "/api/maintenance/stagger")
    assert status == 200 and len(all_entries) == 4
    assert sorted(row["action"] for row in all_entries) == [
        "hold",
        "hold",
        "release",
        "release",
    ]
    for row in all_entries:
        assert {"sequence", "bag_id", "action", "status", "planned_at"} <= set(row)
