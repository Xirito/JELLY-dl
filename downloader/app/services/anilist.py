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

from dataclasses import dataclass, field

from curl_cffi import requests as curl_requests

_API_URL = "https://graphql.anilist.co"

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


class AniListError(RuntimeError):
    pass


def _post(query: str, variables: dict, timeout: int = 15) -> dict:
    resp = curl_requests.post(
        _API_URL,
        json={"query": query, "variables": variables},
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=timeout,
    )
    if resp.status_code == 429:
        # AniList's own rate limit (see docs.anilist.co/guide/rate-limiting)
        # — a plain 503-style raise_for_status() would say "server error",
        # which is misleading for what's actually a client-side backoff
        # situation.
        raise AniListError("AniList rate-limited this request — try again in a moment")
    resp.raise_for_status()
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
    except AniListError:
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
