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
    date: str | None = None
    downloads: int | None = None


# Sort keys the frontend can ask for -> (nyaa.si's `s=` column, `o=` order).
# Sorting happens on the indexer's side rather than over whatever came back,
# so "most seeders" finds the best-seeded release among *every* match, not
# just the best of the newest 30. A provider that can't sort server-side can
# still honour these locally off the TorrentSearchItem fields above.
SORT_OPTIONS: dict[str, tuple[str, str]] = {
    "newest": ("id", "desc"),
    "oldest": ("id", "asc"),
    "seeders": ("seeders", "desc"),
    "leechers": ("leechers", "desc"),
    "downloads": ("downloads", "desc"),
    "size_asc": ("size", "asc"),
    "size_desc": ("size", "desc"),
}
DEFAULT_SORT = "newest"


class TorrentProvider(Protocol):
    name: str

    def search(self, query: str, sort: str = DEFAULT_SORT) -> list[TorrentSearchItem]: ...


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

    url = "https://nyaa.si/"

    def search(self, query: str, sort: str = DEFAULT_SORT) -> list[TorrentSearchItem]:
        # local imports: only this provider needs them
        import requests
        from nyaapy.parser import parse_nyaa
        from nyaapy.torrent import TorrentSite

        # Not Nyaa.search(): nyaapy 0.7 fetches the RSS feed (&page=rss),
        # which ignores s=/o= and has no download counts. The HTML listing
        # honours both, and nyaapy's own HTML parser reads it.
        column, order = SORT_OPTIONS[sort]
        resp = requests.get(
            self.url,
            params={"f": 0, "c": "0_0", "q": query, "s": column, "o": order},
            timeout=20,
        )
        resp.raise_for_status()
        rows = parse_nyaa(request_text=resp.content, limit=None, site=TorrentSite.NYAASI)

        out: list[TorrentSearchItem] = []
        for t in rows[:30]:
            magnet = t.get("magnet")
            if not magnet:
                continue  # no magnet, nothing to leech — skip rather than error
            out.append(TorrentSearchItem(
                title=t.get("name") or "(untitled)",
                magnet=magnet,
                size=t.get("size"),
                seeders=_to_int(t.get("seeders")),
                leechers=_to_int(t.get("leechers")),
                date=t.get("date"),
                downloads=_to_int(t.get("completed_downloads")),
            ))
        return out
