"""DownloadService — job orchestration. Downloader-agnostic."""
from __future__ import annotations

import threading
import uuid
from collections import OrderedDict

from ..models import (
    DownloadProgress,
    DownloadRequest,
    JobInfo,
    JobStatus,
)
from ..config import MEDIA_SERVER_TARGETS
from ..registry import DownloaderRegistry
from .notifier import build_notifiers
from .path_resolver import PathResolver, split_token

_MAX_JOBS_KEPT = 200


class DownloadService:
    def __init__(self, registry: DownloaderRegistry, resolver: PathResolver | None = None):
        self.registry = registry
        self.resolver = resolver or PathResolver()
        self._jobs: OrderedDict[str, JobInfo] = OrderedDict()
        # One cancellation flag per job, polled by the plugin's download loop
        # (progress hooks) via a should_cancel() closure. Not part of JobInfo
        # itself — this is orchestration state, not a DTO the API returns.
        self._cancel_flags: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self.notifiers = build_notifiers(list(MEDIA_SERVER_TARGETS))

    # -- public ------------------------------------------------------------
    def start(self, req: DownloadRequest) -> JobInfo:
        dl = self.registry.get(req.downloader_id)          # KeyError -> 404
        dest = self.resolver.resolve(req.destination_path, dl)  # may raise

        job = JobInfo(
            id=uuid.uuid4().hex[:12],
            downloader_id=dl.id,
            source=req.source,
            destination=str(dest),
            status=JobStatus.queued,
            progress=DownloadProgress(),
        )
        with self._lock:
            self._jobs[job.id] = job
            self._cancel_flags[job.id] = threading.Event()
            while len(self._jobs) > _MAX_JOBS_KEPT:
                evicted_id, _ = self._jobs.popitem(last=False)
                self._cancel_flags.pop(evicted_id, None)

        t = threading.Thread(target=self._run, args=(job.id, dl, req, dest), daemon=True)
        t.start()
        return job

    def get(self, job_id: str) -> JobInfo:
        with self._lock:
            return self._jobs[job_id]

    def all(self) -> list[JobInfo]:
        with self._lock:
            return list(reversed(self._jobs.values()))

    def cancel(self, job_id: str) -> JobInfo:
        with self._lock:
            job = self._jobs[job_id]  # KeyError -> 404
            if job.status in (JobStatus.finished, JobStatus.error, JobStatus.cancelled):
                return job  # already done — nothing to cancel
            event = self._cancel_flags.get(job_id)
            if event:
                event.set()
            # Final status transition stays owned by _run() (the thread
            # actually driving the download) to avoid a race where this
            # marks the job cancelled just as it was about to finish
            # legitimately. This is just an immediate UI hint.
            job.progress.status = "cancelling"
            return job

    # -- internal ----------------------------------------------------------
    def _run(self, job_id: str, dl, req: DownloadRequest, dest) -> None:
        def on_progress(p: DownloadProgress) -> None:
            with self._lock:
                j = self._jobs.get(job_id)
                if j:
                    j.progress = p

        with self._lock:
            self._jobs[job_id].status = JobStatus.running
            event = self._cancel_flags.get(job_id) or threading.Event()

        title = None
        probe = getattr(dl, "probe_title", None)
        if callable(probe):
            title = probe(req.source)
            with self._lock:
                j = self._jobs.get(job_id)
                if j:
                    j.title = title

        result = dl.download(req.source, req.format_selector, dest, on_progress,
                             req.options, should_cancel=event.is_set)

        with self._lock:
            j = self._jobs.get(job_id)
            if j:
                j.result = result
                if event.is_set():
                    j.status = JobStatus.cancelled
                else:
                    j.status = JobStatus.error if result.error else JobStatus.finished
            self._cancel_flags.pop(job_id, None)

        # Best-effort: tell the media server to rescan when a download landed
        # under one of its $token$ paths. Never fails the job.
        if not result.error:
            token, _ = split_token(req.destination_path or "")
            notifier = self.notifiers.get(token) if token else None
            if notifier:
                try:
                    notifier.refresh()
                except Exception:
                    pass
