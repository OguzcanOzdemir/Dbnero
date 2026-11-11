"""Background job execution, independent of Qt.

Long-running work (backup, restore, compare) runs on a thread pool. Each job
gets a ``JobHandle`` with a ``JobContext`` (cancel + progress) and a logger
whose lines are persisted to the catalog. Listeners receive ``JobEvent``s from
worker threads; the UI layer marshals them onto the GUI thread.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import traceback
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from dbnero.adapters.base import JobContext, OperationCancelled
from dbnero.core.logging_setup import mask_secrets
from dbnero.core.models import JobKind, JobStatus, new_id, utc_now
from dbnero.storage.catalog import Catalog, JobRecord

log = logging.getLogger(__name__)


class JobEventType(StrEnum):
    STARTED = "started"
    PROGRESS = "progress"
    LOG = "log"
    FINISHED = "finished"


@dataclass(slots=True)
class Job:
    job_id: str
    kind: JobKind
    title: str
    ctx: JobContext
    connection_id: str | None = None
    status: JobStatus = JobStatus.QUEUED
    progress: float | None = None
    message: str = ""
    result: Any = None
    error: str | None = None
    started_at_utc: datetime = field(default_factory=utc_now)
    finished_at_utc: datetime | None = None
    future: Future[Any] | None = None

    @property
    def done(self) -> bool:
        return self.status in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}


@dataclass(slots=True)
class JobEvent:
    type: JobEventType
    job: Job
    level: str = "INFO"
    message: str = ""


JobListener = Callable[[JobEvent], None]


class JobHandle:
    """What a job function receives."""

    def __init__(self, manager: JobManager, job: Job) -> None:
        self._manager = manager
        self.job = job

    @property
    def ctx(self) -> JobContext:
        return self.job.ctx

    def log(self, message: str, level: str = "INFO") -> None:
        self._manager._log(self.job, level, message)

    def warning(self, message: str) -> None:
        self.log(message, "WARNING")

    def error(self, message: str) -> None:
        self.log(message, "ERROR")


class JobManager:
    def __init__(self, catalog: Catalog | None, max_workers: int = 2) -> None:
        self._catalog = catalog
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, max_workers), thread_name_prefix="dbnero-job"
        )
        self._jobs: dict[str, Job] = {}
        self._listeners: list[JobListener] = []
        self._lock = threading.Lock()

    # -- listeners -------------------------------------------------------

    def add_listener(self, listener: JobListener) -> None:
        with self._lock:
            self._listeners.append(listener)

    def remove_listener(self, listener: JobListener) -> None:
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def _emit(self, event: JobEvent) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(event)
            except Exception:
                log.exception("Job listener failed")

    # -- jobs ------------------------------------------------------------

    def submit(
        self,
        kind: JobKind,
        title: str,
        fn: Callable[[JobHandle], Any],
        connection_id: str | None = None,
    ) -> Job:
        job = Job(
            job_id=new_id(), kind=kind, title=title, ctx=JobContext(), connection_id=connection_id
        )

        def on_progress(fraction: float | None, message: str) -> None:
            job.progress = fraction
            job.message = message
            self._emit(JobEvent(JobEventType.PROGRESS, job, message=message))

        job.ctx.on_progress = on_progress
        with self._lock:
            self._jobs[job.job_id] = job
        if self._catalog is not None:
            self._catalog.add_job(
                JobRecord(
                    job_id=job.job_id,
                    kind=kind,
                    status=JobStatus.QUEUED,
                    title=title,
                    started_at_utc=job.started_at_utc,
                    connection_id=connection_id,
                )
            )
        job.future = self._executor.submit(self._run, job, fn)
        return job

    def _run(self, job: Job, fn: Callable[[JobHandle], Any]) -> Any:
        job.status = JobStatus.RUNNING
        job.started_at_utc = utc_now()
        self._emit(JobEvent(JobEventType.STARTED, job))
        handle = JobHandle(self, job)
        try:
            job.ctx.raise_if_cancelled()
            job.result = fn(handle)
            job.status = JobStatus.SUCCEEDED
            self._log(job, "INFO", "Finished")
        except OperationCancelled:
            job.status = JobStatus.CANCELLED
            job.error = "Cancelled"
            self._log(job, "WARNING", "Job cancelled")
        except Exception as exc:
            job.status = JobStatus.FAILED
            job.error = mask_secrets(str(exc)) or type(exc).__name__
            self._log(job, "ERROR", job.error)
            log.debug("Job %s failed:\n%s", job.job_id, traceback.format_exc())
        finally:
            job.finished_at_utc = utc_now()
            if self._catalog is not None:
                try:
                    self._catalog.finish_job(job.job_id, job.status, job.error)
                except Exception:
                    log.exception("Could not record job result")
            self._emit(JobEvent(JobEventType.FINISHED, job))
        return job.result

    def _log(self, job: Job, level: str, message: str) -> None:
        message = mask_secrets(message)
        log.log(logging.getLevelName(level), "[%s] %s", job.title, message)
        if self._catalog is not None:
            try:
                self._catalog.add_job_log(job.job_id, level, message)
            except Exception:
                log.exception("Could not persist job log")
        self._emit(JobEvent(JobEventType.LOG, job, level=level, message=message))

    def cancel(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is not None and not job.done:
            job.ctx.cancel()
            if job.future is not None and job.future.cancel():
                # Never started: finish it here.
                job.status = JobStatus.CANCELLED
                job.finished_at_utc = utc_now()
                if self._catalog is not None:
                    self._catalog.finish_job(job_id, JobStatus.CANCELLED, "Cancelled")
                self._emit(JobEvent(JobEventType.FINISHED, job))

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def active_jobs(self) -> list[Job]:
        with self._lock:
            return [j for j in self._jobs.values() if not j.done]

    def wait(self, job: Job, timeout: float | None = None) -> Any:
        if job.future is None:
            return job.result
        # Errors are recorded on the job itself; only wait for completion here.
        with contextlib.suppress(Exception):
            job.future.result(timeout)
        return job.result

    def shutdown(self, cancel_running: bool = True) -> None:
        if cancel_running:
            for job in self.active_jobs():
                job.ctx.cancel()
        self._executor.shutdown(wait=True, cancel_futures=True)

    def recover_interrupted(self) -> int:
        """Mark jobs left RUNNING/QUEUED by a crashed previous session as failed."""
        if self._catalog is None:
            return 0
        count = 0
        for rec in self._catalog.list_jobs(limit=1000):
            if rec.status in {JobStatus.RUNNING, JobStatus.QUEUED} and rec.job_id not in self._jobs:
                self._catalog.finish_job(
                    rec.job_id, JobStatus.FAILED, "Interrupted when the application closed"
                )
                count += 1
        return count
