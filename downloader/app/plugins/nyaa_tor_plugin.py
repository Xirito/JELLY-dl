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
resolve a free-text anime name against AniList's public GraphQL API
(services/anilist.py) to an official title (plus romaji/synonyms) and
cover art, before the frontend composes the actual `search(query=...)`
call above (typically as "[GroupTag] <chosen title>", though the
release-group tag itself is a frontend-only concept — this file never sees
or validates it, a torrent search here is just a query string like any
other).

This used to go through anidb.app (services/anidb.py, still used by
anicli_plugin.py) instead of AniList, but anidb.app sits behind Cloudflare
bot-mitigation that intermittently blocks it outright -- and this lookup
never actually needed anything anidb.app-specific in the first place, just
a title + cover. AniList has no episode/stream data, though, so it can't
replace anidb.app for anicli_plugin.py's actual downloads -- only this
metadata pre-search step moved.

AniList itself turned out to have its own failure mode: their whole API
can go down outage-wide on their end (confirmed live 2026-09-06, their
own message: "temporarily disabled due to severe stability issues"), with
no ETA and nothing fixable on this side. anime_search()/anime_details()
below try AniList first and, only if that fails AND services/mal.py has a
Client ID configured (mal.is_configured()), fall back to MyAnimeList's
official API -- separate company, separate infrastructure, so an AniList
outage doesn't take this down too. See services/mal.py's docstring for
how to get a Client ID; without one set, this behaves exactly as before
(AniList failures surface directly, no fallback attempted).

Each AnimeMatch.id below carries a "anilist:" or "mal:" prefix so
anime_details() knows which backend a given id came from -- the two
services' ids are unrelated numbers in unrelated namespaces, and a search
result picked from a fallback response has to route its detail lookup to
that same backend, not back to AniList.

