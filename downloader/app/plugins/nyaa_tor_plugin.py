"""NyaaTorDownloader — third Downloader implementation, and a different
shape from the other two. yt-dlp and ani-cli both resolve a source and
pull it down in one HTTP-ish fetch. A torrent is a peer-to-peer transfer
that takes real wall-clock time and needs a real BitTorrent client running
alongside it — that client (qbittorrent-nox) lives entirely in
services/torrent_client.py, started only when a torrent job needs it and
stopped the moment nothing does (see that file's docstring for the
leech-only enforcement — this plugin never seeds). This file stays a thin
adapter around it, same Downloader Protocol shape as the other two.

Search is provider-based (torrent_providers.py) rather than hardcoded to
one site — nyaa.si is the first indexer, not the only one this is meant to
support. Results are flat, like yt-dlp's (no container/drill-down step,
unlike ani-cli's anime->episode split) — a torrent search result already
IS the thing to download, nothing to pick after it.

There's a separate, optional pre-search step layered on top of that flat
search: anime_search()/anime_details() (capabilities.supports_anime_lookup)
resolve a free-text anime name against anidb.app — same shared client
ani-cli's plugin uses (services/anidb.py) — to an official title (plus
romaji/synonyms, if anidb.app has them) and cover art, before the frontend
composes the actual `search(query=...)` call above (typically as
"[GroupTag] <chosen title>", though the release-group tag itself is a
frontend-only concept — this file never sees or validates it, a torrent
search here is just a query string like any other).
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse

from ..config import DOWNLOAD_ROOT
from ..models import (
    AnimeDetails,
    AnimeMatch,
    DownloaderCapabilities,
    DownloadOptions,
    DownloadProgress,
    DownloadResult,
    FormatOption,
    FormatSelector,
    SearchResult,
)
from ..services import anidb
from ..services.torrent_client import TorrentClientManager
from .torrent_providers import NyaaProvider, TorrentProvider


class NyaaTorDownloader:
    id = "nyaa_tor"
    name = "Nyaa (torrent)"
    capabilities = DownloaderCapabilities(
        supports_search=True,
        supports_format_listing=False,   # a torrent IS the format — there's
                                          # no separate quality picker
        supports_manual_format_select=False,
        supports_metadata_embed=False,
        supports_dub_toggle=False,
        # Torrents don't split into separate video/audio-only streams the
        # way a yt-dlp source can, and "manual" has nothing to list against
        # — best_video_audio (meaning: whatever the torrent contains) is
        # the only preset that makes sense here.
        available_modes=["best_video_audio"],
        supports_anime_lookup=True,
    )

    def __init__(
        self,
        client: TorrentClientManager | None = None,
        download_root: Path | None = None,
        providers: list[TorrentProvider] | None = None,
    ):
        self.default_download_root = download_root or (DOWNLOAD_ROOT / "torrents")
        self.client = client or TorrentClientManager(
            profile_dir=DOWNLOAD_ROOT / ".torrent-client"
        )
        self.providers: list[TorrentProvider] = (
            providers if providers is not None else [NyaaProvider()]
        )

    # -- search --------------------------------------------------------
    def search(self, query: str, parent: str | None = None) -> list[SearchResult]:
        # No container/leaf split — every provider hands back directly
        # downloadable results, so `parent` is accepted for Protocol
        # conformance but never used.
        query = (query or "").strip()
        if not query:
            raise ValueError("search query is required")

        results: list[SearchResult] = []
        errors: list[str] = []
        for provider in self.providers:
            try:
                items = provider.search(query)
            except Exception as e:
                errors.append(f"{provider.name}: {e}")
                continue
            for item in items:
                # Nyaa has no cover art to offer — the confirmation
                # thumbnail box just keeps showing the header animation for
                # this backend, which is fine; size + seeders/leechers is
                # the actually-useful "is this the one" signal here, so it
                # goes in `uploader` (repurposed as a subtitle field — the
                # frontend already renders it as one, and nothing else in
                # SearchResult fits).
                bits = []
                if item.size:
                    bits.append(item.size)
                if item.seeders is not None or item.leechers is not None:
                    bits.append(f"{item.seeders or 0}↑ {item.leechers or 0}↓")
                results.append(SearchResult(
                    source=item.magnet,
                    title=item.title,
                    uploader=" · ".join(bits) or None,
                    is_container=False,
                ))
        if not results and errors:
            # Only surface provider errors when they left us with nothing
            # at all — one broken indexer among several shouldn't sink a
            # search that other providers still answered.
            raise RuntimeError("; ".join(errors))
        return results

    # -- anime lookup (optional pre-search step, see module docstring) -------
    def anime_search(self, query: str) -> list[AnimeMatch]:
        query = (query or "").strip()
        if not query:
            raise ValueError("search query is required")
        return [
            AnimeMatch(id=anime_id, title=title)
            for anime_id, title in anidb.search_anime(query)[:20]
        ]

    def anime_details(self, anime_id: str) -> AnimeDetails:
        detail = anidb.anime_detail(anime_id)
        if detail is None:
            raise anidb.AniDbError("couldn't load that anime's page")
        variants = [detail.official]
        if detail.romaji and detail.romaji.lower() != detail.official.lower():
            variants.append(detail.romaji)
        for syn in detail.synonyms:
            if syn and syn.lower() not in (v.lower() for v in variants):
                variants.append(syn)
        return AnimeDetails(title=detail.official, cover=detail.cover, title_variants=variants)

    # -- formats -------------------------------------------------------------
    def list_formats(self, source: str) -> list[FormatOption]:
        return []  # unreachable via the API — supports_format_listing=False

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
        job_tag = f"jdl-{uuid.uuid4().hex[:16]}"
        filepath, error = self.client.download(
            magnet=source,
            destination=destination,
            job_tag=job_tag,
            on_progress=on_progress,
            should_cancel=should_cancel,
        )
        if error:
            status = "cancelled" if error == "cancelled by user" else "error"
            on_progress(DownloadProgress(status=status))
            return DownloadResult(error=error)
        on_progress(DownloadProgress(
            status="finished", percent=100.0,
            filename=Path(filepath).name if filepath else None,
        ))
        return DownloadResult(filepath=filepath)

    # -- metadata helper (used by the service layer for job titles) --------
    def probe_title(self, source: str) -> str | None:
        # Every magnet nyaapy builds carries dn=<title> (see NyaaProvider) —
        # free, no extra request needed.
        try:
            dn = parse_qs(urlparse(source).query).get("dn")
            return dn[0] if dn else None
        except Exception:
            return None
