"""AniList GraphQL client — anime search + detail lookup, used ONLY by
NyaaTorDownloader's optional pre-search step (anime_search()/anime_details()
in nyaa_tor_plugin.py, capabilities.supports_anime_lookup). This replaces
that one use of anidb.app scraping (services/anidb.py's search_anime()/
anime_detail()) with AniList's public GraphQL API
(https://graphql.anilist.co, no auth required, no bot-mitigation in front
of it).

Why: anidb.app sits behind Cloudflare, which intermittently serves a JS/
managed-challenge page instead of the real site (HTTP 503, "Just a
moment...") — an ongoing, well-documented problem the upstream ani-cli
shell script's own scraping hits too. That made nyaa_tor's anime-lookup
step fail right alongside ani-cli's, even though nyaa_tor never actually
needs anything anidb.app-specific: it only ever wanted an official title
(+ a few name variants) and cover art to build a text search query for a
torrent indexer. AniList is a maintained API built for exactly that.

Deliberately NOT used for anicli_plugin.py's own search/episodes/streams
flow — see that plugin and services/anidb.py. AniList has no episode or
video-stream data at all, and anicli_plugin.py's episodes() needs an
anidb.app-native id (a slug like "bleach-670", numeric suffix pulled off
for anidb.app's own /api/frontend/anime/<id>/episodes endpoint) — an
AniList id wouldn't mean anything there. So that plugin's pipeline stays
fully anidb.app-dependent, and stays exposed to the same Cloudflare
fragility described above; only nyaa_tor's metadata pre-search step gets
freed of it here.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from curl_cffi import requests as curl_requests

log = logging.getLogger(__name__)

# Diagnostic-only, not correctness-critical -- a request number that's off
# by one under concurrent searches doesn't matter, it's just so consecutive
# log lines for the same request are easy to pair up ("request #7" /
# "response #7") in `docker logs`, and so a run of numbers climbing fast
# is itself a visible clue about request volume.
_request_lock = threading.Lock()
_request_count = 0

def is_configured() -> bool:
    return True  # no auth or config needed for AniList's public GraphQL API

def _next_request_num() -> int:
    global _request_count
    with _request_lock:
        _request_count += 1
        return _request_count


# Headers worth logging by name when present: the X-RateLimit-* trio is
# AniList's own documented quota signal (docs.anilist.co/guide/
# rate-limiting) -- watching "remaining" trend down across requests is the
# concrete evidence for or against "a burst of requests tripped a block",
# rather than guessing from the outside. cf-ray, if present, confirms
# Cloudflare specifically handled (or blocked) the request, as opposed to
# AniList's own backend -- useful context if this ever needs reporting to
# either of them.
_LOG_HEADERS = ("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after", "x-ratelimit-reset", "cf-ray")

_API_URL = "https://graphql.anilist.co"
# graphql.anilist.co sits behind Cloudflare too, and a request with no
# User-Agent at all (curl_cffi's default when `impersonate` isn't set --
# confirmed by inspection, it sends none) gets a flat 403 from it. Same
# fix services/anidb.py already uses for the same reason: impersonate a
# real browser's TLS/JA3 fingerprint + header set.
_IMPERSONATE = "chrome124"

# Page(media(search: ...)) rather than the singular `Media(search: ...)`
# query — AniList's singular Media field accepts a search string too, but
# only ever returns its own idea of the single best match; Page lets us
# offer the user a short list to pick from, same UX as anidb.app's old
# /browse results list.
_SEARCH_QUERY = """
query ($search: String!, $perPage: Int) {
  Page(perPage: $perPage) {
    media(search: $search, type: ANIME) {
      id
      title {
        romaji
        english
        native
      }
    }
  }
}
"""

# Singular Media(id: ...) — one exact row, no Page/pagination wrapper needed.
_DETAIL_QUERY = """
query ($id: Int) {
  Media(id: $id, type: ANIME) {
    id
    title {
      romaji
      english
      native
    }
    synonyms
    coverImage {
      extraLarge
      large
      medium
    }
  }
}
"""

# Seasonal browse (services/seasonal.py) -- unlike _SEARCH_QUERY/_DETAIL_QUERY
# above (used only by nyaa_tor_plugin.py's anime pre-search step), this one
# feeds the cross-plugin seasonal-follow tracker, shared by nyaa_tor AND
# ani-cli. format_in restricts to weekly-TV-style releases (a movie/OVA/
# special has no weekday to put in a calendar); sort by popularity so a
# season's ~150+ entries lead with what someone's actually likely to want
# to follow, since this only takes the first page. nextAiringEpisode is
# null once a show has fully finished airing (or before it's started) --
# services/seasonal.py turns its airingAt (a Unix timestamp) into a
# calendar weekday, in the server's local time (this app has no separate
# per-user timezone setting anywhere else either).
_SEASON_QUERY = """
query ($season: MediaSeason!, $seasonYear: Int!, $page: Int, $perPage: Int) {
  Page(page: $page, perPage: $perPage) {
    media(
      season: $season, seasonYear: $seasonYear, type: ANIME,
      format_in: [TV, TV_SHORT], sort: POPULARITY_DESC
    ) {
      id
      title {
        romaji
        english
        native
      }
      coverImage {
        extraLarge
        large
        medium
      }
      nextAiringEpisode {
        airingAt
        episode
      }
    }
  }
}
"""


# Season placement (services/season_resolver.py) -- three small lookups the
# resolver needs beyond what nyaa_tor's pre-search uses:
#   _TITLES_QUERY     every title/synonym for a batch of ids (one request for
#                     all seasons of a show) -- matched against existing
#                     library folder names, and to name a new show's folder
#                     after its season 1.
#   _RELATIONS_QUERY  one entry's PREQUEL edges -- walked back to count how
#                     many seasons came before, for shows anime-lists hasn't
#                     mapped yet (brand-new seasons, mostly).
#   _FIND_QUERY       AniList's own single best match for a title, for
#                     ani-cli picks (they only carry a title, no AniList id);
#                     the resolver only trusts it on an exact title match.
_TITLES_QUERY = """
query ($ids: [Int]) {
  Page(perPage: 50) {
    media(id_in: $ids, type: ANIME) {
      id
      format
      title { romaji english native }
      synonyms
    }
  }
}
"""

_RELATIONS_QUERY = """
query ($id: Int) {
  Media(id: $id, type: ANIME) {
    id
    format
    title { romaji english native }
    relations {
      edges {
        relationType
        node { id type format title { romaji english native } }
      }
    }
  }
}
"""

_FIND_QUERY = """
query ($search: String) {
  Media(search: $search, type: ANIME) {
    id
    format
    title { romaji english native }
    synonyms
  }
}
"""


class AniListError(RuntimeError):
    pass


def _post(query: str, variables: dict, timeout: int = 15) -> dict:
    n = _next_request_num()
    log.info("AniList request #%d: %s", n, variables)
    t0 = time.monotonic()
    try:
        resp = curl_requests.post(
            _API_URL,
            json={"query": query, "variables": variables},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            impersonate=_IMPERSONATE,
            timeout=timeout,
        )
    except Exception as e:
        # Transport-level failure (DNS, refused, timeout, proxy) -- curl_cffi
        # raises its own exception types for these, which used to escape
        # every caller's `except AniListError` untouched: nyaa_tor's MAL
        # fallback never kicked in for a plain "can't reach AniList", and
        # season placement would 500 instead of degrading.
        log.warning("AniList request #%d: couldn't reach AniList: %s", n, e)
        raise AniListError(f"couldn't reach AniList: {e}") from e
    elapsed = time.monotonic() - t0
    seen_headers = {h: resp.headers.get(h) for h in _LOG_HEADERS if resp.headers.get(h) is not None}
    log.info(
        "AniList response #%d: status=%d elapsed=%.2fs %s",
        n, resp.status_code, elapsed, seen_headers or "(no rate-limit/cf-ray headers present)",
    )
    if resp.status_code >= 400:
        # Full body + full headers, but only on failure and only at WARNING
        # -- this is the one thing that actually tells apart "AniList's own
        # JSON error", "Cloudflare's HTML challenge/block page", and
        # "something else entirely", none of which look any different from
        # the status code alone. Truncated: a Cloudflare block page can run
        # several KB of inline CSS/JS that's noise here.
        try:
            body_preview = (resp.text or "")[:1500]
        except Exception:
            body_preview = "<unavailable>"
        log.warning(
            "AniList request #%d failed: status=%d headers=%s body=%r",
            n, resp.status_code, dict(resp.headers), body_preview,
        )
    if resp.status_code == 429:
        # AniList's own rate limit (see docs.anilist.co/guide/rate-limiting)
        # — a plain 503-style raise_for_status() would say "server error",
        # which is misleading for what's actually a client-side backoff
        # situation.
        raise AniListError("AniList rate-limited this request — try again in a moment")
    if resp.status_code == 403:
        # Not the same thing as the missing-User-Agent 403 `impersonate`
        # fixes (that one never reached here at all -- it was rejected
        # before AniList's own app logic ever saw it). Two confirmed,
        # distinguishable causes share this status code:
        try:
            body_text = resp.text or ""
        except Exception:
            body_text = ""
        if "temporarily disabled" in body_text.lower():
            # AniList's *entire* API taken down on their end -- confirmed
            # live 2026-09-06 (their exact wording: "The AniList API has
            # been temporarily disabled due to severe stability issues"),
            # and the identical message is a known recurring AniList-side
            # outage reported by unrelated projects, not anything specific
            # to this app, this request, or this network. No amount of
            # retrying, backing off, or authenticating changes this --
            # it's off for everyone until AniList turns it back on.
            raise AniListError(
                "AniList's API is temporarily disabled on their end (their "
                "own message: \"temporarily disabled due to severe "
                "stability issues\") — a full outage, not specific to this "
                "request. Nothing to fix here; wait for AniList or use a "
                "fallback source."
            )
        # Otherwise: AniList's documented behavior for its OWN abuse
        # detection: "In very rare cases, AniList may block your IP
        # address from accessing the API. This is usually due to a large
        # number of requests being made from a single IP address."
        # (docs.anilist.co/guide/considerations). Impersonating a browser
        # can't prevent this -- it's IP-based, not header/fingerprint-based
        # -- so surface it as what it is (a temporary block that clears on
        # its own) rather than a bare "HTTP Error 403".
        raise AniListError(
            "AniList temporarily blocked this network (HTTP 403) — their "
            "own anti-abuse system does this after a burst of requests from "
            "one IP, not something wrong on this end. It should clear on "
            "its own; avoid firing off repeated anime searches back-to-back "
            "while it's in effect."
        )
    try:
        resp.raise_for_status()
    except Exception as e:
        raise AniListError(f"AniList request failed: {e}") from e
    payload = resp.json()
    errors = payload.get("errors")
    if errors:
        msg = "; ".join(e.get("message", "unknown AniList error") for e in errors if isinstance(e, dict))
        raise AniListError(msg or "AniList returned an error")
    return payload.get("data") or {}


def _best_title(title: dict) -> str | None:
    # English first -- closest match to what anidb.app's og:title scrape
    # used to hand back (the name most release groups use), romaji next,
    # native last-resort so a title always comes back if AniList has the
    # entry at all.
    if not isinstance(title, dict):
        return None
    return title.get("english") or title.get("romaji") or title.get("native")


def search_anime(query: str) -> list[tuple[str, str]]:
    """(id, title) pairs. id is AniList's own numeric media id, as a
    string -- opaque to callers, never fed back into anidb.app (unlike
    services/anidb.py's slug-style ids)."""
    query = (query or "").strip()
    if not query:
        return []
    data = _post(_SEARCH_QUERY, {"search": query, "perPage": 20})
    media_list = (data.get("Page") or {}).get("media") or []
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for m in media_list:
        if not isinstance(m, dict) or m.get("id") is None:
            continue
        title = _best_title(m.get("title") or {})
        if not title:
            continue
        media_id = str(m["id"])
        if media_id in seen:
            continue
        seen.add(media_id)
        out.append((media_id, title))
    return out


@dataclass
class AnimeDetail:
    official: str
    cover: str | None = None
    romaji: str | None = None
    synonyms: list[str] = field(default_factory=list)


def anime_detail(media_id: str) -> AnimeDetail | None:
    try:
        numeric_id = int(media_id)
    except (TypeError, ValueError):
        return None
    try:
        data = _post(_DETAIL_QUERY, {"id": numeric_id})
    except AniListError as e:
        # Unlike search_anime() (which lets this propagate up to the route
        # handler's own error message), this one gets swallowed to None so
        # nyaa_tor_plugin.py can raise its own "couldn't load that anime's
        # page" -- which on its own gives zero clue WHY. Log it here so the
        # real reason (rate limit, IP block, whatever) still shows up in
        # `docker logs` instead of disappearing entirely.
        log.warning("anime_detail(%s) failed: %s", media_id, e)
        return None
    media = data.get("Media")
    if not isinstance(media, dict):
        return None
    title = media.get("title") or {}
    official = _best_title(title)
    if not official:
        return None
    romaji = title.get("romaji")
    if romaji and romaji.lower() == official.lower():
        romaji = None
    cover_obj = media.get("coverImage") or {}
    cover = cover_obj.get("extraLarge") or cover_obj.get("large") or cover_obj.get("medium")
    synonyms = [s for s in (media.get("synonyms") or []) if s]
    return AnimeDetail(official=official, cover=cover, romaji=romaji, synonyms=synonyms)


def best_title(title: dict) -> str | None:
    """Public alias of _best_title() -- English, then romaji, then native."""
    return _best_title(title)


def media_titles(ids: list[int]) -> dict[int, dict]:
    """{id: media} for up to 50 ids in one request; each media carries
    `format`, `title` (romaji/english/native) and `synonyms`. Raises
    AniListError like every other call here."""
    ids = sorted({int(i) for i in ids if i is not None})[:50]
    if not ids:
        return {}
    data = _post(_TITLES_QUERY, {"ids": ids})
    media_list = (data.get("Page") or {}).get("media") or []
    return {m["id"]: m for m in media_list if isinstance(m, dict) and m.get("id") is not None}


def media_relations(media_id: int) -> dict | None:
    """One entry plus its relation edges, or None if AniList has no such id."""
    try:
        data = _post(_RELATIONS_QUERY, {"id": int(media_id)})
    except AniListError as e:
        if _is_not_found(e):
            return None
        raise
    media = data.get("Media")
    return media if isinstance(media, dict) else None


def find_one(search: str) -> dict | None:
    """AniList's single best match for `search`, or None when it has nothing.
    Callers must verify the match themselves -- AniList always returns
    *something* close-ish if it can."""
    search = (search or "").strip()
    if not search:
        return None
    try:
        data = _post(_FIND_QUERY, {"search": search})
    except AniListError as e:
        if _is_not_found(e):
            return None
        raise
    media = data.get("Media")
    return media if isinstance(media, dict) else None


def _is_not_found(e: Exception) -> bool:
    # Singular Media(...) with no match comes back as HTTP 404 + a GraphQL
    # "Not Found." error -- an answer, not an outage.
    msg = str(e).lower()
    return "not found" in msg or "404" in msg


@dataclass
class SeasonalEntry:
    id: str
    title: str
    cover: str | None = None
    next_airing_at: int | None = None  # Unix timestamp (UTC), or None


def seasonal_anime(season: str, year: int, per_page: int = 50) -> list[SeasonalEntry]:
    """One page (default 50, AniList's own per-page ceiling) of a season's
    anime, most popular first. `season` is case-insensitive
    ("winter"/"WINTER" both work -- AniList's MediaSeason enum itself is
    uppercase, normalized here so callers don't have to remember that).
    """
    data = _post(_SEASON_QUERY, {"season": season.upper(), "seasonYear": year, "page": 1, "perPage": per_page})
    media_list = (data.get("Page") or {}).get("media") or []
    out: list[SeasonalEntry] = []
    for m in media_list:
        if not isinstance(m, dict) or m.get("id") is None:
            continue
        title = _best_title(m.get("title") or {})
        if not title:
            continue
        cover_obj = m.get("coverImage") or {}
        cover = cover_obj.get("extraLarge") or cover_obj.get("large") or cover_obj.get("medium")
        nae = m.get("nextAiringEpisode")
        airing_at = nae.get("airingAt") if isinstance(nae, dict) else None
        out.append(SeasonalEntry(id=str(m["id"]), title=title, cover=cover, next_airing_at=airing_at))
    return out
