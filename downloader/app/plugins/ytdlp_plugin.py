"""YtdlpDownloader — first Downloader implementation.

Wraps the yt-dlp Python module (not the CLI) for structured output and
progress hooks. All yt-dlp-specific knowledge (format-string syntax, raw
format dicts) stays inside this file.

NOTE: yt-dlp is deliberately NOT version-pinned (sites change frequently;
pinning causes format errors). Muxing of separate bestvideo+bestaudio
streams is delegated to ffmpeg — yt-dlp drives it for its own downloads,
and the shared FfmpegPostProcessor is available for plugins that need to
merge/remux themselves.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

import yt_dlp

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
from ..services.ffmpeg import FfmpegPostProcessor


def _human_res(f: dict) -> str | None:
    if f.get("resolution") and f["resolution"] != "audio only":
        return f["resolution"]
    if f.get("height"):
        return f"{f.get('width', '?')}x{f['height']}"
    return None


class YtdlpDownloader:
    id = "ytdlp"
    name = "yt-dlp"
    capabilities = DownloaderCapabilities(
        supports_search=True,           # via ytsearch: pseudo-URLs
        supports_format_listing=True,
        supports_manual_format_select=True,
        supports_metadata_embed=True,   # via FFmpegMetadata postprocessor
    )

    def __init__(self, postprocessor: FfmpegPostProcessor | None = None,
                 download_root: Path | None = None):
        self.post = postprocessor or FfmpegPostProcessor()
        self.default_download_root = download_root or DOWNLOAD_ROOT

    # -- search ------------------------------------------------------------
    def search(self, query: str, parent: str | None = None) -> list[SearchResult]:
        # yt-dlp results are always leaves; there's no container/drill-down
        # concept here, so `parent` is accepted for Protocol conformance
        # but has nothing to do.
        opts = {"quiet": True, "no_warnings": True, "extract_flat": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch10:{query}", download=False)
        results = []
        for e in (info or {}).get("entries") or []:
            if not e:
                continue
            results.append(SearchResult(
                source=e.get("url") or e.get("webpage_url") or e.get("id", ""),
                title=e.get("title") or "(untitled)",
                uploader=e.get("uploader") or e.get("channel"),
                duration_s=e.get("duration"),
                thumbnail=(e.get("thumbnails") or [{}])[-1].get("url"),
            ))
        return results

    # -- formats -----------------------------------------------------------
    def list_formats(self, source: str) -> list[FormatOption]:
        opts = {"quiet": True, "no_warnings": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(source, download=False)
        out: list[FormatOption] = []
        for f in (info or {}).get("formats") or []:
            has_v = f.get("vcodec") not in (None, "none")
            has_a = f.get("acodec") not in (None, "none")
            if not has_v and not has_a:
                continue
            res = _human_res(f)
            kind = "A/V" if (has_v and has_a) else ("video" if has_v else "audio")
            bits = [f.get("ext") or "?", kind]
            if res:
                bits.append(res)
            if f.get("tbr"):
                bits.append(f"{round(f['tbr'])}k")
            out.append(FormatOption(
                format_id=str(f["format_id"]),
                label=" · ".join(bits),
                resolution=res,
                ext=f.get("ext"),
                vcodec=f.get("vcodec") if has_v else None,
                acodec=f.get("acodec") if has_a else None,
                has_video=has_v,
                has_audio=has_a,
                filesize_approx=f.get("filesize") or f.get("filesize_approx"),
                tbr=f.get("tbr"),
            ))
        return out

    # -- download ----------------------------------------------------------
    def _fmt_string(self, sel: FormatSelector) -> str:
        # yt-dlp format-selection syntax lives ONLY here.
        if sel.mode == "manual":
            if not sel.format_id:
                raise ValueError("manual mode requires format_id")
            # if the chosen format is video-only, grab bestaudio alongside it
            return f"{sel.format_id}+bestaudio/{sel.format_id}"
        return {
            "best_audio": "bestaudio/best",
            "best_video_audio": "bestvideo*+bestaudio/best",
            "best_video_only": "bestvideo",
        }[sel.mode]

    def download(
        self,
        source: str,
        format_selector: FormatSelector,
        destination: Path,
        on_progress: Callable[[DownloadProgress], None],
        options: DownloadOptions | None = None,
    ) -> DownloadResult:
        destination.mkdir(parents=True, exist_ok=True)
        final_path: dict[str, str] = {}

        def hook(d: dict):
            st = d.get("status")
            if st == "downloading":
                # total_bytes_estimate can be a float — DTO wants ints
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
            "format": self._fmt_string(format_selector),
            "outtmpl": str(destination / "%(title)s [%(id)s].%(ext)s"),
            "progress_hooks": [hook],
            "postprocessor_hooks": [pp_hook],
            "merge_output_format": "mkv",
            "noplaylist": True,
            "restrictfilenames": False,
        }
        postprocessors: list[dict] = []
        if format_selector.mode == "best_audio":
            # Music libraries don't index .webm — extract to a native audio
            # container (.opus/.m4a) instead of leaving bestaudio in webm.
            opts.pop("merge_output_format", None)
            postprocessors.append(
                {"key": "FFmpegExtractAudio", "preferredcodec": "best"}
            )
        if options and options.embed_metadata:
            # Write title/artist/date/chapters tags into the output file.
            postprocessors.append({"key": "FFmpegMetadata"})
        if format_selector.mode == "best_audio":
            # Auto-embed the thumbnail as cover art on audio downloads.
            opts["writethumbnail"] = True
            postprocessors.insert(0, {"key": "FFmpegThumbnailsConvertor",
                                      "format": "png", "when": "before_dl"})
            postprocessors.append({"key": "EmbedThumbnail",
                                   "already_have_thumbnail": False})
        if postprocessors:
            opts["postprocessors"] = postprocessors

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(source, download=True)
            if not final_path.get("p") and info:
                reqs = info.get("requested_downloads") or []
                if reqs:
                    final_path["p"] = reqs[0].get("filepath", "")
            on_progress(DownloadProgress(status="finished",
                                         filename=Path(final_path.get("p", "")).name or None,
                                         percent=100.0))
            return DownloadResult(filepath=final_path.get("p"))
        except Exception as e:  # surfaced to the job store
            on_progress(DownloadProgress(status="error"))
            return DownloadResult(error=str(e)[:2000])

    # -- metadata helper (used by the service layer for job titles) --------
    def probe_title(self, source: str) -> str | None:
        try:
            opts = {"quiet": True, "no_warnings": True, "extract_flat": True}
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(source, download=False)
            return (info or {}).get("title")
        except Exception:
            return None
