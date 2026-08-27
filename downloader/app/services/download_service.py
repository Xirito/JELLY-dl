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
from ..registry import DownloaderRegistry
from .path_resolver import PathResolver

_MAX_JOBS_KEPT = 200


class DownloadService:
    def __init__(self, registry: DownloaderRegistry, resolver: PathResolver | None = None):
        self.registry = registry
        self.resolver = resolver or PathResolver()
        self._jobs: OrderedDict[str, JobInfo] = OrderedDict()
        self._lock = threading.Lock()

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
            while len(self._jobs) > _MAX_JOBS_KEPT:
                self._jobs.popitem(last=False)

        t = threading.Thread(target=self._run, args=(job.id, dl, req, dest), daemon=True)
        t.start()
        return job

    def get(self, job_id: str) -> JobInfo:
        with self._lock:
            return self._jobs[job_id]

    def all(self) -> list[JobInfo]:
        with self._lock:
            return list(reversed(self._jobs.values()))

    # -- internal ----------------------------------------------------------
    def _run(self, job_id: str, dl, req: DownloadRequest, dest) -> None:
        def on_progress(p: DownloadProgress) -> None:
            with self._lock:
                j = self._jobs.get(job_id)
                if j:
                    j.progress = p

        with self._lock:
            self._jobs[job_id].status = JobStatus.running

        title = None
        probe = getattr(dl, "probe_title", None)
        if callable(probe):
            title = probe(req.source)
            with self._lock:
                j = self._jobs.get(job_id)
                if j:
                    j.title = title

        result = dl.download(req.source, req.format_selector, dest, on_progress)

        with self._lock:
            j = self._jobs.get(job_id)
            if j:
                j.result = result
                j.status = JobStatus.error if result.error else JobStatus.finished
