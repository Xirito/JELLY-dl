"""Torrent search providers — plural on purpose.

NyaaTorDownloader (nyaa_tor_plugin.py) searches every provider in its list
and flattens the results, rather than being hardcoded to nyaa.si. Adding a
second indexer later is "write a class implementing TorrentProvider,
append an instance of it to the plugin's provider list" — nothing about
the plugin, the shared torrent client, or the rest of the app needs to
change, since every provider just needs to hand back a magnet link.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class TorrentSearchItem:
    title: str
    magnet: str
    size: str | None = None
    seeders: int | None = None
    leechers: int | None = None


class TorrentProvider(Protocol):
    name: str

    def search(self, query: str) -> list[TorrentSearchItem]: ...


def _to_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


class NyaaProvider:
    """nyaa.si, via the nyaapy scraper (no official API exists — see the
    note on nyaapy in requirements.txt on how fragile that makes this:
    HTML/RSS scraping breaks silently whenever the site's markup changes).
    """
    name = "Nyaa"

    def search(self, query: str) -> list[TorrentSearchItem]:
        from nyaapy.nyaasi.nyaa import Nyaa  # local import: only this provider needs it

        out: list[TorrentSearchItem] = []
        for t in Nyaa.search(query)[:30]:
            magnet = getattr(t, "magnet", None)
            if not magnet:
                continue  # no magnet, nothing to leech — skip rather than error
            out.append(TorrentSearchItem(
                title=getattr(t, "name", None) or "(untitled)",
                magnet=magnet,
                size=getattr(t, "size", None),
                seeders=_to_int(getattr(t, "seeders", None)),
                leechers=_to_int(getattr(t, "leechers", None)),
            ))
        return out
