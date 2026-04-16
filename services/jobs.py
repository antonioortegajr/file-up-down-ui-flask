"""
In-process job queue using Python threading.

Provides queue_job(fn, *args) which returns a job ID.
Each job tracks: id, status, progress, log, result.
Use get_job(id) to retrieve job state, or stream_progress(id) to get updates.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterator


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class Job:
    id: str
    status: JobStatus = JobStatus.QUEUED
    progress: int = 0
    log: list[str] = field(default_factory=list)
    result: Any = None
    error: str | None = None
    current_file: str | None = None
    answer: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def set_progress(self, value: int, message: str = "", current_file: str | None = None, answer: str | None = None) -> None:
        with self._lock:
            self.progress = min(100, max(0, value))
            if current_file is not None:
                self.current_file = current_file
            if answer is not None:
                self.answer = answer
            if message:
                self.log.append(message)

    def set_status(self, status: JobStatus) -> None:
        with self._lock:
            self.status = status

    def set_result(self, result: Any) -> None:
        with self._lock:
            self.result = result
            self.status = JobStatus.DONE
            self.progress = 100

    def set_error(self, error: str) -> None:
        with self._lock:
            self.error = error
            self.status = JobStatus.FAILED

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "id": self.id,
                "status": self.status.value,
                "progress": self.progress,
                "log": list(self.log),
                "result": self.result,
                "error": self.error,
                "current_file": self.current_file,
                "answer": self.answer,
            }


_jobs: dict[str, Job] = {}
_jobs_lock = threading.Lock()
_executor = threading.Thread(target=lambda: None)
_executor.daemon = True


def queue_job(fn: Callable, *args, **kwargs) -> str:
    """
    Queue a job to run in a background thread.
    Returns the job ID.
    """
    job_id = str(uuid.uuid4())[:8]
    job = Job(id=job_id)
    with _jobs_lock:
        _jobs[job_id] = job

    def run():
        job.set_status(JobStatus.RUNNING)
        try:
            result = fn(job, *args, **kwargs)
            job.set_result(result)
        except Exception as exc:
            job.set_error(str(exc))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return job_id


def get_job(job_id: str) -> Job | None:
    """Return the Job object for the given ID, or None if not found."""
    with _jobs_lock:
        return _jobs.get(job_id)


def stream_progress(job_id: str) -> Iterator[dict]:
    """Yield progress updates for a job as dicts (for SSE)."""
    seen_indices: dict[str, int] = {job_id: 0}
    last_status = None

    while True:
        job = get_job(job_id)
        if job is None:
            return

        with job._lock:
            current_status = job.status
            current_log = list(job.log)
            current_progress = job.progress
            current_result = job.result
            current_error = job.error

        if current_status in (JobStatus.DONE, JobStatus.FAILED):
            yield {
                "status": current_status.value,
                "progress": current_progress,
                "message": "",
                "result": current_result,
                "error": current_error,
                "current_file": job.current_file,
                "answer": job.answer,
            }
            return

        start_idx = seen_indices.get(job_id, 0)
        for i in range(start_idx, len(current_log)):
            yield {
                "status": current_status.value,
                "progress": current_progress,
                "message": current_log[i],
                "current_file": job.current_file,
                "answer": job.answer,
            }
        seen_indices[job_id] = len(current_log)

        if current_status != last_status:
            last_status = current_status

        time.sleep(0.1)
