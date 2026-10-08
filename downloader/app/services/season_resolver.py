"""Season placement: where a picked anime should be downloaded to.

Jellyfin wants  <library>/shows/<Series>/Season NN/<episodes>  -- but every
lookup source hands back one title PER SEASON ("Oshi no Ko 2nd Season",
"Spy x Family Season 2", "Kaguya-sama: Ultra Romantic"), so the old
auto-fill made a brand-new top-level folder for every sequel. resolve()
turns a pick into the right series folder + season number, in layers:

Season number
  1. The anime-lists mapping (services/anime_lists.py): AniList/MAL id ->
     TVDB show + TVDB season + episode offset. Exact, and right even when
     the title has no number in it at all. ani-cli picks carry only a title,
     so those go through an AniList search first that's only trusted on an
     exact title/synonym match.
  2. Not mapped yet (mostly brand-new seasons): walk AniList's PREQUEL
     links. If an earlier season IS mapped, count forward from it ("TVDB
     season 2, one season later"); otherwise count the TV-format prequels
     back to the first season. -> confidence "guess".
  3. AniList unreachable, or no id at all: parse the title ("2nd Season",
     "Season 2", "S2", "II") -> "guess"; no marker at all -> season 1.
  A movie/OVA/special pick isn't a season: it goes to Season 00 (Jellyfin's
  specials) unless the mapping says otherwise.

Series folder
  a. The show's existing library folder, asked from Jellyfin by provider id
     (TVDB/AniList/MAL/AniDB -- no title guessing), with the same
     NOTIFY_<TOKEN>_URL / _APIKEY the refresh notifier already uses. Both
     containers mount the library at the same path (docker-compose.yml), so
     Jellyfin's item path maps straight onto a $token$/... destination.
  b. Otherwise a folder under <library>/shows/ whose name matches one of
     season 1's titles -- covers folders this app made that Jellyfin hasn't
     scanned yet, and setups without an API key. Deliberately season 1's
     titles only: the old auto-fill left per-season folders like
     "Attack on Titan Season 2" behind, and season 3 must not end up nested
     inside one of those.
  c. Otherwise a new folder named after season 1's title.

Nothing here raises for a flaky AniList/Jellyfin -- every layer degrades to
the next, and the frontend always gets *some* placement plus a note saying
how it was decided, which the user can still edit.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..config import MEDIA_SERVER_TARGETS
from ..models import SeasonPlacement
from . import alt_titles, anilist, anime_lists

log = logging.getLogger(__name__)

SHOWS_DIR = "shows"   # the convention the frontend's auto-fill has always used
_TV_FORMATS = {"TV", "TV_SHORT", "ONA"}
_MAX_RELATION_REQUESTS = 6
_MAPPED_TTL_S = 3600
_GUESS_TTL_S = 120     # short: a guess may only be a guess because AniList blipped
_LIBRARY_TTL_S = 300
_JELLYFIN_TIMEOUT_S = 8


class NoMediaTarget(ValueError):
    pass


# -- names -------------------------------------------------------------------

_ILLEGAL = re.compile(r'[\\/:*?"<>|]')


def folder_name(title: str) -> str:
    """Filesystem-safe folder name -- the same rule the frontend uses
    (illegal characters become spaces, whitespace runs collapse), plus no
    trailing dot/space, which Windows/SMB clients choke on."""
    return re.sub(r"\s+", " ", _ILLEGAL.sub(" ", title or "")).strip().rstrip(". ")


def norm(s: str) -> str:
    """Comparison key: casefolded, diacritics and punctuation dropped --
    "Attack on Titan:" and "attack on titan" compare equal."""
    return alt_titles.norm(s or "")


_WORD_ORDINALS = {"second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7}
_ROMAN = {"II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8, "IX": 9, "X": 10}
_SEASON_PATTERNS = [
    # "2nd Season", "3rd season"
    (re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)\s+season\b", re.I), lambda m: int(m.group(1))),
    # "Season 2", "season2"
    (re.compile(r"\bseason\s*(\d{1,2})\b", re.I), lambda m: int(m.group(1))),
    # "(The) Second Season"
    (re.compile(r"\b(?:the\s+)?(" + "|".join(_WORD_ORDINALS) + r")\s+season\b", re.I),
     lambda m: _WORD_ORDINALS[m.group(1).lower()]),
    # "S2" -- upper-case S only, so it can't eat the end of an ordinary word
    (re.compile(r"\bS(\d{1,2})\b"), lambda m: int(m.group(1))),
    # "Mob Psycho 100 II", "Mushoku Tensei II: Isekai..." -- a standalone
    # roman numeral at the very end or right before a subtitle separator
    # ("Final Fantasy VII Advent Children" is left alone)
    (re.compile(r"\s(" + "|".join(sorted(_ROMAN, key=len, reverse=True)) + r")(?=\s*(?:[:\-–]|$))"),
     lambda m: _ROMAN[m.group(1)]),
]
# Split-cour markers continue the SAME TVDB season -- only ever stripped from
# the series name, never turned into a season number.
_PART = re.compile(r"\b(?:part|cour)\s*(?:\d{1,2}|[ivx]+)\b", re.I)
_TRAILING_SEP = re.compile(r"[\s:\-–,.]+$")


def parse_title(title: str) -> tuple[str, int | None]:
    """("series name", season) from a per-season title. season is None when
    the title carries no sequel marker at all."""
    original = (title or "").strip()
    t = original
    season = None
    for pattern, value in _SEASON_PATTERNS:
        m = pattern.search(t)
        if m:
            season = value(m)
            # Everything after the marker is the season's own subtitle:
            # "Mushoku Tensei II: Isekai Ittara..." -> "Mushoku Tensei"
            t = t[: m.start()]
            break
    t = _PART.sub(" ", t)
    t = _TRAILING_SEP.sub("", re.sub(r"\s+", " ", t)).strip()
    return (t or original), season


def _all_titles(media: dict) -> list[str]:
    t = media.get("title") or {}
    return [x for x in (t.get("english"), t.get("romaji"), t.get("native"), *(media.get("synonyms") or [])) if x]


# -- ids -----------------------------------------------------------------------

def _parse_anime_id(anime_id: str | None) -> tuple[int | None, int | None]:
    """"anilist:123" -> (123, None); "mal:456" -> (None, 456); else (None, None)."""
    source, _, raw = (anime_id or "").partition(":")
    n = anime_lists._int(raw)
    if n is None:
        return None, None
    if source == "anilist":
        return n, None
    if source == "mal":
        return None, n
    return None, None


def _search_verified(title: str) -> dict | None:
    """AniList's best match for a bare title (ani-cli picks), trusted only
    when one of its titles/synonyms matches exactly."""
    try:
        media = anilist.find_one(title)
    except Exception as e:  # AniListError, or a malformed response
        log.info("placement: AniList search for %r failed: %s", title, e)
        return None
    if media and norm(title) in {norm(x) for x in _all_titles(media)}:
        return media
    return None


# -- season from AniList relations ----------------------------------------------

@dataclass
class _Guess:
    season: int
    tvdb_id: int | None      # set when anchored on a mapped earlier season
    root_title: str | None   # earliest TV-format season reached
    note: str


def _pick_prequel(media: dict) -> dict | None:
    edges = (media.get("relations") or {}).get("edges") or []
    prequels = [
        e["node"] for e in edges
        if isinstance(e, dict) and e.get("relationType") == "PREQUEL"
        and isinstance(e.get("node"), dict) and e["node"].get("type") in (None, "ANIME")
    ]
    tv = [n for n in prequels if n.get("format") in _TV_FORMATS]
    return (tv or prequels or [None])[0]


def _guess_from_relations(al_id: int | None, lists) -> _Guess | None:
    """Walk PREQUEL edges back from al_id. None when AniList can't tell
    (no id, unknown id, or AniList unreachable)."""
    if al_id is None:
        return None
    try:
        media = anilist.media_relations(al_id)
        if media is None:
            return None
        is_season = media.get("format") in _TV_FORMATS
        root_title = anilist.best_title(media.get("title") or {}) if is_season else None
        steps = 0   # TV-format seasons walked past
        for _ in range(_MAX_RELATION_REQUESTS - 1):
            prequel = _pick_prequel(media)
            if prequel is None:
                break
            if prequel.get("format") in _TV_FORMATS:
                steps += 1
                root_title = anilist.best_title(prequel.get("title") or {}) or root_title
                e = lists.lookup(prequel.get("id")) if lists else None
                if e and e.tvdb_id is not None and e.tvdb_season:
                    if not is_season:
                        return _Guess(0, e.tvdb_id, None, "movie/OVA, placed with the show's specials")
                    season = e.tvdb_season + steps
                    after = "the season" if steps == 1 else f"{steps} seasons"
                    return _Guess(
                        season, e.tvdb_id, None,
                        f"season {season}: {after} after TVDB season {e.tvdb_season} (AniList prequels)",
                    )
            media = anilist.media_relations(prequel["id"])
            if media is None:
                break
    except Exception as e:  # AniListError, or a malformed response
        log.info("placement: AniList relations lookup failed: %s", e)
        return None
    if not is_season:
        return _Guess(0, None, root_title, "movie/OVA, placed with the show's specials")
    return _Guess(steps + 1, None, root_title, f"season {steps + 1}, counted from AniList prequels")


# -- library ----------------------------------------------------------------------

_lib_lock = threading.Lock()
_lib_cache: dict[str, tuple[float, list[dict]]] = {}


def _jellyfin_series(token: str) -> list[dict]:
    """Every Series item (ProviderIds + Path), cached a few minutes. [] when
    Jellyfin isn't configured for this token or doesn't answer quickly."""
    url = os.environ.get(f"NOTIFY_{token.upper()}_URL")
    key = os.environ.get(f"NOTIFY_{token.upper()}_APIKEY")
    if not (url and key):
        return []
    with _lib_lock:
        hit = _lib_cache.get(token)
        if hit and time.time() - hit[0] < _LIBRARY_TTL_S:
            return hit[1]
    try:
        q = "&".join(f"{k}={v}" for k, v in {
            "Recursive": "true", "IncludeItemTypes": "Series", "Fields": "ProviderIds,Path",
            "EnableImages": "false", "EnableUserData": "false",
        }.items())
        # one attempt, short timeout: someone is waiting on this
        res = alt_titles.http_json(
            f"{url.rstrip('/')}/Items?{q}",
            headers={"Authorization": f'MediaBrowser Token="{key}"'},
            timeout=_JELLYFIN_TIMEOUT_S, retries=1,
        )
        items = [it for it in (res or {}).get("Items", []) if isinstance(it, dict)]
    except Exception as e:
        log.info("placement: Jellyfin library lookup failed: %s", e)
        return []
    with _lib_lock:
        _lib_cache[token] = (time.time(), items)
    return items


def _item_matches(item: dict, tvdb_id: int | None, al_ids: set[int], lists) -> bool:
    p = {k.lower(): str(v) for k, v in (item.get("ProviderIds") or {}).items() if v}
    if tvdb_id is not None and p.get("tvdb") == str(tvdb_id):
        return True
    if anime_lists._int(p.get("anilist")) in al_ids:
        return True
    if lists is not None and tvdb_id is not None:
        for key, index in (("anilist", lists.by_anilist), ("mal", lists.by_mal),
                           ("myanimelist", lists.by_mal), ("anidb", lists.by_anidb)):
            e = index.get(anime_lists._int(p.get(key)))
            if e is not None and e.tvdb_id == tvdb_id:
                return True
    return False


def _as_destination(token: str, base: Path, path: str | None) -> str | None:
    """Jellyfin's absolute item path -> "$token$/<relative>", if it lies
    inside this media target."""
    if not path:
        return None
    try:
        rel = PurePosixPath(path).relative_to(PurePosixPath(str(base)))
    except ValueError:
        return None
    return f"${token}$/{rel.as_posix()}" if rel.parts else None


def _jellyfin_folder(token: str, base: Path, tvdb_id, al_ids, lists, root_keys: set[str]) -> str | None:
    if tvdb_id is None and not al_ids:
        return None
    hits = []
    for it in _jellyfin_series(token):
        if not _item_matches(it, tvdb_id, al_ids, lists):
            continue
        dest = _as_destination(token, base, it.get("Path"))
        if dest:
            hits.append((dest, it.get("Name") or ""))
    if not hits:
        return None
    # Several items for one show = leftover per-season folders from the old
    # auto-fill. Prefer the one named like season 1, then one with no
    # season marker in its name.
    def rank(hit):
        dest, name = hit
        folder = dest.rsplit("/", 1)[-1]
        return (norm(folder) not in root_keys, parse_title(folder)[1] is not None, len(folder))
    return sorted(hits, key=rank)[0][0]


def _disk_folder(token: str, base: Path, root_keys: set[str]) -> str | None:
    if not root_keys:
        return None
    try:
        names = sorted(d.name for d in (base / SHOWS_DIR).iterdir() if d.is_dir())
    except OSError:
        return None
    for name in names:
        if norm(name) in root_keys:
            return f"${token}$/{SHOWS_DIR}/{name}"
    return None


def _season_one(group: list) -> list:
    """The season-1 entries of a show, first cour first."""
    ones = [e for e in group if e.anilist_id and e.tvdb_season == 1 and not e.episode_offset]
    if not ones:
        seasons = [e.tvdb_season for e in group if e.anilist_id and e.tvdb_season]
        ones = [e for e in group if e.anilist_id and seasons and e.tvdb_season == min(seasons)]
    return sorted(ones, key=lambda e: (e.type != "TV", e.anilist_id))


# -- entry point ----------------------------------------------------------------

_cache_lock = threading.Lock()
_cache: dict[tuple, tuple[float, SeasonPlacement]] = {}


def _target(token: str | None):
    if token:
        if token not in MEDIA_SERVER_TARGETS:
            raise NoMediaTarget(f"unknown media library ${token}$")
        return token, MEDIA_SERVER_TARGETS[token]
    if "jellyfin" in MEDIA_SERVER_TARGETS:
        return "jellyfin", MEDIA_SERVER_TARGETS["jellyfin"]
    if MEDIA_SERVER_TARGETS:
        t = next(iter(MEDIA_SERVER_TARGETS))
        return t, MEDIA_SERVER_TARGETS[t]
    raise NoMediaTarget("no media library is configured (MEDIA_TARGETS)")


def resolve(title: str, anime_id: str | None = None, token: str | None = None) -> SeasonPlacement:
    token, target = _target(token)
    title = (title or "").strip()
    key = (token, anime_id or "", norm(title))
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.time() < hit[0]:
            return hit[1]
    placement = _resolve(title, anime_id, token, target.base_path)
    ttl = _MAPPED_TTL_S if placement.confidence == "mapped" else _GUESS_TTL_S
    with _cache_lock:
        _cache[key] = (time.time() + ttl, placement)
    return placement


def _resolve(title: str, anime_id: str | None, token: str, base: Path) -> SeasonPlacement:
    lists = anime_lists.get()
    al_id, mal_id = _parse_anime_id(anime_id)
    if al_id is None and mal_id is None and title:
        verified = _search_verified(title)
        if verified:
            al_id = verified["id"]
    entry = lists.lookup(al_id, mal_id) if lists else None
    if entry is not None and al_id is None:
        al_id = entry.anilist_id

    # -- season number --------------------------------------------------------
    offset = None
    root_title = None
    if entry is not None and entry.tvdb_id is not None and entry.tvdb_season is not None:
        tvdb_id, season, offset = entry.tvdb_id, entry.tvdb_season, entry.episode_offset
        confidence = "mapped"
        how = f"TVDB season {season}" + (f", episodes after {offset}" if offset else "") + " (anime-lists)"
    else:
        confidence = "guess"
        guess = _guess_from_relations(al_id, lists)
        if guess is not None:
            season, tvdb_id, root_title, how = guess.season, guess.tvdb_id, guess.root_title, guess.note
        else:
            tvdb_id = None
            base_name, parsed = parse_title(title)
            season, root_title = (parsed or 1), base_name
            how = f"season {parsed} from the title" if parsed else "no season in the title, assuming season 1"

    # -- series folder --------------------------------------------------------
    group = lists.series(tvdb_id) if lists is not None else []
    al_ids = {e.anilist_id for e in group if e.anilist_id} | ({al_id} if al_id else set())
    roots = _season_one(group)
    root_names: list[str] = []
    if roots:
        try:
            titles = anilist.media_titles([e.anilist_id for e in roots[:5]])
            for e in roots[:5]:
                m = titles.get(e.anilist_id)
                if m:
                    root_names += _all_titles(m)
                    root_title = root_title or anilist.best_title(m.get("title") or {})
        except Exception as e:  # AniListError, or a malformed response
            log.info("placement: AniList title lookup failed: %s", e)
    if root_title:
        root_names.append(root_title)
    # The picked title minus its season marker is a series-level name too
    # ("Oshi no Ko 2nd Season" -> "Oshi no Ko").
    stripped = parse_title(title)[0]
    root_keys = {k for k in (norm(n) for n in [*root_names, stripped]) if k}

    series_dir = (_jellyfin_folder(token, base, tvdb_id, al_ids, lists, root_keys)
                  or _disk_folder(token, base, root_keys))
    in_library = series_dir is not None
    if series_dir is None:
        name = folder_name(root_title or stripped or title) or folder_name(title)
        series_dir = f"${token}$/{SHOWS_DIR}/{name}"
    series = series_dir.rsplit("/", 1)[-1]
    where = "existing folder in your library" if in_library else "new show folder"

    placement = SeasonPlacement(
        destination=f"{series_dir}/Season {season:02d}",
        series_dir=series_dir,
        series=series,
        season=season,
        episode_offset=offset or None,
        confidence=confidence,
        in_library=in_library,
        note=f"{how} · {where}",
    )
    log.info("placement for %r (%s): %s [%s]", title, anime_id or "no id", placement.destination, placement.note)
    return placement
