"""On-disk ledger of yield and peak-shift artifacts.

The journal snapshot already makes every artifact durable; this ledger writes
the same artifacts as standalone, per-maintenance JSON files so an operator can
re-open one decision, one barrier manifest, one recovery report or one bag's
shift line without replaying the store. Writes are atomic and idempotent.
"""

from __future__ import annotations

import json
import os
from typing import Any, Sequence

from bagsort.model.entities import (
    BarrierManifest,
    MaintenanceRequest,
    RecoveryReport,
    ShiftRecord,
    YieldDecision,
)


class ArtifactLedger:
    """One directory of reviewable artifacts, grouped by maintenance id."""

    def __init__(self, root: str) -> None:
        self._root = os.path.abspath(root)

    @property
    def root(self) -> str:
        return self._root

    def path_for(self, maintenance_id: str, name: str) -> str:
        return os.path.join(self._root, maintenance_id, name)

    def write_decision(self, decision: YieldDecision) -> str:
        return self._dump(
            self.path_for(decision.maintenance_id, "decision.json"),
            decision.as_dict(),
        )

    def write_barrier(self, barrier: BarrierManifest) -> str:
        return self._dump(
            self.path_for(barrier.maintenance_id, "barrier.json"),
            barrier.as_dict(),
        )

    def write_recovery(self, recovery: RecoveryReport) -> str:
        return self._dump(
            self.path_for(recovery.maintenance_id, "recovery.json"),
            recovery.as_dict(),
        )

    def write_shifts(
        self, maintenance_id: str, shifts: Sequence[ShiftRecord]
    ) -> str:
        payload = {
            "maintenance_id": maintenance_id,
            "count": len(shifts),
            "shifts": [shift.as_dict() for shift in shifts],
        }
        return self._dump(self.path_for(maintenance_id, "shifts.json"), payload)

    def write_request(self, request: MaintenanceRequest) -> str:
        return self._dump(
            self.path_for(request.id, "request.json"), request.as_dict()
        )

    def read(self, maintenance_id: str, name: str) -> dict[str, Any]:
        with open(self.path_for(maintenance_id, name), "r", encoding="utf-8") as handle:
            return json.load(handle)

    def _dump(self, path: str, payload: dict[str, Any]) -> str:
        parent = os.path.dirname(path)
        os.makedirs(parent, exist_ok=True)
        temporary = f"{path}.tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(temporary, path)
        return path
