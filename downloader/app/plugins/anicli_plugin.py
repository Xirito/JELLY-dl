"""AniCliDownloader — second Downloader implementation.

Unlike YtdlpDownloader, this does NOT shell out to the actual `ani-cli`
shell script (which is built for an interactive terminal with fzf). Instead
it re-implements the same anidb.app scraping steps the script itself uses
(see https://github.com/pystardust/ani-cli, function names kept as comments
below for cross-reference), driven from Python so it fits the request/
response shape the rest of this app expects. All ani-cli/anidb.app-specific
knowledge — endpoints, page-scraping regexes, the HLS master-playlist
format — stays inside this file.

Two things make anime downloading a different shape from yt-dlp, and both
are handled generically rather than bolted on as ani-cli-only code:

1. Searching is two-level: a query matches *anime titles*, not directly
   playable sources — you still have to pick an episode. `SearchResult.
   is_container` + the `parent` argument to `search()` express that: the
   top-level search returns containers (anime), and the frontend re-calls
   search(query="", parent=<anime source>) to list the leaves (episodes).
2. Formats are a small fixed set of resolutions pulled straight out of the
   HLS master playlist, not yt-dlp's rich per-format metadata — and there's
   no separate "audio only" or "video only" stream to offer, so
   `capabilities.available_modes` only advertises best_video_audio + manual.

Cloudflare sits in front of anidb.app, which is why the upstream shell
script insists on curl-impersonate binaries. `curl_cffi` is the Python
equivalent (ships its own impersonated TLS/JA3 fingerprints) and is used
for every request this plugin makes to anidb.app.
"""
from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin

from curl_cffi import requests as curl_requests

from ..config import DOWNLOAD_ROOT
from ..models import (
    DownloaderCapabilities,
    DownloadOptions,
    DownloadProgress,
    DownloadResult,
    FormatOption,
    FormatSelector,
    SearchResult,
)

_BASE = "https://anidb.app"
_SEARCH_URL = _BASE + "/browse?q={q}"
_DESC_URL = _BASE + "/anime/{anime_id}"
_EPISODES_URL = _BASE + "/api/frontend/anime/{numeric_id}/episodes"
_LANGUAGES_URL = _BASE + "/api/frontend/episode/{episode_id}/languages"

_IMPERSONATE = "chrome124"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

_SOURCE_SEP = "::"  # anime_id::ep_no  (leaf source token)


class AniCliError(RuntimeError):
    pass


def _get(url: str, timeout: int = 15) -> curl_requests.Response:
    resp = curl_requests.get(url, impersonate=_IMPERSONATE,
                              headers={"User-Agent": _UA}, timeout=timeout)
    resp.raise_for_status()
    return resp


