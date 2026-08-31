"""AniCliDownloader — second Downloader implementation.

Unlike YtdlpDownloader, this does NOT shell out to the actual `ani-cli`
shell script (which is built for an interactive terminal with fzf). Instead
it drives the shared anidb.app client in services/anidb.py (extracted here
so NyaaTorDownloader can reuse the same site knowledge — see that file's
docstring) to fit the request/response shape the rest of this app expects.

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
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

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
from ..services import anidb
from ..services.anidb import AniDbError as AniCliError

_SOURCE_SEP = "::"  # anime_id::ep_no  (leaf source token)


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
            for anime_id, title in anidb.search_anime(query)[:20]
        ]

    def _list_episodes(self, anime_id: str) -> list[SearchResult]:
        eps = anidb.episodes(anime_id)
        if not eps:
            raise AniCliError("no episodes found for that anime")
        # One extra request here, for the single anime just picked — not per
        # search result. The browse/search results page has no cover images
        # of its own (verified against the live site), only each anime's own
        # detail page does (its og:image tag). Every episode gets tagged with
        # the same cover so the frontend can show a "you're about to
        # download this" preview as soon as the episode list appears,
        # without a separate per-episode fetch.
        cover = anidb.anime_cover(anime_id)
        return [
            SearchResult(source=f"{anime_id}{_SOURCE_SEP}{ep_no}",
                         title=f"Episode {ep_no}", is_container=False,
                         thumbnail=cover)
            for _ep_id, ep_no in eps
        ]

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
        for ep_id, no in anidb.episodes(anime_id):
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
        streams, _referer = anidb.quality_links(ep_id, lang="jpn")
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
            streams, referer = anidb.quality_links(ep_id, lang)
            if not streams:
                kind = "dub" if lang == "eng" else "sub"
                raise AniCliError(f"no {kind} source found for episode {ep_no}")
            want = format_selector.format_id if format_selector.mode == "manual" else "best"
            chosen = self._select_quality(streams, want)
            if not chosen:
                raise AniCliError("no playable stream found")
            _height, _label, video_link = chosen
            anime_title = anidb.anime_title(anime_id) or anime_id
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
            "http_headers": {"User-Agent": anidb.USER_AGENT, "Referer": referer or video_link},
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
        title = anidb.anime_title(anime_id)
        return f"{title} - Episode {ep_no}" if title else f"Episode {ep_no}"
