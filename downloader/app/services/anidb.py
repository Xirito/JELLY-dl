"""Shared anidb.app client — search, titles, cover art, episode/stream
resolution. Used by AniCliDownloader (anicli_plugin.py — streams
episodes) and NyaaTorDownloader (nyaa_tor_plugin.py — only resolves an
anime's title(s) + cover before composing a torrent search query, never
touches episodes/streams). Extracted out of anicli_plugin.py so both
plugins share one implementation of anidb.app's quirks rather than two
copies drifting apart.

Not the classic AniDB.net — anidb.app is a separate site. This
re-implements the same anidb.app scraping steps the ani-cli shell script
itself uses (see https://github.com/pystardust/ani-cli, function names
kept as comments below for cross-reference). Cloudflare sits in front of
it, which is why every request goes through curl_cffi with an
impersonated Chrome TLS/JA3 fingerprint (the Python equivalent of the
curl-impersonate binaries the upstream shell script relies on).
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

from curl_cffi import requests as curl_requests

_BASE = "https://anidb.app"
_SEARCH_URL = _BASE + "/browse?q={q}"
_DESC_URL = _BASE + "/anime/{anime_id}"
_EPISODES_URL = _BASE + "/api/frontend/anime/{numeric_id}/episodes"
_LANGUAGES_URL = _BASE + "/api/frontend/episode/{episode_id}/languages"

_IMPERSONATE = "chrome124"
# Public (no leading underscore) — anicli_plugin.py's download() reuses this
# exact UA string as the Referer/User-Agent pair the CDN expects on the
# actual HLS segment requests, not just the anidb.app scraping requests above.
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


class AniDbError(RuntimeError):
    pass


def _get(url: str, timeout: int = 15) -> curl_requests.Response:
    resp = curl_requests.get(url, impersonate=_IMPERSONATE,
                              headers={"User-Agent": USER_AGENT}, timeout=timeout)
    resp.raise_for_status()
    return resp


def _extract_og_title(page: str) -> str | None:
    m = re.search(r'property="og:title"\s+content="([^"]+)"', page)
    if not m:
        m = re.search(r"<title>([^<]+)</title>", page)
    return html.unescape(m.group(1)).strip() if m else None


# -- search / browse -----------------------------------------------------
def search_anime(query: str) -> list[tuple[str, str]]:
    # anidb_search(): GET /browse?q=..., pull (id, title) pairs out of each
    # <a href=".../anime/<id>" ...alt="<title>"...> anchor.
    page = _get(_SEARCH_URL.format(q=query.replace(" ", "+"))).text
    if "Just a moment" in page:
        raise AniDbError("blocked by Cloudflare — try again in a bit")
    flat = page.replace("\n", " ")
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for chunk in re.split(r"(?=<a href)", flat):
        m = re.search(r'anime/([a-z0-9-]+-[0-9]+)"', chunk)
        if not m:
            continue
        alt = re.search(r'alt="([^"]+)"', chunk)
        if not alt:
            continue
        anime_id = m.group(1)
        if anime_id in seen:
            continue
        seen.add(anime_id)
        out.append((anime_id, html.unescape(alt.group(1))))
    return out


# -- per-anime detail page -------------------------------------------------
def anime_title(anime_id: str) -> str | None:
    try:
        page = _get(_DESC_URL.format(anime_id=anime_id)).text
    except Exception:
        return None
    return _extract_og_title(page)


def anime_cover(anime_id: str) -> str | None:
    # og:image on the anime's own detail page (verified: e.g.
    # https://anidb.app/anime/bleach-670 -> https://cdn.xlsbox.com/
    # poster/small/.../670.jpg). The browse/search results page has no
    # images of its own, so this is the only source — best-effort, any
    # failure just means no preview, not an error.
    try:
        page = _get(_DESC_URL.format(anime_id=anime_id)).text
    except Exception:
        return None
    m = re.search(r'property="og:image"\s+content="([^"]+)"', page)
    return html.unescape(m.group(1)).strip() if m else None


@dataclass
class AnimeDetail:
    official: str
    cover: str | None = None
    romaji: str | None = None
    synonyms: list[str] = field(default_factory=list)


def anime_detail(anime_id: str) -> AnimeDetail | None:
    """One combined fetch of the detail page: official title and cover art
    come from the same reliable og:title/og:image meta tags anime_title()/
    anime_cover() use above. The romaji subtitle and "Synonyms" sidebar
    line are scraped from page markup that isn't a stable, documented
    format the way og: meta tags are (unverified against the live site as
    of writing — flagged here deliberately) — so either, or both, can
    legitimately come back empty. Treat that as a degraded result, not a
    failure: a title with no variants is still a usable title, just with
    fewer options to offer.
    """
    try:
        page = _get(_DESC_URL.format(anime_id=anime_id)).text
    except Exception:
        return None
    official = _extract_og_title(page)
    if not official:
        return None

    cover_m = re.search(r'property="og:image"\s+content="([^"]+)"', page)
    cover = html.unescape(cover_m.group(1)).strip() if cover_m else None

    # Best-effort: the romanized title usually renders as a short subtitle
    # right after the English <h1> — take the first run of plain text
    # after </h1>, tolerating a few wrapper tags in between.
    romaji = None
    h1_end = re.search(r"</h1>", page, re.IGNORECASE)
    if h1_end:
        window = page[h1_end.end():h1_end.end() + 400]
        m2 = re.search(r">\s*([A-Za-z0-9][A-Za-z0-9:,.'\-\s]{2,79})\s*<", window)
        if m2:
            candidate = html.unescape(m2.group(1)).strip()
            if candidate and candidate.lower() != official.lower():
                romaji = candidate

    # Best-effort: a "Synonyms: X, Y, Z"-shaped sidebar line, wherever it
    # is and whatever tags surround the label.
    synonyms: list[str] = []
    syn_m = re.search(r"Synonyms\s*(?:</[^>]+>\s*)*:?\s*(?:<[^>]+>\s*)*([^<]{2,200})", page)
    if syn_m:
        raw = html.unescape(syn_m.group(1)).strip()
        synonyms = [s.strip() for s in raw.split(",") if s.strip()]

    return AnimeDetail(official=official, cover=cover, romaji=romaji, synonyms=synonyms)


# -- episodes / streams (AniCliDownloader only) ----------------------------
def episodes(anime_id: str) -> list[tuple[str, str]]:
    # anidb_episodes(): numeric id is whatever follows the last "-" in the
    # slug (e.g. "bleach-100" -> "100").
    numeric_id = anime_id.rsplit("-", 1)[-1]
    resp = _get(_EPISODES_URL.format(numeric_id=numeric_id))
    try:
        data = resp.json()
    except ValueError:
        data = []
    out: list[tuple[str, str]] = []
    entries = data if isinstance(data, list) else data.get("episodes", [])
    for e in entries:
        if not isinstance(e, dict):
            continue
        ep_id = e.get("id")
        ep_no = e.get("number")
        if ep_id is None or ep_no is None:
            continue
        out.append((str(ep_id), str(ep_no)))

    def _key(t: tuple[str, str]):
        try:
            return (0, float(t[1]))
        except ValueError:
            return (1, t[1])

    out.sort(key=_key)
    return out


def quality_links(episode_id: str, lang: str) -> tuple[list[tuple[str | None, str, str]], str | None]:
    # anidb_m3u8(): languages endpoint -> pick the embed_url whose entry
    # mentions our language -> scrape the player page for the m3u8 master
    # URL -> parse it into (height, label, url) tuples, best first. Returns
    # (streams, referer_url) — the embed page doubles as the Referer the
    # CDN expects on the actual segment/download requests.
    resp = _get(_LANGUAGES_URL.format(episode_id=episode_id))
    try:
        entries = resp.json()
    except ValueError:
        entries = []
    if isinstance(entries, dict):
        entries = entries.get("languages", [])
    if not isinstance(entries, list):
        entries = []
    embed_url = None
    for e in entries:
        if not isinstance(e, dict) or not e.get("embed_url"):
            continue
        if e.get("code") == lang or lang in str(e):
            embed_url = e["embed_url"]
            break
    if not embed_url:
        return [], None
    embed_page = _get(embed_url).text
    m = re.search(r"file:\s*'([^']+)'", embed_page)
    if not m:
        return [], embed_url
    master_url = m.group(1)
    master = _get(master_url).text
    return _parse_master_playlist(master, master_url), embed_url


def _parse_master_playlist(text: str, base_url: str) -> list[tuple[str | None, str, str]]:
    lines = text.splitlines()
    out: list[tuple[str | None, str, str]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("#EXT-X-STREAM-INF") and "I-FRAME" not in line:
            m = re.search(r"RESOLUTION=\d+x(\d+)", line)
            height = m.group(1) if m else None
            j = i + 1
            while j < len(lines) and (not lines[j].strip() or lines[j].startswith("#")):
                j += 1
            if j < len(lines):
                url = lines[j].strip()
                if not re.match(r"^https?://", url):
                    url = urljoin(base_url, url)
                label = f"{height}p" if height else "?"
                out.append((height, label, url))
            i = j
        i += 1

    def _h(t: tuple[str | None, str, str]) -> int:
        try:
            return int(t[0]) if t[0] else -1
        except ValueError:
            return -1

    out.sort(key=_h, reverse=True)
    return out
