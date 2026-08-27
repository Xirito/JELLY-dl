"""Media-server notifiers — ping a server to rescan after a download lands.

Configured per media-server token via env (docker-compose):

  NOTIFY_<TOKEN>_URL      e.g. NOTIFY_JELLYFIN_URL=http://jellyfin:8096
  NOTIFY_<TOKEN>_APIKEY   an API key created in that server's dashboard

Only Jellyfin is implemented today; the Protocol keeps it open for others.
Notification is best-effort: a failure never fails the download job.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Protocol

log = logging.getLogger("notifier")


class MediaServerNotifier(Protocol):
    def refresh(self) -> None: ...


class JellyfinNotifier:
    def __init__(self, url: str, api_key: str):
        self.url = url.rstrip("/")
        self.api_key = api_key

    def refresh(self) -> None:
        req = urllib.request.Request(
            f"{self.url}/Library/Refresh",
            method="POST",
            headers={
                "Authorization": f'MediaBrowser Token="{self.api_key}"',
                "Content-Type": "application/json",
            },
            data=json.dumps({}).encode(),
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status >= 300:
                raise RuntimeError(f"refresh returned HTTP {resp.status}")


def build_notifiers(tokens: list[str]) -> dict[str, MediaServerNotifier]:
    """One notifier per configured media-server token that has env creds."""
    notifiers: dict[str, MediaServerNotifier] = {}
    for token in tokens:
        prefix = f"NOTIFY_{token.upper()}_"
        url = os.environ.get(prefix + "URL")
        key = os.environ.get(prefix + "APIKEY")
        if url and key:
            notifiers[token] = JellyfinNotifier(url, key)
            log.info("library-refresh notifier active for $%s$", token)
    return notifiers
