"""Configuration — media-server targets and downloader storage roots.

Everything comes from environment variables so the docker-compose file is the
single source of truth:

  MEDIA_TARGETS      comma-separated token=path pairs, e.g. "jellyfin=/media"
  DOWNLOAD_ROOT      default storage root for downloaders, e.g. "/downloads"
"""
from __future__ import annotations

import os
from pathlib import Path

from .models import MediaServerTarget


def load_media_targets() -> dict[str, MediaServerTarget]:
    raw = os.environ.get("MEDIA_TARGETS", "")
    targets: dict[str, MediaServerTarget] = {}
    for pair in filter(None, (p.strip() for p in raw.split(","))):
        token, _, path = pair.partition("=")
        token = token.strip().strip("$").lower()
        if token and path:
            targets[token] = MediaServerTarget(token=token, base_path=Path(path.strip()))
    return targets


MEDIA_SERVER_TARGETS: dict[str, MediaServerTarget] = load_media_targets()
DOWNLOAD_ROOT: Path = Path(os.environ.get("DOWNLOAD_ROOT", "/downloads"))
