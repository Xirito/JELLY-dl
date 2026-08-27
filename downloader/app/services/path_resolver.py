"""PathResolver — turns a raw user path into an absolute Path.

Rule:
  "$jellyfin$/shows/x"  -> <jellyfin base_path>/shows/x   (recognized token)
  "podcasts/y"          -> <downloader storage root>/podcasts/y
Escapes ("..", absolute paths) are rejected so nothing outside the
configured roots is ever reachable.
"""
from __future__ import annotations

import re
from pathlib import Path

from ..config import MEDIA_SERVER_TARGETS
from ..plugins.base import Downloader

_TOKEN_RE = re.compile(r"^/?\$(?P<token>[a-zA-Z0-9_-]+)\$/?(?P<rest>.*)$")


class PathEscapeError(ValueError):
    pass


def split_token(raw: str) -> tuple[str | None, str]:
    m = _TOKEN_RE.match(raw.strip())
    if m:
        return m.group("token").lower(), m.group("rest")
    return None, raw.strip().lstrip("/")


def _safe_join(base: Path, rel: str) -> Path:
    candidate = (base / rel).resolve() if rel else base.resolve()
    base_r = base.resolve()
    if candidate != base_r and base_r not in candidate.parents:
        raise PathEscapeError(f"path escapes its root: {rel!r}")
    return candidate


class PathResolver:
    def __init__(self, targets=None):
        self.targets = targets if targets is not None else MEDIA_SERVER_TARGETS

    def resolve(self, raw_path: str, downloader: Downloader) -> Path:
        token, rest = split_token(raw_path or "")
        if token:
            target = self.targets.get(token)
            if target is None:
                raise KeyError(f"unknown media-server token: ${token}$")
            return _safe_join(target.base_path, rest)
        return _safe_join(downloader.default_download_root, rest)
