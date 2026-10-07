"""Half-open ISO time windows and the overlap rules maintenance scheduling uses.

Times are UTC ISO-8601 strings (``YYYY-MM-DDTHH:MM:SSZ``), which sort
lexicographically, so every comparison is a string comparison and every
computation here is reproducible without the wall clock.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence

from bagsort.errors import ValidationError

ISO = "%Y-%m-%dT%H:%M:%SZ"


def parse(at: str) -> datetime:
    try:
        return datetime.strptime(at.strip(), ISO).replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValidationError(f"time {at!r} must look like 2024-01-01T09:00:00Z") from exc


def format(at: datetime) -> str:
    return at.astimezone(timezone.utc).strftime(ISO)


def validate_window(start_at: str, end_at: str) -> None:
    start = parse(start_at)
    end = parse(end_at)
    if end <= start:
        raise ValidationError("window end must be strictly after its start")


def minutes_between(earlier: str, later: str) -> int:
    return int((parse(later) - parse(earlier)).total_seconds() // 60)


def shift(start_at: str, end_at: str, minutes: int) -> tuple[str, str]:
    delta = timedelta(minutes=minutes)
    return format(parse(start_at) + delta), format(parse(end_at) + delta)


def overlaps(first: Sequence[str], second: Sequence[str]) -> bool:
    """Half-open intersection: ``[start, end)``; touching edges do not clash."""

    return parse(first[0]) < parse(second[1]) and parse(second[0]) < parse(first[1])


def intersection(
    first: Sequence[str], second: Sequence[str]
) -> tuple[str, str] | None:
    if not overlaps(first, second):
        return None
    start = max(parse(first[0]), parse(second[0]))
    end = min(parse(first[1]), parse(second[1]))
    return format(start), format(end)


def subtract(window: Sequence[str], block: Sequence[str]) -> tuple[tuple[str, str], ...]:
    """The parts of ``window`` not covered by ``block`` (0, 1 or 2 pieces)."""

    hit = intersection(window, block)
    if hit is None:
        return ((window[0], window[1]),)
    pieces: list[tuple[str, str]] = []
    if parse(window[0]) < parse(hit[0]):
        pieces.append((window[0], hit[0]))
    if parse(hit[1]) < parse(window[1]):
        pieces.append((hit[1], window[1]))
    return tuple(pieces)


def any_overlap(window: Sequence[str], others: Iterable[Sequence[str]]) -> bool:
    return any(overlaps(window, other) for other in others)
