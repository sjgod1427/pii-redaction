"""Job store for the web front end.

A redaction run takes tens of seconds, so the browser cannot wait on a single
request.  Each upload becomes a job that runs on a worker thread and publishes
progress events; the page subscribes to those over SSE.

Everything about this module is deliberately short-lived.  Uploaded documents
routinely contain exactly the data the tool exists to remove, so they live in a
per-job temporary directory, are never written anywhere else, are never logged,
and are deleted when the job expires.  Nothing is persisted between restarts.
"""

from __future__ import annotations

import queue
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

#: How long a finished job (and its files) survives before being deleted.
JOB_TTL_SECONDS = 15 * 60

#: Largest upload accepted.  A .docx is compressed XML; anything past this is
#: either not a document or not something a free-tier container should attempt.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024


@dataclass
class Event:
    """One progress update, streamed to the browser as SSE."""

    kind: str          # "stage" | "done" | "error"
    payload: dict = field(default_factory=dict)


@dataclass
class Job:
    id: str
    directory: Path
    created: float = field(default_factory=time.time)
    finished: bool = False
    failed: str = ""
    result: dict = field(default_factory=dict)
    _events: "queue.Queue[Event]" = field(default_factory=queue.Queue)
    _history: list[Event] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def emit(self, kind: str, **payload) -> None:
        event = Event(kind, payload)
        with self._lock:
            self._history.append(event)
        self._events.put(event)

    def history(self) -> list[Event]:
        with self._lock:
            return list(self._history)

    def next_event(self, timeout: float = 1.0) -> Event | None:
        try:
            return self._events.get(timeout=timeout)
        except queue.Empty:
            return None

    def path(self, name: str) -> Path:
        return self.directory / name

    def expired(self, now: float | None = None) -> bool:
        return (now or time.time()) - self.created > JOB_TTL_SECONDS


class JobStore:
    """In-memory registry of running and recently-finished jobs."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._root = Path(tempfile.mkdtemp(prefix="pii-redactor-"))

    def create(self) -> Job:
        self.sweep()
        job_id = uuid.uuid4().hex
        directory = self._root / job_id
        directory.mkdir(parents=True, exist_ok=True)
        job = Job(id=job_id, directory=directory)
        with self._lock:
            self._jobs[job_id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
        if job and job.expired():
            self.discard(job_id)
            return None
        return job

    def discard(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.pop(job_id, None)
        if job is not None:
            shutil.rmtree(job.directory, ignore_errors=True)

    def sweep(self) -> None:
        """Delete expired jobs and their uploaded documents."""
        now = time.time()
        with self._lock:
            stale = [jid for jid, job in self._jobs.items() if job.expired(now)]
        for job_id in stale:
            self.discard(job_id)

    def shutdown(self) -> None:
        with self._lock:
            job_ids = list(self._jobs)
        for job_id in job_ids:
            self.discard(job_id)
        shutil.rmtree(self._root, ignore_errors=True)
