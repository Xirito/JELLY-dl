"""The core abstraction: the Downloader interface.

Nothing above the plugin layer may import anything backend-specific.
Swapping in ani-cli later means implementing this Protocol, nothing more.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol, runtime_checkable

from ..models import (
    DownloaderCapabilities,
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

    def search(self, query: str) -> list[SearchResult]: ...

    def list_formats(self, source: str) -> list[FormatOption]: ...

    def download(
        self,
        source: str,
        format_selector: FormatSelector,
        destination: Path,
        on_progress: Callable[[DownloadProgress], None],
    ) -> DownloadResult: ...
