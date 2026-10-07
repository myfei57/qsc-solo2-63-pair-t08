"""Maintenance windows, sorting yield plans and per-bag staggering.

The three pieces move together:

* :class:`MaintenanceBoard` owns the window lifecycle and the chute gate.
* :class:`MaintenancePlanner` decides who gives way, writes the per-bag
  stagger entries and opens the recovery path once maintenance is over.
* :mod:`bagsort.maintenance.windows` holds the time helpers both share.

Every decision lands as journal commands in one transaction, so a yield plan
and the bag states it produces can never diverge.
"""

from __future__ import annotations

from .planner import MaintenancePlanner
from .windows import MaintenanceBoard

__all__ = ["MaintenanceBoard", "MaintenancePlanner"]
