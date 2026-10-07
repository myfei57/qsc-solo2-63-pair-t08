"""Enumerations for the two state machines the plant tracks."""

from __future__ import annotations

from enum import Enum


class FlightState(str, Enum):
    """Lifecycle of one flight batch."""

    OPEN = "open"
    BOARDING = "boarding"
    CUTOFF = "cutoff"
    CLOSED = "closed"


class BagState(str, Enum):
    """Lifecycle of one piece of baggage."""

    CHECKED_IN = "checked_in"
    INJECTED = "injected"
    HELD = "held"
    SORTED = "sorted"
    RECHECKED = "rechecked"
    LOADED = "loaded"
    DIVERTED = "diverted"

    @property
    def active(self) -> bool:
        """Whether the bag still occupies plant capacity."""

        return self in (
            BagState.CHECKED_IN,
            BagState.INJECTED,
            BagState.HELD,
            BagState.SORTED,
            BagState.RECHECKED,
        )


class MaintenanceWindowState(str, Enum):
    """Lifecycle of a chute maintenance window."""

    REQUESTED = "requested"
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"

    @property
    def open_claim(self) -> bool:
        """Whether the window still reserves its chute (so overlaps are refused)."""

        return self in (MaintenanceWindowState.REQUESTED, MaintenanceWindowState.ACTIVE)


class ConflictPolicy(str, Enum):
    """Who gives way when a maintenance window meets live sorting work."""

    WAIT_SCHEDULE = "wait_schedule"
    YIELD_TO_MAINTENANCE = "yield_to_maintenance"


class StaggerAction(str, Enum):
    """What the staggered plan does with one bag during the maintenance window."""

    HOLD = "hold"
    REROUTE = "reroute"
    CLEAR = "clear"
    RELEASE = "release"


class StaggerStatus(str, Enum):
    """Execution state of one staggered entry."""

    PLANNED = "planned"
    DONE = "done"

class Verdict(str, Enum):
    """Result of comparing an observed (epoch, sequence) pair with the high-water mark."""

    FRESH = "fresh"
    REPLAYED = "replayed"
    ROLLED_OVER = "rolled_over"
