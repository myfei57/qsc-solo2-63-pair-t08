"""Maintenance and schedule endpoints exercised through the JSON console."""

from __future__ import annotations

import json

from tests.support import make_server


def call(server, method, path, payload=None, query=None):
    response = server.call(method, path, payload, query)
    return response.status, json.loads(response.body)


def query(**kwargs):
    return {key: [value] for key, value in kwargs.items()}


def _setup(server, home="A", alternate="B"):
    for code in (home, alternate):
        status, _ = call(
            server,
            "POST",
            "/api/chutes",
            {"chute_id": f"T1-CH{code}", "carousel_id": "T1-CRCR1"},
        )
        assert status == 201
    for code in ("CA1", "CA2"):
        status, _ = call(
            server,
            "POST",
            "/api/flights",
            {"code": code, "carousel": "CR1", "chute": home},
        )
        assert status == 201
    call(server, "POST", "/api/flights/T1-CA1/schedule", {"cutoff": "2024-01-01T09:45:00Z"})
    call(server, "POST", "/api/flights/T1-CA2/schedule", {"cutoff": "2024-01-01T20:00:00Z"})
    for barcode, flight in (("U0", "T1-CA1"), ("L0", "T1-CA2")):
        status, bag = call(server, "POST", "/api/bags/checkin", {"barcode": barcode, "flight_id": flight})
        assert status == 200 or status == 201
        status, _ = call(server, "POST", "/api/sort", {"bag_id": bag["id"], "chute_id": "T1-CHA"})
        assert status == 201


def test_full_pause_sort_cycle_over_http(server):
    _setup(server)
    status, job = call(
        server,
        "POST",
        "/api/schedule/jobs",
        {
            "job_id": "J1",
            "chute_id": "T1-CHA",
            "start_at": "2024-01-01T09:00:00Z",
            "end_at": "2024-01-01T10:00:00Z",
        },
    )
    assert status == 201
    assert call(server, "POST", "/api/schedule/jobs/J1/start")[1]["state"] == "running"

    status, preview = call(
        server,
        "POST",
        "/api/maintenance",
        {
            "chute_id": "T1-CHA",
            "start_at": "2024-01-01T09:30:00Z",
            "end_at": "2024-01-01T10:30:00Z",
            "reason": "inspection",
        },
    )
    assert status == 201
    maintenance_id = preview["request"]["id"]
    assert preview["conflicting_jobs"] == ["J1"]

    status, decision = call(
        server,
        "POST",
        f"/api/maintenance/{maintenance_id}/resolve",
        {"policy": "pause_sort"},
    )
    assert status == 200 and decision["paused_jobs"] == ["J1"]

    status, armed = call(server, "POST", f"/api/maintenance/{maintenance_id}/arm")
    assert status == 200 and armed["barrier"]["clear"] is True
    actions = {shift["bag_id"]: shift["action"] for shift in armed["shifts"]}
    assert set(actions.values()) == {"load_early", "reroute"}

    status, _ = call(server, "POST", f"/api/maintenance/{maintenance_id}/start")
    assert status == 200
    status, recovery = call(server, "POST", f"/api/maintenance/{maintenance_id}/complete")
    assert status == 200
    assert recovery["resumed_jobs"] == ["J1"] and recovery["restored_bags"]

    status, barrier = call(server, "GET", f"/api/maintenance/{maintenance_id}/barrier")
    assert status == 200 and barrier["clear"] is True
    status, lines = call(server, "GET", "/api/maintenance/shifts", query=query(bag="T1-L0"))
    assert status == 200 and len(lines) == 1 and lines[0]["bag_id"] == "T1-L0"


def test_schedule_overlap_with_approved_maintenance_rejected(server):
    _setup(server)
    status, preview = call(
        server,
        "POST",
        "/api/maintenance",
        {
            "chute_id": "T1-CHA",
            "start_at": "2024-01-01T09:30:00Z",
            "end_at": "2024-01-01T10:30:00Z",
        },
    )
    maintenance_id = preview["request"]["id"]
    call(server, "POST", f"/api/maintenance/{maintenance_id}/resolve", {"policy": "wait_sort"})
    status, error = call(
        server,
        "POST",
        "/api/schedule/jobs",
        {
            "job_id": "LATE",
            "chute_id": "T1-CHA",
            "start_at": "2024-01-01T10:00:00Z",
            "end_at": "2024-01-01T11:00:00Z",
        },
    )
    assert status == 409 and "overlaps approved maintenance" in error["error"]


def test_overlapping_maintenance_windows_rejected(server):
    _setup(server)
    call(
        server,
        "POST",
        "/api/maintenance",
        {
            "chute_id": "T1-CHA",
            "start_at": "2024-01-01T09:30:00Z",
            "end_at": "2024-01-01T10:30:00Z",
        },
    )
    status, error = call(
        server,
        "POST",
        "/api/maintenance",
        {
            "chute_id": "T1-CHA",
            "start_at": "2024-01-01T10:00:00Z",
            "end_at": "2024-01-01T11:00:00Z",
        },
    )
    assert status == 409 and "must not overlap" in error["error"]