Per-provider health (last_ok on SearchProvider, guarded by the same lock
as the sticky-primary swap) is tracked passively off these two calls --
whatever a real anime_search()/anime_details() call already tells us,
nothing more. There's deliberately no background health-check ping to
AniList/MyAnimeList: this is the same "don't make requests nothing asked
for" reasoning as the sticky failover itself. That means a provider that
hasn't been hit yet this run shows as unknown/"down" in provider_status()
below until something actually calls it -- see main.py's
/downloaders/{id}/provider-status route, which just reads this in-memory
state back out for the frontend's status dots.
"""
from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass
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
from ..services import anilist, mal
from ..services.torrent_client import TorrentClientManager
from .torrent_providers import NyaaProvider, TorrentProvider

log = logging.getLogger(__name__)


@dataclass
class SearchProvider:
    service: str
    dta: object  # the anilist or mal module -- not an actual type, just needs is_configured()/anime_detail()
    search: Callable[[str], list[tuple[str, str]]]
    error: type[Exception]
    # Passive health signal -- None means "never tried this run", True/False
    # is the outcome of the most recent real call. Only ever written by
    # _mark() below, under _search_providers_lock.
    last_ok: bool | None = None


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

    searchDB = [
        {
            "service": "anilist",
            "dta": anilist,
            "search": lambda query: anilist.search_anime(query),
            "error": anilist.AniListError,
        },
        {
            "service": "mal",
            "dta": mal,
            "search": lambda query: mal.search_anime(query),
            "error": mal.MalError,
        },
    ]

    search_providers: list[SearchProvider] = [SearchProvider(**db) for db in searchDB]

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
        # Guards reads/writes of the class-level `search_providers` list
        # AND each SearchProvider's `last_ok` field. anime_search() below
        # runs through FastAPI's threadpool (it's a plain `def`, not
        # `async def`), and NyaaTorDownloader is a singleton (one instance,
        # registered once in registry.py) shared by every request -- so two
        # searches landing at the same moment (two tabs, a double-click, two
        # different people) genuinely execute concurrently against the same
        # list. Without this lock, one request's `.reverse()` can land
        # mid-way through another request's read of `search_providers[0]`,
        # producing a result tagged with the wrong backend's id prefix or an
        # exception type that no longer matches what actually failed.
        self._search_providers_lock = threading.Lock()

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
    def _provider_order(self) -> tuple[SearchProvider, SearchProvider]:
        # One locked read, then everything below works off these two local
        # references -- never re-reads self.search_providers[0]/[1] again
        # for the rest of the call. That's what makes a concurrent
        # _promote() from another request safe to ignore mid-call: this
        # request's notion of "primary" and "fallback" can't change out
        # from under it once it's captured them.
        with self._search_providers_lock:
            return self.search_providers[0], self.search_providers[1]

    def _promote(self, provider: SearchProvider) -> None:
        # Sticky failover: once `provider` has proven itself as the
        # fallback, make it primary for future requests too, so the next
        # search doesn't pay AniList's timeout again during an outage (see
        # chat history for why this is deliberately sticky rather than
        # per-request). Idempotent under concurrency -- if two requests hit
        # a primary failure at the same moment, only the first to acquire
        # the lock actually swaps; the second sees `provider` is already
        # primary and no-ops, instead of swapping twice and landing back on
        # the original (still-broken) order.
        with self._search_providers_lock:
            if self.search_providers[0] is not provider:
                self.search_providers[0], self.search_providers[1] = (
                    self.search_providers[1], self.search_providers[0],
                )

    def _mark(self, provider: SearchProvider, ok: bool) -> None:
        # Passive health tracking -- called only from real anime_search()/
        # anime_details() outcomes below, never from a background poll. See
        # provider_status() for how this turns into the frontend's dots.
        with self._search_providers_lock:
            provider.last_ok = ok

    def provider_status(self) -> list[dict]:
        """Snapshot for the frontend's status dots (main.py's
        /downloaders/{id}/provider-status). Three states, matching the
        UI's green/orange/red:
          "active"  -- configured, last known call succeeded, AND currently
                       primary (this is what a search actually uses).
          "standby" -- configured and last known call succeeded, but not
                       currently primary (healthy fallback, sitting idle).
          "down"    -- not configured, OR never successfully called yet
                       this run, OR its last real call failed.
        Purely a read of in-memory state -- makes no request to AniList or
        MyAnimeList itself.
        """
        with self._search_providers_lock:
            primary = self.search_providers[0]
            snapshot = [
                (sp, sp.dta.is_configured(), sp.last_ok)
                for sp in self.search_providers
            ]
        result = []
        for sp, configured, last_ok in snapshot:
            if not configured or last_ok is not True:
                status = "down"
            elif sp is primary:
                status = "active"
            else:
                status = "standby"
            result.append({"service": sp.service, "status": status})
        return result

    def anime_search(self, query: str) -> list[AnimeMatch]:
        query = (query or "").strip()
        if not query:
            raise ValueError("search query is required")
        errors: list[str] = []
        primary, fallback = self._provider_order()
        try:
            results = primary.search(query)
            source = primary.service
            self._mark(primary, True)
        except primary.error as e:
            errors.append(f" {primary.service}: {e}")
            self._mark(primary, False)
            if not fallback.dta.is_configured():
                raise
            log.warning("%s search failed, falling back to %s: %s", primary.service, fallback.service, e)
            self._promote(fallback)
            try:
                results = fallback.search(query)
                source = fallback.service
                self._mark(fallback, True)
            except fallback.error as e2:
                errors.append(f" {fallback.service}: {e2}")
                self._mark(fallback, False)
                raise RuntimeError("; ".join(errors)) from e2
        return [
            AnimeMatch(id=f"{source}:{anime_id}", title=title)
            for anime_id, title in results[:20]
        ]

    def anime_details(self, anime_id: str) -> AnimeDetails:
        # Route to whichever backend the id's prefix names (see module
        # docstring) -- a result picked from a MyAnimeList fallback list
        # must not be looked up against AniList, and vice versa.
        source, sep, raw_id = anime_id.partition(":")
        sprovider = next((sp for sp in self.search_providers if sp.service == source), None)
        if not sprovider:
            raise ValueError("unknown anime source")

        not_found = sprovider.error("couldn't load that anime's page")
        try:
            detail = sprovider.dta.anime_detail(raw_id)
        except sprovider.error:
            # The API call itself failed (network/outage) -- a real health
            # signal. A plain "not found" below is NOT this: the API
            # answered fine, it just had nothing for this id.
            self._mark(sprovider, False)
            raise
        self._mark(sprovider, True)

        if detail is None:
            raise not_found
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
