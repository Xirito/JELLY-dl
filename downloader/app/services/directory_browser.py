"""DirectoryBrowser — autofill suggestions for destination paths.

Works on whatever Path the resolver hands back; doesn't care whether the
base is a media-server root or a downloader's own storage root.
"""
from __future__ import annotations

from pathlib import Path

from ..config import MEDIA_SERVER_TARGETS
from ..plugins.base import Downloader
from .path_resolver import PathResolver, split_token


class DirectoryBrowser:
    def __init__(self, resolver: PathResolver | None = None):
        self.resolver = resolver or PathResolver()

    def list_existing(self, base: Path, prefix: str) -> list[str]:
        if not base.is_dir():
            return []
        pl = prefix.lower()
        try:
            return sorted(
                d.name for d in base.iterdir()
                if d.is_dir() and not d.name.startswith(".")
                and d.name.lower().startswith(pl)
            )[:25]
        except PermissionError:
            return []

    def suggest(self, raw_path: str, downloader: Downloader) -> list[str]:
        """Given the raw path as typed (may include $token$), return full
        raw-path suggestions for the last segment."""
        raw = raw_path or ""
        token, rest = split_token(raw)
        parts = rest.split("/") if rest else [""]
        parent_rel, last = "/".join(parts[:-1]), parts[-1]

        prefix_raw = (f"${token}$/" if token else "") + (parent_rel + "/" if parent_rel else "")
        try:
            base = self.resolver.resolve(prefix_raw if prefix_raw else "", downloader)
        except Exception:
            return []
        return [prefix_raw + name for name in self.list_existing(base, last)]

    @staticmethod
    def tokens() -> list[str]:
        return [f"${t}$" for t in MEDIA_SERVER_TARGETS]
