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
from dataclasses import dataclass
from datetime import date, datetime

from ..config import DOWNLOAD_ROOT
from ..models import SeasonalShow
from ..services import anilist, mal

log = logging.getLogger(__name__)

_DB_PATH = DOWNLOAD_ROOT / ".seasonal" / "seasonal.db"
_STALE_AFTER_S = 6 * 3600  # re-check a followed show's schedule at most this often
# A full season is now several AniList requests (all pages, plus everything
# airing right now for the current season) -- cache it so flipping between
# tabs/seasons and the stale-refresh below don't refetch it each time.
_BROWSE_TTL_S = 30 * 60

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
                    followed_at INTEGER NOT NULL,
                    day_override INTEGER
                )
            """)
            # day_override: the weekday the user placed a followed show on
            # (set_day()). Lives on `follows`, not `shows`, so the schedule
            # refresh -- which rewrites `shows` rows from AniList -- never
            # undoes it. Added after the first release, hence the migration.
            if "day_override" not in {r[1] for r in con.execute("PRAGMA table_info(follows)")}:
                con.execute("ALTER TABLE follows ADD COLUMN day_override INTEGER")
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


@dataclass
class _Row:
    id: str            # namespaced, "anilist:123" / "mal:456"
    title: str
    cover: str | None
    day: int | None
    continuing: bool = False


def _fetch_season(season: str, year: int) -> list[_Row]:
    """Every show for the browse view, id already namespaced. Tries AniList
    first, falls back to MAL only if AniList raises AND mal.is_configured()
    -- see module docstring for why this is a plain try/except rather than
    nyaa_tor_plugin.py's sticky-primary pattern.

    For the current season, shows that started earlier but are still airing
    are merged in after this season's own (continuing=True): AniList files
    a show under its start season only, so without this a second cour or a
    long-runner is simply missing. Best-effort -- if that extra lookup fails,
    this season's own shows still come back."""
    try:
        entries = anilist.seasonal_anime(season, year)
    except Exception as e1:
        log.warning("AniList seasonal lookup failed (%s %d): %s", season, year, e1)
        if not mal.is_configured():
            raise SeasonalError(f"AniList seasonal lookup failed: {e1}") from e1
        try:
            entries = mal.seasonal_anime(season, year)
        except Exception as e2:
            raise SeasonalError(f"AniList seasonal lookup failed ({e1}); MAL fallback also failed: {e2}") from e2
        return [_Row(f"mal:{e.id}", e.title, e.cover, e.day_of_week) for e in entries]

    rows = [_Row(f"anilist:{e.id}", e.title, e.cover, _weekday_from_airing_at(e.next_airing_at)) for e in entries]
    if (season, year) == current_season():
        have = {r.id for r in rows}
        try:
            for e in anilist.airing_now():
                rid = f"anilist:{e.id}"
                if rid not in have:
                    have.add(rid)
                    rows.append(_Row(rid, e.title, e.cover, _weekday_from_airing_at(e.next_airing_at), True))
        except Exception as e:
            log.warning("AniList airing-now lookup failed; showing only shows that started this season: %s", e)
    return rows


_browse_lock = threading.Lock()
_browse_cache: dict[tuple[str, int], tuple[float, list[_Row]]] = {}


def browse_season(season: str, year: int) -> list[SeasonalShow]:
    season = season.upper()
    key = (season, year)
    with _browse_lock:
        hit = _browse_cache.get(key)
    fresh = not (hit and time.time() < hit[0])
    if fresh:
        rows = _fetch_season(season, year)
        with _browse_lock:
            _browse_cache[key] = (time.time() + _BROWSE_TTL_S, rows)
    else:
        rows = hit[1]
    now = int(time.time())
    with _conn() as con:
        followed_ids = {r[0] for r in con.execute("SELECT id FROM follows")}
        if fresh:
            # Continuing shows are stored under the season they were browsed
            # in, so the stale-refresh re-reads them from this same (cached)
            # listing instead of fetching their years-old start season.
            for r in rows:
                _upsert_show(con, r.id, r.title, r.cover, r.day, season, year, now)
    return [
        SeasonalShow(id=r.id, title=r.title, cover=r.cover, day_of_week=r.day,
                     followed=r.id in followed_ids, continuing=r.continuing)
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


def follow_anime(anime_id: str | None, title: str) -> SeasonalShow:
    """"Add to schedule" from a search result: look the show up (cover,
    airing day, season) and follow it. `anime_id` is a nyaa pick's
    "anilist:123"/"mal:456"; without one (ani-cli picks) the title goes to
    AniList's own best-match search -- the returned show says what was
    actually added, so a wrong match is visible (and removable) right away.
    Raises SeasonalError when nothing can be found or looked up."""
    source, _, raw = (anime_id or "").partition(":")
    try:
        if source == "mal" and raw.isdigit():
            e = mal.schedule_info(int(raw))
            show = (f"mal:{e.id}", e.title, e.cover, e.day_of_week, e.season, e.season_year) if e else None
        else:
            if source == "anilist" and raw.isdigit():
                e = anilist.schedule_info(media_id=int(raw))
            else:
                e = anilist.schedule_info(search=title)
            show = (f"anilist:{e.id}", e.title, e.cover, _weekday_from_airing_at(e.next_airing_at),
                    e.season, e.season_year) if e else None
    except Exception as ex:
        raise SeasonalError(f"couldn't look up {title!r}: {ex}") from ex
    if show is None:
        raise SeasonalError(f"couldn't find {title!r} on AniList/MyAnimeList")
    show_id, show_title, cover, day, start_season, start_year = show
    # Same storage rule as browse_season(): something airing now is kept
    # under the current season (its refresh then reads the cached
    # current-season listing, which includes everything airing); anything
    # else under its own start season.
    cur_season, cur_year = current_season()
    if day is not None or not (start_season and start_year):
        start_season, start_year = cur_season, cur_year
    follow(show_id, show_title, cover, day, start_season, start_year)
    return SeasonalShow(id=show_id, title=show_title, cover=cover, day_of_week=day, followed=True)


def set_day(show_id: str, day: int | None) -> None:
    """Place a followed show on a weekday (0=Monday..6=Sunday), or None to
    go back to its airing day. KeyError if it isn't followed."""
    if day is not None and not 0 <= day <= 6:
        raise ValueError("day must be 0 (Monday) .. 6 (Sunday)")
    with _conn() as con:
        cur = con.execute("UPDATE follows SET day_override = ? WHERE id = ?", (day, show_id))
        if cur.rowcount == 0:
            raise KeyError(show_id)


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
            """SELECT shows.*, follows.day_override FROM shows
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
        SeasonalShow(
            id=r["id"], title=r["title"], cover=r["cover"], followed=True,
            day_of_week=r["day_override"] if r["day_override"] is not None else r["day_of_week"],
            day_manual=r["day_override"] is not None,
            auto_day_of_week=r["day_of_week"],
        )
        for r in rows
    ]
