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

## Instant library refresh

After a download lands under a `$token$` path, the downloader pings that media
server to rescan (best-effort). Config per token, via env:
`NOTIFY_JELLYFIN_URL=http://jellyfin:8096` (set in compose) and
`NOTIFY_JELLYFIN_APIKEY` — supplied through `JELLYFIN_API_KEY` in a `.env`
file next to `docker-compose.yml` (gitignored). Create the key in Jellyfin:
Dashboard → API Keys → “+”.

## Operate

```sh
sudo docker compose up -d --build     # build + start both
sudo docker compose logs -f downloader
sudo docker compose pull && sudo docker compose up -d   # update jellyfin
```

yt-dlp is unpinned on purpose; rebuild the downloader image to pick up its updates:
`sudo docker compose build --no-cache downloader && sudo docker compose up -d downloader`
