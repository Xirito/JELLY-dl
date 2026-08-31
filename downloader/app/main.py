"""API layer — thin FastAPI routes, DTO-only, delegates to services."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import MEDIA_SERVER_TARGETS
from .models import AnimeDetails, AnimeMatch, DownloadRequest, DownloaderInfo, JobInfo
from .registry import build_default_registry
from .services.directory_browser import DirectoryBrowser
from .services.download_service import DownloadService
from .services.path_resolver import PathEscapeError, PathResolver

app = FastAPI(title="Downloader", version="0.1.0")

registry = build_default_registry()
resolver = PathResolver()
browser = DirectoryBrowser(resolver)
service = DownloadService(registry, resolver)

WEB_DIR = Path(__file__).parent / "web"


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/downloaders", response_model=list[DownloaderInfo])
def downloaders():
    return [DownloaderInfo(id=d.id, name=d.name, capabilities=d.capabilities)
            for d in registry.all()]


@app.get("/downloaders/{downloader_id}/search")
def search(downloader_id: str, q: str = Query(""), parent: str | None = Query(None)):
    d = _get_downloader(downloader_id)
    if not d.capabilities.supports_search:
        raise HTTPException(400, f"{downloader_id} does not support search")
    if not parent and not q:
        raise HTTPException(422, "q is required")
    try:
        return d.search(q, parent)
    except Exception as e:
        raise HTTPException(502, f"search failed: {e}")


@app.get("/downloaders/{downloader_id}/formats")
def formats(downloader_id: str, source: str = Query(min_length=1)):
    d = _get_downloader(downloader_id)
    if not d.capabilities.supports_format_listing:
        raise HTTPException(400, f"{downloader_id} does not support format listing")
    try:
        return d.list_formats(source)
    except Exception as e:
        raise HTTPException(502, f"format listing failed: {e}")


@app.get("/downloaders/{downloader_id}/anime-search", response_model=list[AnimeMatch])
def anime_search(downloader_id: str, q: str = Query(min_length=1)):
    d = _get_downloader(downloader_id)
    if not d.capabilities.supports_anime_lookup:
        raise HTTPException(400, f"{downloader_id} does not support anime lookup")
    fn = getattr(d, "anime_search", None)
    if not callable(fn):
        raise HTTPException(500, f"{downloader_id} advertises anime lookup but doesn't implement it")
    try:
        return fn(q)
    except Exception as e:
        raise HTTPException(502, f"anime search failed: {e}")


@app.get("/downloaders/{downloader_id}/anime/{anime_id}", response_model=AnimeDetails)
def anime_details(downloader_id: str, anime_id: str):
    d = _get_downloader(downloader_id)
    if not d.capabilities.supports_anime_lookup:
        raise HTTPException(400, f"{downloader_id} does not support anime lookup")
    fn = getattr(d, "anime_details", None)
    if not callable(fn):
        raise HTTPException(500, f"{downloader_id} advertises anime lookup but doesn't implement it")
    try:
        return fn(anime_id)
    except Exception as e:
        raise HTTPException(502, f"anime lookup failed: {e}")


@app.post("/downloads", response_model=JobInfo)
def create_download(req: DownloadRequest):
    if req.format_selector.mode == "manual" and not req.format_selector.format_id:
        raise HTTPException(422, "manual mode requires format_id")
    try:
        return service.start(req)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except PathEscapeError as e:
        raise HTTPException(400, str(e))


@app.get("/downloads", response_model=list[JobInfo])
def list_downloads():
    return service.all()


@app.get("/downloads/{job_id}", response_model=JobInfo)
def get_download(job_id: str):
    try:
        return service.get(job_id)
    except KeyError:
        raise HTTPException(404, "no such job")


@app.post("/downloads/{job_id}/cancel", response_model=JobInfo)
def cancel_download(job_id: str):
    try:
        return service.cancel(job_id)
    except KeyError:
        raise HTTPException(404, "no such job")


@app.get("/interfaces")
def interfaces():
    return [{"token": t.token, "placeholder": f"${t.token}$"}
            for t in MEDIA_SERVER_TARGETS.values()]


@app.get("/paths/suggest")
def suggest(path: str = "", downloader_id: str = "ytdlp"):
    d = _get_downloader(downloader_id)
    return browser.suggest(path, d)


def _get_downloader(downloader_id: str):
    try:
        return registry.get(downloader_id)
    except KeyError as e:
        raise HTTPException(404, str(e))


# Everything else the built frontend serves out of the dist root — the PWA
# manifest, service worker + workbox runtime, and icons (manifest.webmanifest,
# sw.js, workbox-*.js, registerSW.js, icon-*.png, apple-touch-icon.png,
# favicon.svg), plus the hashed /assets/*.js|css chunks — isn't handled by
# any route above. Mounted LAST, after every API route, so a request only
# reaches it once nothing above already matched — this was the actual PWA
# install bug: manifest.webmanifest and sw.js were both 404ing, so Chrome
# had nothing valid to base an install decision on. Guarded so a plain local
# `uvicorn app.main:app` still starts before the frontend has ever been built.
if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=WEB_DIR), name="web-root")
