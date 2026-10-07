"""The sorting schedule board: time-windowed jobs and the non-overlap rule.

A sort job owns one or more half-open segments on one chute. The scheduler is
the single place that decides which windows are allowed: a job may never
overlap an APPROVED (or later) maintenance window on the same chute, and two
live jobs may never overlap each other. Maintenance rescheduling goes through
the same helpers, so the invariant has one implementation.
"""

from __future__ import annotations

from datetime import timedelta

from bagsort.clock import Clock
from bagsort.errors import ConflictError, NotFoundError, ValidationError
from bagsort.model.entities import MaintenanceRequest, SortJob
from bagsort.model.enums import JobState, MaintenanceState
from bagsort.store.store import Store
from .windows import any_overlap, format as iso, overlaps, parse, validate_window

LIVE_JOB_STATES = (JobState.PLANNED, JobState.RUNNING, JobState.PAUSED)
BLOCKING_MAINTENANCE = (
    MaintenanceState.APPROVED,
    MaintenanceState.ARMED,
    MaintenanceState.IN_PROGRESS,
)


class Scheduler:
    """Registers, revises and queries sorting windows."""

    def __init__(self, store: Store, clock: Clock) -> None:
        self._store = store
        self._clock = clock

    # -- read side ---------------------------------------------------------

    def get(self, job_id: str) -> SortJob:
        job = self._store.state().sort_jobs.get(job_id)
        if job is None:
            raise NotFoundError(f"sort job {job_id!r} does not exist")
        return job

    def list_jobs(
        self, chute_id: str = "", state: JobState | None = None
    ) -> list[SortJob]:
        jobs = list(self._store.state().sort_jobs.values())
        if chute_id:
            jobs = [job for job in jobs if job.chute_id == chute_id]
        if state is not None:
            jobs = [job for job in jobs if job.state is state]
        return sorted(jobs, key=lambda job: (job.start_at, job.id))

    def live_jobs(self, chute_id: str) -> list[SortJob]:
        return [
            job
            for job in self.list_jobs(chute_id)
            if job.state in LIVE_JOB_STATES
        ]

    def blocking_maintenance(self, chute_id: str) -> list[MaintenanceRequest]:
        """Approved-or-later windows on this chute that scheduling must avoid."""

        return [
            request
            for request in self._store.state().maintenance.values()
            if request.chute_id == chute_id and request.state in BLOCKING_MAINTENANCE
        ]

    def conflicts(self, chute_id: str, start_at: str, end_at: str) ->list[SortJob]:
        """Live jobs on the chute that the given window would overlap."""

        window = (start_at, end_at)
        return [
            job
            for job in self.live_jobs(chute_id)
            if any_overlap(window, job.segments)
        ]

    def windows_free(
        self,
        chute_id: str,
        start_at: str,
        end_at: str,
        ignore_job: str = "",
        ignore_maintenance: str = "",
    ) -> bool:
        """Whether ``[start_at, end_at)`` is clear on the chute."""

        window = (start_at, end_at)
        for job in self.live_jobs(chute_id):
            if job.id == ignore_job:
                continue
            if any_overlap(window, job.segments):
                return False
        for request in self.blocking_maintenance(chute_id):
            if request.id == ignore_maintenance:
                continue
            if overlaps(window, (request.start_at, request.end_at)):
                return False
        return True

    # -- write side --------------------------------------------------------

    def schedule(
        self,
        job_id: str,
        chute_id: str,
        start_at: str,
        end_at: str,
        note: str = "",
        temp_for_maintenance: str = "",
    ) -> SortJob:
        identifier = (chute_id or "").strip()
        if not identifier:
            raise ValidationError("chute id must not be empty")
        validate_window(start_at, end_at)
        self._reject_maintenance_overlap(identifier, start_at, end_at)
        self._reject_job_overlap(identifier, start_at, end_at)
        job = SortJob(
            id=job_id,
            chute_id=identifier,
            start_at=start_at,
            end_at=end_at,
            note=note,
            temp_for_maintenance=temp_for_maintenance,
        )
        self._put(job)
        return job

    def revise(self, job_id: str, start_at: str, end_at: str) -> SortJob:
        job = self.get(job_id)
        if job.state not in (JobState.PLANNED, JobState.PAUSED):
            raise ConflictError(f"job {job_id!r} in state {job.state.value} cannot be revised")
        validate_window(start_at, end_at)
        self._reject_maintenance_overlap(job.chute_id, start_at, end_at)
        self._reject_job_overlap(job.chute_id, start_at, end_at, ignore_job=job_id)
        updated = job.evolved(
            start_at=start_at,
            end_at=end_at,
            segments=((start_at, end_at),),
            generation=job.generation + 1,
        )
        self._put(updated)
        return updated

    def start(self, job_id: str) -> SortJob:
        job = self.get(job_id)
        if job.state is not JobState.PLANNED:
            raise ConflictError(f"job {job_id!r} is not planned")
        self._reject_maintenance_overlap(job.chute_id, job.start_at, job.end_at)
        return self._transition(job, JobState.RUNNING)

    def pause(self, job_id: str) -> SortJob:
        job = self.get(job_id)
        if job.state not in (JobState.PLANNED, JobState.RUNNING):
            raise ConflictError(f"job {job_id!r} cannot pause from {job.state.value}")
        return self._transition(job, JobState.PAUSED)

    def resume(self, job_id: str) -> SortJob:
        job = self.get(job_id)
        if job.state is not JobState.PAUSED:
            raise ConflictError(f"job {job_id!r} is not paused")
        if any(
            not self.windows_free(job.chute_id, segment[0], segment[1], ignore_job=job.id)
            for segment in job.segments
        ):
            raise ConflictError(f"resumed job {job_id!r} overlaps another window")
        return self._transition(job, JobState.RUNNING)

    def complete(self, job_id: str) -> SortJob:
        job = self.get(job_id)
        if job.state in (JobState.COMPLETED, JobState.CANCELLED):
            raise ConflictError(f"job {job_id!r} is already {job.state.value}")
        return self._transition(job, JobState.COMPLETED)

    def cancel(self, job_id: str) -> SortJob:
        job = self.get(job_id)
        if job.state in (JobState.COMPLETED, JobState.CANCELLED):
            raise ConflictError(f"job {job_id!r} is already {job.state.value}")
        return self._transition(job, JobState.CANCELLED)

    # -- maintenance driven rescheduling -----------------------------------

    def replace_segments(
        self,
        job: SortJob,
        segments: tuple[tuple[str, str], ...],
        state: JobState,
        bump_generation: bool = True,
    ) -> SortJob:
        """Put a job on a new segment set, asserting every segment is clear."""

        if not segments:
            raise ValidationError("a rescheduled job must keep at least one segment")
        for start_at, end_at in segments:
            validate_window(start_at, end_at)
            if not self.windows_free(job.chute_id, start_at, end_at, ignore_job=job.id):
                raise ConflictError(
                    f"rescheduled segment {start_at}/{end_at} on {job.chute_id} "
                    "overlaps another window"
                )
        first = min(parse(segment[0]) for segment in segments)
        last = max(parse(segment[1]) for segment in segments)
        updated = job.evolved(
            start_at=iso(first),
            end_at=iso(last),
            segments=tuple(sorted(segments)),
            state=state,
            generation=job.generation + (1 if bump_generation else 0),
        )
        self._put(updated)
        return updated

    def first_free_slot(
        self,
        chute_id: str,
        earliest: str,
        duration_minutes: int,
        ignore_job: str = "",
    ) -> tuple[str, str]:
        """Earliest free slot of the given length at or after ``earliest``."""

        blocked_segments = self._blocked_segments(chute_id, ignore_job)
        cursor = parse(earliest)
        while True:
            end = cursor + timedelta(minutes=duration_minutes)
            collision = next(
                (
                    segment
                    for segment in blocked_segments
                    if overlaps((iso(cursor), iso(end)), segment)
                ),
                None,
            )
            if collision is None:
                return iso(cursor), iso(end)
            cursor = parse(collision[1])

    # -- helpers -----------------------------------------------------------

    def _blocked_segments(
        self, chute_id: str, ignore_job: str
    ) -> list[tuple[str, str]]:
        segments: list[tuple[str, str]] = []
        for job in self.live_jobs(chute_id):
            if job.id != ignore_job:
                segments.extend(job.segments)
        for request in self.blocking_maintenance(chute_id):
            segments.append((request.start_at, request.end_at))
        return sorted(segments)

    def _reject_maintenance_overlap(
        self, chute_id: str, start_at: str, end_at: str
    ) -> None:
        window = (start_at, end_at)
        for request in self.blocking_maintenance(chute_id):
            if overlaps(window, (request.start_at, request.end_at)):
                raise ConflictError(
                    f"sorting window {start_at}/{end_at} overlaps approved "
                    f"maintenance {request.id!r} on {chute_id}"
                )

    def _reject_job_overlap(
        self, chute_id: str, start_at: str, end_at: str, ignore_job: str = ""
    ) -> None:
        window = (start_at, end_at)
        for job in self.live_jobs(chute_id):
            if job.id == ignore_job:
                continue
            if any_overlap(window, job.segments):
                raise ConflictError(
                    f"sorting window {start_at}/{end_at} overlaps job "
                    f"{job.id!r} on {chute_id}"
                )

    def _transition(self, job: SortJob, state: JobState) -> SortJob:
        updated = job.evolved(state=state)
        self._put(updated)
        return updated

    def _put(self, job: SortJob) -> None:
        with self._store.session() as session:
            session.state.sort_jobs[job.id] = job
            session.record("sortjob.put", {"job": job.as_dict()}, self._clock.now_iso())
