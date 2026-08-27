# Media stack — Jellyfin + Downloader webapp

Runs on TrueNAS (plain docker compose). Two services:

- **jellyfin** — media server, port 8096, library at `/mnt/FA_1/media`, NVIDIA GPU passthrough for transcoding.
- **downloader** — FastAPI webapp, port 8790, pluggable downloader backends (yt-dlp today, ani-cli or others later).

## Downloader architecture

Layered per `downloaderwebapparchitecture.md` (kept in this repo's `docs/`):

```
Presentation (app/web/index.html)      knows only DTOs + capability flags
API          (app/main.py)             thin FastAPI routes
Services     (app/services/)           DownloadService, PathResolver,
                                       DirectoryBrowser, FfmpegPostProcessor
Plugins      (app/plugins/)            Downloader protocol; YtdlpDownloader
Infra                                  subprocess/ffmpeg/filesystem/env config
```

Nothing above the plugin layer knows it's talking to yt-dlp. A new backend =
implement `Downloader` in `app/plugins/`, register it in `app/registry.py`, done.

## Path rule

- `$jellyfin$/shows/x` → resolved under the Jellyfin library root (`/media` in-container).
- `podcasts/y` → resolved under the downloader's own storage root (`/downloads`).
- Tokens come from `MEDIA_TARGETS` env (`token=path,token=path`); add `$plex$` etc. by config only.

## Operate

```sh
sudo docker compose up -d --build     # build + start both
sudo docker compose logs -f downloader
sudo docker compose pull && sudo docker compose up -d   # update jellyfin
```

yt-dlp is unpinned on purpose; rebuild the downloader image to pick up its updates:
`sudo docker compose build --no-cache downloader && sudo docker compose up -d downloader`
