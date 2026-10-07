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
    SORTED = "sorted"
    RECHECKED = "rechecked"
    LOADED = "loaded"
    DIVERTED = "diverted"
    HELD = "held"

    @property
    def active(self) -> bool:
        """Whether the bag still occupies plant capacity."""

        return self in (
            BagState.CHECKED_IN,
            BagState.INJECTED,
            BagState.SORTED,
            BagState.RECHECKED,
        )

    @property
    def occupies_chute(self) -> bool:
        """Whether the bag is physically sitting in its chute.

        HELD bags are registered and accounted for but pulled out of the lane,
        so they no longer block a maintenance barrier. LOADED/DIVERTED bags
        have left the line.
        """

        return self in (BagState.SORTED, BagState.RECHECKED)


class JobState(str, Enum):
    """Lifecycle of one scheduled sorting window."""

    PLANNED = "planned"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class MaintenanceState(str, Enum):
    """Lifecycle of one chute maintenance request."""

    REQUESTED = "requested"
    APPROVED = "approved"
    ARMED = "armed"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    CANCELLED = "cancelled"


class YieldPolicy(str, Enum):
    """Who concedes when a maintenance window meets sorting work."""

    WAIT_SORT = "wait_sort"
    PAUSE_SORT = "pause_sort"


class ShiftAction(str, Enum):
    """How one bag is moved off a chute that maintenance is about to take."""

    REROUTE = "reroute"
    HOLD = "hold"
    LOAD_EARLY = "load_early"

class Verdict(str, Enum):
    """Result of comparing an observed (epoch, sequence) pair with the high-water mark."""

    FRESH = "fresh"
    REPLAYED = "replayed"
    ROLLED_OVER = "rolled_over"