class AniCliDownloader:
    id = "anicli"
    name = "ani-cli (anime)"
    capabilities = DownloaderCapabilities(
        supports_search=True,
        supports_format_listing=True,
        supports_manual_format_select=True,
        supports_metadata_embed=False,
        supports_dub_toggle=True,
        # No standalone audio/video-only stream exists for an HLS episode —
        # only "best available" or a manually picked resolution make sense.
        available_modes=["best_video_audio", "manual"],
    )

    def __init__(self, download_root: Path | None = None):
        self.default_download_root = download_root or (DOWNLOAD_ROOT / "anime")

    # -- search / browse ----------------------------------------------------
    def search(self, query: str, parent: str | None = None) -> list[SearchResult]:
        if parent:
            return self._list_episodes(parent)
        query = (query or "").strip()
        if not query:
            raise AniCliError("search query is required")
        return [
            SearchResult(source=anime_id, title=title, is_container=True)
            for anime_id, title in self._search_anime(query)[:20]
        ]

    def _search_anime(self, query: str) -> list[tuple[str, str]]:
        # anidb_search(): GET /browse?q=..., pull (id, title) pairs out of
        # each <a href=".../anime/<id>" ...alt="<title>"...> anchor.
        page = _get(_SEARCH_URL.format(q=query.replace(" ", "+"))).text
        if "Just a moment" in page:
            raise AniCliError("blocked by Cloudflare — try again in a bit")
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

    def _list_episodes(self, anime_id: str) -> list[SearchResult]:
        eps = self._episodes(anime_id)
        if not eps:
            raise AniCliError("no episodes found for that anime")
        return [
            SearchResult(source=f"{anime_id}{_SOURCE_SEP}{ep_no}",
                         title=f"Episode {ep_no}", is_container=False)
            for _ep_id, ep_no in eps
        ]

    def _episodes(self, anime_id: str) -> list[tuple[str, str]]:
        # anidb_episodes(): numeric id is whatever follows the last "-" in
        # the slug (e.g. "bleach-100" -> "100").
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
        # numeric sort where possible, stable otherwise
        def _key(t: tuple[str, str]):
            try:
                return (0, float(t[1]))
            except ValueError:
                return (1, t[1])
        out.sort(key=_key)
        return out

    def _anime_title(self, anime_id: str) -> str | None:
        try:
            page = _get(_DESC_URL.format(anime_id=anime_id)).text
        except Exception:
            return None
        m = re.search(r'property="og:title"\s+content="([^"]+)"', page)
        if not m:
            m = re.search(r"<title>([^<]+)</title>", page)
        return html.unescape(m.group(1)).strip() if m else None

    def _split_leaf(self, source: str) -> tuple[str, str]:
        if _SOURCE_SEP not in source:
            raise AniCliError(
                "invalid source — pick an episode from search results "
                "(top-level results need a drill-down step for this backend)"
            )
        anime_id, ep_no = source.split(_SOURCE_SEP, 1)
        if not anime_id or not ep_no:
            raise AniCliError("invalid source")
        return anime_id, ep_no

    def _resolve_episode_id(self, anime_id: str, ep_no: str) -> str:
        for ep_id, no in self._episodes(anime_id):
            if no == ep_no:
                return ep_id
        raise AniCliError(f"episode {ep_no} not found")

    # -- formats -------------------------------------------------------------
    def list_formats(self, source: str) -> list[FormatOption]:
        anime_id, ep_no = self._split_leaf(source)
        ep_id = self._resolve_episode_id(anime_id, ep_no)
        # Quality tiers are usually identical across sub/dub for the same
        # episode; list against sub (almost always available) and let
        # download() re-resolve against the actually-requested language.
        streams, _referer = self._quality_links(ep_id, lang="jpn")
        return [
            FormatOption(
                format_id=label,
                label=label,
                resolution=f"{height}p" if height else None,
                ext="mp4",
                has_video=True,
                has_audio=True,
            )
            for height, label, _url in streams
        ]

    def _quality_links(
        self, episode_id: str, lang: str
    ) -> tuple[list[tuple[str | None, str, str]], str | None]:
        # anidb_m3u8(): languages endpoint -> pick the embed_url whose entry
        # mentions our language -> scrape the player page for the m3u8
        # master URL -> parse it into (height, label, url) tuples, best first.
        # Returns (streams, referer_url) — the embed page doubles as the
        # Referer the CDN expects on the actual segment/download requests.
        resp = _get(_LANGUAGES_URL.format(episode_id=episode_id))
        try:
            entries = resp.json()
        except ValueError:
            entries = []
        # The endpoint wraps the array in {"languages": [...]}, not a bare list.
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
        return self._parse_master_playlist(master, master_url), embed_url

    @staticmethod
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

    @staticmethod
    def _select_quality(streams: list[tuple[str | None, str, str]], want: str | None):
        if not streams:
            return None
        if not want or want == "best":
            return streams[0]
        if want == "worst":
            return streams[-1]
        target = want[:-1] if want.endswith("p") else want
        for s in streams:
            if s[0] == target:
                return s
        for s in streams:
            if want in s[1]:
                return s
        return streams[0]  # fall back to best, mirroring ani-cli's own behavior

    # -- download --------------------------------------------------------
    def download(
        self,
        source: str,
        format_selector: FormatSelector,
        destination: Path,
        on_progress: Callable[[DownloadProgress], None],
        options: DownloadOptions | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> DownloadResult:
        try:
            if should_cancel and should_cancel():
                return DownloadResult(error="cancelled by user")
            anime_id, ep_no = self._split_leaf(source)
            lang = "eng" if (options and options.dub) else "jpn"
            ep_id = self._resolve_episode_id(anime_id, ep_no)
            streams, referer = self._quality_links(ep_id, lang)
            if not streams:
                kind = "dub" if lang == "eng" else "sub"
                raise AniCliError(f"no {kind} source found for episode {ep_no}")
            want = format_selector.format_id if format_selector.mode == "manual" else "best"
            chosen = self._select_quality(streams, want)
            if not chosen:
                raise AniCliError("no playable stream found")
            _height, _label, video_link = chosen
            anime_title = self._anime_title(anime_id) or anime_id
        except Exception as e:
            on_progress(DownloadProgress(status="error"))
            return DownloadResult(error=str(e)[:2000])

        destination.mkdir(parents=True, exist_ok=True)
        safe_title = re.sub(r'[\\/:*?"<>|]', "_", f"{anime_title} - Episode {ep_no}")
        final_path: dict[str, str] = {}

        import yt_dlp  # local import: only this method needs it

        def hook(d: dict):
            if should_cancel and should_cancel():
                raise yt_dlp.utils.DownloadCancelled("cancelled by user")
            st = d.get("status")
            if st == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                total = int(total) if total else None
                done = int(d.get("downloaded_bytes") or 0)
                on_progress(DownloadProgress(
                    status="downloading",
                    downloaded_bytes=done,
                    total_bytes=total,
                    speed_bps=d.get("speed"),
                    eta_s=d.get("eta"),
                    filename=Path(d.get("filename", "")).name or None,
                    percent=(done / total * 100) if total else None,
                ))
            elif st == "finished":
                final_path["p"] = d.get("filename", "")
                on_progress(DownloadProgress(
                    status="processing",
                    filename=Path(d.get("filename", "")).name or None,
                    percent=100.0,
                ))

        def pp_hook(d: dict):
            if d.get("status") == "finished":
                info = d.get("info_dict") or {}
                fp = info.get("filepath")
                if fp:
                    final_path["p"] = fp

        opts = {
            "quiet": True,
            "no_warnings": True,
            "outtmpl": str(destination / f"{safe_title}.%(ext)s"),
            "progress_hooks": [hook],
            "postprocessor_hooks": [pp_hook],
            "merge_output_format": "mp4",
            "noplaylist": True,
            "http_headers": {"User-Agent": _UA, "Referer": referer or video_link},
        }

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([video_link])
            if not final_path.get("p"):
                guessed = destination / f"{safe_title}.mp4"
                if guessed.exists():
                    final_path["p"] = str(guessed)
            on_progress(DownloadProgress(status="finished",
                                         filename=Path(final_path.get("p", "")).name or None,
                                         percent=100.0))
            return DownloadResult(filepath=final_path.get("p") or None)
        except yt_dlp.utils.DownloadCancelled:
            on_progress(DownloadProgress(status="cancelled"))
            return DownloadResult(error="cancelled by user")
        except Exception as e:
            on_progress(DownloadProgress(status="error"))
            return DownloadResult(error=str(e)[:2000])

    # -- metadata helper (used by the service layer for job titles) --------
    def probe_title(self, source: str) -> str | None:
        try:
            anime_id, ep_no = self._split_leaf(source)
        except AniCliError:
            return None
        title = self._anime_title(anime_id)
        return f"{title} - Episode {ep_no}" if title else f"Episode {ep_no}"
