"""Seasonal-anime follow tracker -- shared by BOTH downloader plugins
(nyaa_tor and ani-cli), which is why this lives here as its own service
rather than inside either plugin. A followed show is just a title, a
cover, and a weekday to put in the calendar; "search" from the calendar
(the frontend's job, not this module's) re-runs a lookup against
whichever downloader backend the user currently has selected -- this
module never touches torrents or streams itself.

Data source: originally asked for via livechart.me's RSS feeds, but those
only publish a "headlines" feed and a "recently aired episodes" feed --
neither has a season's lineup or air-day info, so there's nothing here to
build a calendar from. Using AniList's own seasonal Media query instead
(services/anilist.py) -- already integrated in this app for nyaa_tor's
search -- with MyAnimeList's official /anime/season endpoint
(services/mal.py) as a fallback if AniList's call fails and a Client ID
is configured. Same reasoning as nyaa_tor_plugin.py's own AniList->MAL
fallback: AniList's API can go down outage-wide on their end. Unlike that
plugin's sticky-primary machinery (built for a per-keystroke interactive
search), this is a plain try/except -- browsing a season is a low-
frequency action (once per page load or season change), so there's no
meaningful cost to just trying AniList again next time instead of
remembering which source worked last time.

Storage: a small SQLite db under DOWNLOAD_ROOT/.seasonal/seasonal.db --
same "dotfolder under the download root" convention
services/torrent_client.py already uses for its own persistent state, and
DOWNLOAD_ROOT is already a mounted, persistent volume (see
docker-compose.yml), so this needs no new volume. sqlite3 is stdlib, no
new dependency. Two tables:
  shows   -- cache of anything ever browsed or followed: id (namespaced
             "anilist:123" / "mal:456", exactly like NyaaTorDownloader's
             own anime ids -- see nyaa_tor_plugin.py), title, cover,
             day_of_week, season, year, updated_at.
  follows -- just which ids are currently followed, and when. Unfollowing
             never deletes the shows cache row (harmless to keep, and
             re-following the same id later is then instant, no re-fetch
             needed).
Followed shows' cached schedule is refreshed lazily and best-effort (see
_refresh_stale) -- there's no background poller, only a check on
list_followed() itself.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime

from ..config import DOWNLOAD_ROOT
from ..models import SeasonalShow
from ..services import anilist, mal

log = logging.getLogger(__name__)

_DB_PATH = DOWNLOAD_ROOT / ".seasonal" / "seasonal.db"
_STALE_AFTER_S = 6 * 3600  # re-check a followed show's schedule at most this often

_SEASON_MONTHS = {
    1: "WINTER", 2: "WINTER", 3: "WINTER",
    4: "SPRING", 5: "SPRING", 6: "SPRING",
    7: "SUMMER", 8: "SUMMER", 9: "SUMMER",
    10: "FALL", 11: "FALL", 12: "FALL",
}
_SEASON_ORDER = ["WINTER", "SPRING", "SUMMER", "FALL"]


class SeasonalError(RuntimeError):
    pass


def current_season() -> tuple[str, int]:
    today = date.today()
    return _SEASON_MONTHS[today.month], today.year


def adjacent_season(season: str, year: int, delta: int) -> tuple[str, int]:
    """+1/-1 season, rolling the year at WINTER/FALL -- used by the
    frontend's prev/next season browse controls so it never has to know
    the WINTER/SPRING/SUMMER/FALL ordering itself. Python's `//` and `%`
    both floor toward negative infinity, so this rolls the year correctly
    in both directions with no special-casing for a negative index."""
    idx = _SEASON_ORDER.index(season.upper()) + delta
    return _SEASON_ORDER[idx % 4], year + idx // 4


# Guards every sqlite3 connection below -- these routes run through
# FastAPI's threadpool (plain `def`s, see main.py) same as
# nyaa_tor_plugin.py's, so concurrent requests are real, not theoretical.
# A single lock around the whole db file is simpler than per-row locking
# and costs nothing in practice: this is a handful of follow/browse calls,
# not a per-keystroke path.
_lock = threading.Lock()


def _weekday_from_airing_at(ts: int | None) -> int | None:
    # datetime.fromtimestamp() with no explicit tz converts a UTC Unix
    # timestamp straight into the server's local naive datetime -- exactly
    # the "local calendar day" this needs, in one step.
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(ts).weekday()
    except (OverflowError, OSError, ValueError):
        return None


@contextmanager
def _conn():
    with _lock:
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(_DB_PATH, check_same_thread=False)
        try:
            con.execute("PRAGMA foreign_keys = ON")
            con.execute("""
                CREATE TABLE IF NOT EXISTS shows (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    cover TEXT,
                    day_of_week INTEGER,
                    season TEXT,
                    year INTEGER,
                    updated_at INTEGER NOT NULL
                )
            """)
            con.execute("""
                CREATE TABLE IF NOT EXISTS follows (
                    id TEXT PRIMARY KEY REFERENCES shows(id) ON DELETE CASCADE,
                    followed_at INTEGER NOT NULL
                )
            """)
            yield con
            con.commit()
        finally:
            con.close()


def _upsert_show(con: sqlite3.Connection, show_id: str, title: str, cover: str | None,
                  day_of_week: int | None, season: str, year: int, now: int) -> None:
    con.execute(
        """INSERT INTO shows (id, title, cover, day_of_week, season, year, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             title=excluded.title, cover=excluded.cover, day_of_week=excluded.day_of_week,
             season=excluded.season, year=excluded.year, updated_at=excluded.updated_at""",
        (show_id, title, cover, day_of_week, season.upper(), year, now),
    )


def _fetch_season(season: str, year: int) -> list[tuple[str, str, str | None, int | None]]:
    """(id, title, cover, day_of_week) tuples, id already namespaced. Tries
    AniList first, falls back to MAL only if AniList raises AND
    mal.is_configured() -- see module docstring for why this is a plain
    try/except rather than nyaa_tor_plugin.py's sticky-primary pattern."""
    try:
        entries = anilist.seasonal_anime(season, year)
        return [
            (f"anilist:{e.id}", e.title, e.cover, _weekday_from_airing_at(e.next_airing_at))
            for e in entries
        ]
    except Exception as e1:
        log.warning("AniList seasonal lookup failed (%s %d): %s", season, year, e1)
        if not mal.is_configured():
            raise SeasonalError(f"AniList seasonal lookup failed: {e1}") from e1
        try:
            entries = mal.seasonal_anime(season, year)
            return [(f"mal:{e.id}", e.title, e.cover, e.day_of_week) for e in entries]
        except Exception as e2:
            raise SeasonalError(f"AniList seasonal lookup failed ({e1}); MAL fallback also failed: {e2}") from e2


def browse_season(season: str, year: int) -> list[SeasonalShow]:
    season = season.upper()
    rows = _fetch_season(season, year)
    now = int(time.time())
    with _conn() as con:
        followed_ids = {r[0] for r in con.execute("SELECT id FROM follows")}
        for show_id, title, cover, dow in rows:
            _upsert_show(con, show_id, title, cover, dow, season, year, now)
    return [
        SeasonalShow(id=r[0], title=r[1], cover=r[2], day_of_week=r[3], followed=r[0] in followed_ids)
        for r in rows
    ]


def follow(show_id: str, title: str, cover: str | None, day_of_week: int | None, season: str, year: int) -> None:
    now = int(time.time())
    with _conn() as con:
        _upsert_show(con, show_id, title, cover, day_of_week, season, year, now)
        con.execute("INSERT OR IGNORE INTO follows (id, followed_at) VALUES (?, ?)", (show_id, now))


def unfollow(show_id: str) -> None:
    with _conn() as con:
        con.execute("DELETE FROM follows WHERE id = ?", (show_id,))


def _refresh_stale(rows: list[sqlite3.Row], now: int) -> None:
    """Best-effort: re-run browse_season() once per distinct (season,
    year) among stale followed shows -- not once per show -- so a mid-
    cours schedule change (a delay, a new next-episode time) eventually
    shows up without a background poller. Any failure here just means the
    existing cached rows keep being served; never raises."""
    stale_seasons: set[tuple[str, int]] = set()
    for r in rows:
        if r["season"] and r["year"] is not None and (now - r["updated_at"]) > _STALE_AFTER_S:
            stale_seasons.add((r["season"], r["year"]))
    for season, year in stale_seasons:
        try:
            browse_season(season, year)
        except Exception as e:
            log.warning("seasonal refresh failed for %s %d: %s", season, year, e)


def _followed_rows() -> list[sqlite3.Row]:
    with _conn() as con:
        con.row_factory = sqlite3.Row
        return con.execute(
            """SELECT shows.* FROM shows
               JOIN follows ON follows.id = shows.id
               ORDER BY follows.followed_at ASC"""
        ).fetchall()


def list_followed() -> list[SeasonalShow]:
    now = int(time.time())
    rows = _followed_rows()
    # _refresh_stale() runs its own browse_season() calls, each opening its
    # own _conn() -- that MUST happen after the _conn() block above has
    # already exited (releasing _lock), or a stale-refresh would deadlock
    # against itself on this thread. _followed_rows() returning (not
    # yielding) before this line is what guarantees that.
    if any(r["season"] and r["year"] is not None and (now - r["updated_at"]) > _STALE_AFTER_S for r in rows):
        _refresh_stale(rows, now)
        rows = _followed_rows()
    return [
        SeasonalShow(id=r["id"], title=r["title"], cover=r["cover"], day_of_week=r["day_of_week"], followed=True)
        for r in rows
    ]
