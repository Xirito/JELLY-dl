"""MyAnimeList (official API v2) client — anime search + detail lookup.
Fallback for services/anilist.py in NyaaTorDownloader's anime pre-search
step (anime_search()/anime_details() in nyaa_tor_plugin.py), used only
when AniList's call fails -- see that plugin for the fallback logic. Not
used by anicli_plugin.py at all (same reasoning as anilist.py: that
plugin's episodes()/streams need an anidb.app-native id, which neither
AniList nor MAL can substitute for).

Why a second source instead of just fixing AniList: AniList's own API can
go down outage-wide on their end ("The AniList API has been temporarily
disabled due to severe stability issues" -- a real, documented, recurring
message, not specific to this app or this network) with no ETA and
nothing on our side to fix. Separate infrastructure -- a different
company, different backend, not behind the same Cloudflare zone as either
AniList or anidb.app -- means an AniList outage doesn't take this down
too.

Requires a Client ID (docs.anilist.co-style API keys don't exist here
either, but MAL's public-data auth is much lighter than AniList's -- no
OAuth login flow needed for read-only search/detail, just a header):

  1. Log into (or create a free account on) myanimelist.net.
  2. Preferences -> API -> "Create ID".
  3. Fill in an app name/description; for App Type pick something like
     "web" -- the redirect URI field can be a placeholder (e.g.
     http://localhost/) since this never does the OAuth login flow, only
     the plain client_auth header below.
  4. Submit -- the Client ID is what this needs, no client secret, no
     token exchange.
  5. Set MAL_CLIENT_ID to that value wherever this container's other
     secrets live (same place as JELLYFIN_API_KEY), then rebuild/restart.

Without MAL_CLIENT_ID set, is_configured() returns False and the fallback
in nyaa_tor_plugin.py is skipped entirely -- AniList failures surface
exactly as before, no behavior change for anyone who hasn't set this up.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field

from curl_cffi import requests as curl_requests

log = logging.getLogger(__name__)

_API_BASE = "https://api.myanimelist.net/v2"
_CLIENT_ID = os.environ.get("MAL_CLIENT_ID", "").strip()
# Not documented as Cloudflare-fronted the way anidb.app/AniList are, but
# impersonating a real browser costs nothing and matches this codebase's
# established defensive default for every outbound HTTP client (see
# services/anidb.py, services/anilist.py) rather than assuming this one's
# different.
_IMPERSONATE = "chrome124"

_request_lock = threading.Lock()
_request_count = 0


def _next_request_num() -> int:
    global _request_count
    with _request_lock:
        _request_count += 1
        return _request_count


class MalError(RuntimeError):
    pass


def is_configured() -> bool:
    return bool(_CLIENT_ID)


def _get(path: str, params: dict, timeout: int = 15) -> dict:
    if not _CLIENT_ID:
        raise MalError("MAL_CLIENT_ID is not set -- MyAnimeList fallback isn't configured")
    n = _next_request_num()
    log.info("MAL request #%d: %s %s", n, path, params)
    t0 = time.monotonic()
    resp = curl_requests.get(
        _API_BASE + path,
        params=params,
        headers={"X-MAL-CLIENT-ID": _CLIENT_ID, "Accept": "application/json"},
        impersonate=_IMPERSONATE,
        timeout=timeout,
    )
    elapsed = time.monotonic() - t0
    log.info("MAL response #%d: status=%d elapsed=%.2fs", n, resp.status_code, elapsed)
    if resp.status_code >= 400:
        try:
            body_preview = (resp.text or "")[:1500]
        except Exception:
            body_preview = "<unavailable>"
        log.warning("MAL request #%d failed: status=%d body=%r", n, resp.status_code, body_preview)
    try:
        resp.raise_for_status()
    except Exception as e:
        raise MalError(f"MyAnimeList request failed: {e}") from e
    try:
        return resp.json()
    except Exception as e:
        raise MalError(f"MyAnimeList returned an unparseable response: {e}") from e


def search_anime(query: str) -> list[tuple[str, str]]:
    """(id, title) pairs, id being MAL's own numeric anime id as a string
    -- opaque to callers, meaningless to anidb.app or AniList."""
    query = (query or "").strip()
    if not query:
        return []
    data = _get("/anime", {"q": query, "limit": 20})
    out: list[tuple[str, str]] = []
    for node in data.get("data") or []:
        entry = node.get("node") if isinstance(node, dict) else None
        if not isinstance(entry, dict):
            continue
        anime_id = entry.get("id")
        title = entry.get("title")
        if anime_id is None or not title:
            continue
        out.append((str(anime_id), title))
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
        data = _get(
            f"/anime/{numeric_id}",
            {"fields": "title,main_picture{medium,large},alternative_titles{synonyms,en,ja}"},
        )
    except MalError as e:
        log.warning("mal.anime_detail(%s) failed: %s", media_id, e)
        return None
    if not isinstance(data, dict) or not data.get("title"):
        return None
    plain_title = data["title"]
    alt = data.get("alternative_titles") or {}
    # MAL's plain "title" is usually the romaji/original transliteration,
    # not English -- unlike AniList's title.english/.romaji split, MAL
    # keeps the English name (when it has one) only under
    # alternative_titles.en. Same English-first preference as
    # anilist.py's _best_title() for a consistent result either way this
    # falls back.
    official = alt.get("en") or plain_title
    romaji = plain_title if plain_title.lower() != official.lower() else None
    cover_obj = data.get("main_picture") or {}
    cover = cover_obj.get("large") or cover_obj.get("medium")
    synonyms = [s for s in (alt.get("synonyms") or []) if s]
    return AnimeDetail(official=official, cover=cover, romaji=romaji, synonyms=synonyms)
