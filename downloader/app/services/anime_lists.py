"""Fribb/anime-lists, indexed for season placement (services/season_resolver.py).

The same community dataset services/alt_titles.py already downloads to
bridge ids, but this module keeps a different slice of it: for every
AniList/MAL/AniDB entry, which TVDB series it belongs to, which TVDB season
it is, and (for split cours) the TVDB episode offset -- e.g. Attack on
Titan "Final Season Part 2" is TVDB season 4 starting after episode 16.
That's the numbering Jellyfin's TVDB metadata uses, which is why it beats
parsing "2nd Season"/"Season 2"/"II" out of a title, and it also covers
sequels whose titles carry no number at all (Demon Slayer's "Entertainment
District Arc" is TVDB season 3).

Own cache, not alt_titles'. That one lives in /tmp (gone on every container
restart) and is rewritten in place, so reading it while a post-download
alias pass rewrites it could see half a file. This copy sits under
DOWNLOAD_ROOT/.seasonal/ next to seasonal.db (a persistent volume) and is
replaced atomically.

Refreshed weekly in the background -- a stale copy keeps serving meanwhile,
and a failed download just means trying again a bit later. Only the very
first load (no cache on disk yet) blocks on the download. If that fails,
get() returns None and the resolver falls back to guessing.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from ..config import DOWNLOAD_ROOT
from . import alt_titles

log = logging.getLogger(__name__)

_CACHE = DOWNLOAD_ROOT / ".seasonal" / "anime-lists.json"
_MAX_AGE_S = 7 * 86400
_RETRY_AFTER_FAIL_S = 15 * 60


@dataclass(frozen=True)
class Entry:
    anilist_id: int | None
    mal_id: int | None
    anidb_id: int | None
    tvdb_id: int | None
    tvdb_season: int | None
    episode_offset: int | None  # TVDB episodes of this entry start after this many
    type: str | None            # "TV", "MOVIE", "OVA", "ONA", "SPECIAL", ...


class AnimeLists:
    def __init__(self, entries: list[Entry]):
        self.by_anilist: dict[int, Entry] = {}
        self.by_mal: dict[int, Entry] = {}
        self.by_anidb: dict[int, Entry] = {}
        self.by_tvdb: dict[int, list[Entry]] = {}
        for e in entries:
            if e.anilist_id is not None:
                self.by_anilist.setdefault(e.anilist_id, e)
            if e.mal_id is not None:
                self.by_mal.setdefault(e.mal_id, e)
            if e.anidb_id is not None:
                self.by_anidb.setdefault(e.anidb_id, e)
            if e.tvdb_id is not None:
                self.by_tvdb.setdefault(e.tvdb_id, []).append(e)

    def lookup(self, anilist_id: int | None = None, mal_id: int | None = None) -> Entry | None:
        e = self.by_anilist.get(anilist_id) if anilist_id is not None else None
        if e is None and mal_id is not None:
            e = self.by_mal.get(mal_id)
        return e

    def series(self, tvdb_id: int | None) -> list[Entry]:
        """Every entry (all seasons, cours, specials) sharing one TVDB show."""
        return list(self.by_tvdb.get(tvdb_id, [])) if tvdb_id is not None else []


def _int(v) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return None


def _tvdb_field(v) -> int | None:
    # season / episode_offset are {"tvdb": n, "tmdb": n} dicts
    return _int(v.get("tvdb")) if isinstance(v, dict) else None


def _parse(raw: bytes) -> AnimeLists:
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("anime-lists: expected a JSON array")
    entries = []
    for d in data:
        if not isinstance(d, dict):
            continue
        entries.append(Entry(
            anilist_id=_int(d.get("anilist_id")),
            mal_id=_int(d.get("mal_id")),
            anidb_id=_int(d.get("anidb_id")),
            # older dumps called it thetvdb_id
            tvdb_id=_int(d.get("tvdb_id", d.get("thetvdb_id"))),
            tvdb_season=_tvdb_field(d.get("season")),
            episode_offset=_tvdb_field(d.get("episode_offset")),
            type=d.get("type") if isinstance(d.get("type"), str) else None,
        ))
    return AnimeLists(entries)


def _download() -> bytes:
    req = urllib.request.Request(alt_titles.FRIBB_URL, headers={"User-Agent": alt_titles.UA})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def _write_atomic(raw: bytes) -> None:
    _CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _CACHE.with_name(_CACHE.name + ".tmp")
    tmp.write_bytes(raw)
    os.replace(tmp, _CACHE)


_lock = threading.Lock()      # guards the module state below
_dl_lock = threading.Lock()   # one download at a time
_data: AnimeLists | None = None
_refreshing = False
_last_fail = 0.0


def _fetch_and_store() -> AnimeLists | None:
    """Download, validate (parse) BEFORE replacing the cache, then swap in."""
    global _data, _last_fail
    with _dl_lock:
        try:
            raw = _download()
            parsed = _parse(raw)
            _write_atomic(raw)
        except Exception as e:  # network, bad JSON, disk -- keep whatever we had
            log.warning("anime-lists download failed: %s", e)
            with _lock:
                _last_fail = time.time()
            return None
        log.info("anime-lists refreshed: %d AniList ids, %d TVDB shows",
                 len(parsed.by_anilist), len(parsed.by_tvdb))
        with _lock:
            _data = parsed
        return parsed


def _background_refresh() -> None:
    global _refreshing
    try:
        _fetch_and_store()
    finally:
        with _lock:
            _refreshing = False


def get() -> AnimeLists | None:
    global _data, _refreshing
    with _lock:
        data = _data
    if data is None and _CACHE.exists():
        try:
            data = _parse(_CACHE.read_bytes())
            with _lock:
                if _data is None:
                    _data = data
                data = _data
        except Exception as e:
            log.warning("anime-lists cache unreadable (%s); re-downloading", e)
            data = None
    if data is None:
        with _lock:
            recently_failed = time.time() - _last_fail < _RETRY_AFTER_FAIL_S
        return None if recently_failed else _fetch_and_store()

    try:
        stale = time.time() - _CACHE.stat().st_mtime > _MAX_AGE_S
    except OSError:
        stale = True
    with _lock:
        start = stale and not _refreshing and time.time() - _last_fail > _RETRY_AFTER_FAIL_S
        if start:
            _refreshing = True
    if start:
        threading.Thread(target=_background_refresh, name="anime-lists-refresh", daemon=True).start()
    return data
