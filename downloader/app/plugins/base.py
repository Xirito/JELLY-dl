"""The core abstraction: the Downloader interface.

Nothing above the plugin layer may import anything backend-specific.
Swapping in ani-cli later means implementing this Protocol, nothing more.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol, runtime_checkable

from ..models import (
    DownloaderCapabilities,
    DownloadOptions,
    DownloadProgress,
    DownloadResult,
    FormatOption,
    FormatSelector,
    SearchResult,
)


@runtime_checkable
class Downloader(Protocol):
    id: str
    name: str
    capabilities: DownloaderCapabilities
    default_download_root: Path

    def search(self, query: str, parent: str | None = None) -> list[SearchResult]: ...
    # `parent`: when set, `query` may be ignored — it means "list the items
    # inside this container" (see SearchResult.is_container). Backends that
    # never return containers can ignore the parameter entirely.

    def list_formats(self, source: str) -> list[FormatOption]: ...

    def download(
        self,
        source: str,
        format_selector: FormatSelector,
        destination: Path,
        on_progress: Callable[[DownloadProgress], None],
        options: DownloadOptions | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> DownloadResult: ...
    # `should_cancel`: polled from inside the download loop (progress hooks
    # are a natural place) -- when it returns True the implementation should
    # stop as soon as practical and return. The service, not the plugin,
    # decides the resulting job status; plugins just need to stop early.
