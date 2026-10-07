"""Time helpers and the maintenance window registry/lifecycle.

A maintenance window reserves one chute over a half-open interval
``[start_at, end_at)``. Two live claims on the same chute may never overlap,
which is what keeps a maintenance window from colliding with another work
window. Everything is expressed in the service's ``...Z`` UTC strings so the
deterministic clock drives the whole subsystem.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bagsort.clock import Clock
from bagsort.errors import ConflictError, NotFoundError, OrderingError, ValidationError
from bagsort.model.entities import MaintenanceWindow
from bagsort.model.enums import MaintenanceWindowState
from bagsort.store.store import Store

_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def parse_instant(value: str) -> datetime:
    """Parse a ``YYYY-MM-DDTHH:MM:SSZ`` instant into an aware UTC datetime."""

    text = (value or "").strip()
    if not text:
        raise ValidationError("time must not be empty")
    try:
        parsed = datetime.strptime(text, _TIME_FORMAT)
    except ValueError as exc:
        raise ValidationError(f"time {value!r} must look like {_TIME_FORMAT!r}") from exc
    return parsed.replace(tzinfo=timezone.utc)


def format_instant(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(_TIME_FORMAT)


def format_delta(seconds: int) -> str:
    """Render a non-negative duration as ``HH:MM:SS`` for tradeoff notes."""

    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def intervals_overlap(start_a: str, end_a: str, start_b: str, end_b: str) -> bool:
    """Half-open overlap: touching endpoints do not collide."""

    return parse_instant(start_a) < parse_instant(end_b) and parse_instant(
        start_b
    ) < parse_instant(end_a)


def seconds_between(earlier: str, later: str) -> int:
    delta = parse_instant(later) - parse_instant(earlier)
    return int(delta.total_seconds())


def plus_seconds(value: str, seconds: float) -> str:
    return format_instant(parse_instant(value) + timedelta(seconds=seconds))


class MaintenanceBoard:
    """Registers windows, gates the chutes and owns the window state machine."""

    def __init__(self, store: Store, clock: Clock) -> None:
        self._store = store
        self._clock = clock

    # -- queries -----------------------------------------------------------

    def get(self, window_id: str) -> MaintenanceWindow:
        window = self._store.state().maintenance_windows.get(window_id)
        if window is None:
            raise NotFoundError(f"maintenance window {window_id!r} is not registered")
        return window

    def list(self) -> list[MaintenanceWindow]:
        windows = self._store.state().maintenance_windows
        return [windows[key] for key in sorted(windows)]

    def for_chute(self, chute_id: str) -> list[MaintenanceWindow]:
        return [
            window
            for window in self.list()
            if window.chute_id == chute_id and window.state.open_claim
        ]

    def active(self) -> list[MaintenanceWindow]:
        return [
            window
            for window in self.list()
            if window.state is MaintenanceWindowState.ACTIVE
        ]

    def active_for_chute(self, chute_id: str) -> MaintenanceWindow | None:
        for window in self.active():
            if window.chute_id == chute_id:
                return window
        return None

    def is_chute_blocked(self, chute_id: str) -> bool:
        return self.active_for_chute(chute_id) is not None

    def blocked_chutes(self) -> list[str]:
        return sorted({window.chute_id for window in self.active()})

    # -- lifecycle ---------------------------------------------------------

    def register(
        self,
        window_id: str,
        chute_id: str,
        start_at: str,
        end_at: str,
        ticket: str = "",
    ) -> MaintenanceWindow:
        identifier = (window_id or "").strip()
        chute = (chute_id or "").strip()
        if not identifier:
            raise ValidationError("maintenance window id must not be empty")
        if not chute:
            raise ValidationError("chute id must not be empty")
        start = parse_instant(start_at)
        end = parse_instant(end_at)
        if end <= start:
            raise ValidationError("maintenance window must end after it starts")
        if identifier in self._store.state().maintenance_windows:
            raise ConflictError(f"maintenance window {identifier!r} is already registered")
        for other in self.for_chute(chute):
            if intervals_overlap(start_at, end_at, other.start_at, other.end_at):
                raise ConflictError(
                    f"maintenance window {identifier!r} on {chute!r} overlaps window "
                    f"{other.id!r} ({other.start_at}..{other.end_at})"
                )
        window = MaintenanceWindow(
            id=identifier,
            chute_id=chute,
            start_at=format_instant(start),
            end_at=format_instant(end),
            ticket=(ticket or "").strip(),
        )
        self._put(window)
        return window

    def complete(self, window_id: str, at: str = "") -> MaintenanceWindow:
        window = self.get(window_id)
        if window.state is MaintenanceWindowState.COMPLETED:
            return window
        if window.state is not MaintenanceWindowState.ACTIVE:
            raise OrderingError(
                f"maintenance window {window_id!r} is {window.state.value}, not active"
            )
        updated = window.evolved(
            state=MaintenanceWindowState.COMPLETED,
            completed_at=at or self._clock.now_iso(),
        )
        self._put(updated)
        return updated

    def cancel(self, window_id: str, at: str = "") -> MaintenanceWindow:
        """Drop a window that has not started, releasing its hold on the chute."""

        window = self.get(window_id)
        if window.state is not MaintenanceWindowState.REQUESTED:
            raise OrderingError(
                f"maintenance window {window_id!r} is {window.state.value}; only requested "
                "windows can be cancelled"
            )
        updated = window.evolved(
            state=MaintenanceWindowState.CANCELLED,
            completed_at=at or self._clock.now_iso(),
        )
        self._put(updated)
        return updated

    def _put(self, window: MaintenanceWindow) -> None:
        with self._store.session() as session:
            session.state.maintenance_windows[window.id] = window
            session.record(
                "maintenance.put", {"window": window.as_dict()}, self._clock.now_iso()
            )
