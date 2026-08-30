"""DownloaderRegistry — id -> Downloader instance map."""
from __future__ import annotations

from .plugins.anicli_plugin import AniCliDownloader
from .plugins.base import Downloader
from .plugins.ytdlp_plugin import YtdlpDownloader


class DownloaderRegistry:
    def __init__(self):
        self._by_id: dict[str, Downloader] = {}

    def register(self, dl: Downloader) -> None:
        self._by_id[dl.id] = dl

    def get(self, downloader_id: str) -> Downloader:
        try:
            return self._by_id[downloader_id]
        except KeyError:
            raise KeyError(f"unknown downloader: {downloader_id}")

    def all(self) -> list[Downloader]:
        return list(self._by_id.values())


def build_default_registry() -> DownloaderRegistry:
    reg = DownloaderRegistry()
    reg.register(YtdlpDownloader())
    reg.register(AniCliDownloader())
    return reg
