"""DTOs shared across layers. Nothing in here is downloader-specific."""
from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel


class DownloaderCapabilities(BaseModel):
    supports_search: bool
    supports_format_listing: bool
    supports_manual_format_select: bool
    supports_metadata_embed: bool = False
    supports_dub_toggle: bool = False   # sub/dub language choice (anime backends)
    # Which FormatSelector.mode presets this backend can actually resolve —
    # the frontend hides preset buttons not in this list. Defaults to all
    # four so existing plugins (yt-dlp) don't need to opt in explicitly.
    available_modes: list[str] = [
        "best_video_audio", "best_audio", "best_video_only", "manual",
    ]


class DownloaderInfo(BaseModel):
    id: str
    name: str
    capabilities: DownloaderCapabilities


class SearchResult(BaseModel):
    source: str          # what to feed back into formats/download (usually a URL)
    title: str
    uploader: Optional[str] = None
    duration_s: Optional[float] = None
    thumbnail: Optional[str] = None
    # True when `source` isn't directly downloadable yet and instead needs a
    # follow-up search(query="", parent=source) to drill in (e.g. an anime
    # title that still needs an episode picked). yt-dlp results are always
    # leaves (False); a backend like ani-cli returns containers at the top
    # level and leaves one level down.
    is_container: bool = False


class FormatOption(BaseModel):
    format_id: str
    label: str
    resolution: Optional[str] = None
    ext: Optional[str] = None
    vcodec: Optional[str] = None
    acodec: Optional[str] = None
    has_video: bool = False
    has_audio: bool = False
    filesize_approx: Optional[int] = None
    tbr: Optional[float] = None   # total bitrate, kbps


class FormatSelector(BaseModel):
    mode: Literal["manual", "best_audio", "best_video_audio", "best_video_only"]
    format_id: Optional[str] = None  # required when mode == "manual"


class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    finished = "finished"
    error = "error"


class DownloadProgress(BaseModel):
    status: str = "starting"
    downloaded_bytes: int = 0
    total_bytes: Optional[int] = None
    speed_bps: Optional[float] = None
    eta_s: Optional[float] = None
    filename: Optional[str] = None
    percent: Optional[float] = None


class DownloadResult(BaseModel):
    filepath: Optional[str] = None
    error: Optional[str] = None


class DownloadOptions(BaseModel):
    embed_metadata: bool = False
    dub: bool = False   # ignored by backends without supports_dub_toggle


class DownloadRequest(BaseModel):
    downloader_id: str
    source: str
    format_selector: FormatSelector
    destination_path: str = ""   # raw user path, may start with $token$
    options: DownloadOptions = DownloadOptions()


class JobInfo(BaseModel):
    id: str
    downloader_id: str
    source: str
    title: Optional[str] = None
    destination: str
    status: JobStatus
    progress: DownloadProgress
    result: Optional[DownloadResult] = None


class MediaServerTarget(BaseModel):
    token: str       # "jellyfin", "plex", ...
    base_path: Path  # e.g. /media
