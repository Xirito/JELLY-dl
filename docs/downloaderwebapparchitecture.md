# Downloader Webapp — Architecture & Layout

## Goal
A web app (browser now, native via Capacitor later) that downloads media through a pluggable **downloader backend** (yt-dlp today, ani-cli or others later) and saves it into a path chosen by the user — either relative to a recognized media server's library, or relative to the downloader's own storage. Format selection (manual or preset) and merging are handled through ffmpeg where needed.

The core design constraint: **nothing above the plugin layer should know it's talking to yt-dlp specifically.** Swapping in ani-cli should only mean writing a new plugin, not touching the API, the frontend, or the path/format logic.

## Layers

```
┌─────────────────────────────────────────────┐
│ Presentation (web now / Capacitor app later) │  knows only DTOs + capability flags
├─────────────────────────────────────────────┤
│ API (FastAPI routes)                         │  thin, delegates to services
├─────────────────────────────────────────────┤
│ Domain / Services                            │  orchestration, downloader-agnostic
│  - DownloadService                           │
│  - PathResolver                              │
│  - FormatSelector                            │
│  - DirectoryBrowser                          │
├─────────────────────────────────────────────┤
│ Downloader Plugins (implement Downloader)    │  yt-dlp today, ani-cli later
├─────────────────────────────────────────────┤
│ Infrastructure                               │  subprocess exec, ffmpeg, filesystem,
│                                               │  media-server target config
└─────────────────────────────────────────────┘
```

Each layer only depends on the one below it, never sideways or upward. The API layer never imports anything yt-dlp-specific directly — it only knows the `Downloader` interface.

## Core abstraction: the `Downloader` interface

```python
class Downloader(Protocol):
    id: str                      # "ytdlp", "anicli", ...
    capabilities: DownloaderCapabilities

    def search(self, query: str) -> list[SearchResult]: ...
    def list_formats(self, source: str) -> list[FormatOption]: ...
    def download(
        self,
        source: str,
        format_selector: FormatSelector,
        destination: Path,
        on_progress: Callable[[DownloadProgress], None],
    ) -> DownloadResult: ...
```

```python
class DownloaderCapabilities(BaseModel):
    supports_search: bool
    supports_format_listing: bool
    supports_manual_format_select: bool
```

**Why capabilities, not just duck-typing:** yt-dlp doesn't really "search" in the ani-cli sense (browsing a catalog by title) — it takes a URL, though it can accept a `ytsearch:` query as a pseudo-URL. ani-cli genuinely has a search/browse step before picking a source. Rather than forcing every plugin to implement every method, the frontend reads `capabilities` and shows/hides UI accordingly (e.g. no search box for a plugin that doesn't support it).

### `YtdlpDownloader` (first implementation)
- Wraps the `yt-dlp` Python module (not the CLI, to get structured output/progress hooks).
- `list_formats()` maps yt-dlp's raw format dicts into a normalized `FormatOption` DTO — id, resolution, codec, approx filesize, `has_audio`, `has_video`, bitrate — so the frontend never sees yt-dlp-specific fields.
- `download()` resolves the `FormatSelector` (see below) into yt-dlp's format-selection string internally. This translation lives entirely inside this plugin — no other layer knows yt-dlp's format-string syntax exists.
- Post-processing (muxing separate video+audio streams, remuxing containers) is delegated to a shared `FfmpegPostProcessor` — reusable by future plugins too, so ffmpeg logic isn't duplicated per-downloader.
- Known gotcha (carried over from the current setup): don't pin the `yt-dlp` version in requirements — sites change frequently and pinning causes format errors. Keep the binary/package updated instead.

### Future `AniCliDownloader`
- Implements `search()` for real (ani-cli's built-in search).
- `list_formats()` may return a smaller, fixed set (ani-cli typically exposes fewer quality options than yt-dlp).
- Everything else — path resolution, directory autocomplete, the download queue/progress UI — is unchanged, because those layers never depended on yt-dlp specifics in the first place.

## Format selection

```python
class FormatSelector(BaseModel):
    mode: Literal["manual", "best_audio", "best_video_audio", "best_video_only"]
    format_id: str | None = None   # required when mode == "manual"
```

- **Manual**: user picks a specific `format_id` from `list_formats()` results shown in the UI.
- **Presets** (`best_audio`, `best_video_audio`, `best_video_only`): resolved inside the plugin — for yt-dlp this maps to its own `bestaudio`, `bestvideo+bestaudio`, `bestvideo` selectors; a future plugin resolves the same preset names using whatever its own backend supports. The preset names are the shared contract, not the underlying implementation.
- ffmpeg is invoked (via `FfmpegPostProcessor`) whenever a merge or remux is needed — e.g. separate best-video + best-audio streams get muxed into one file.

## Path resolution

User-facing rule:
- A path starting with a recognized `$interface$` token (e.g. `$jellyfin$`, `$plex$`) is resolved **relative to that media server's configured library root.**
- Any other path is resolved **relative to the active downloader's own storage root.**

```python
class MediaServerTarget(BaseModel):
    token: str          # "jellyfin", "plex"
    base_path: Path      # e.g. /mnt/media

MEDIA_SERVER_TARGETS: list[MediaServerTarget]  # configured, not hardcoded — easy to add more
```

```python
class PathResolver:
    def resolve(self, raw_path: str, downloader: Downloader) -> Path:
        token = extract_interface_token(raw_path)   # parses "$jellyfin$/..." 
        if token and token in registered_targets:
            return registered_targets[token].base_path / strip_token(raw_path)
        return downloader.default_download_root / raw_path
```

This keeps "where do interfaces live" as pure configuration (add a new `MediaServerTarget` entry, no code change) and keeps the resolution logic itself downloader-agnostic — it only calls `downloader.default_download_root` as a fallback, nothing more.

## Directory browsing / autofill

- `DirectoryBrowser.list_existing(base: Path, prefix: str) -> list[str]` — lists subdirectories under the resolved base and returns those matching the typed prefix.
- Example: user types `/$jellyfin$/parkou` → resolver identifies `$jellyfin$` → lists subdirectories of the Jellyfin library root → matches `parkour civilization` → frontend shows it as an autofill suggestion.
- Same component is reused regardless of whether the base ended up being a media-server root or a downloader's own root — it just operates on whatever `Path` `PathResolver` handed back.

## API surface (thin, DTO-only)

- `GET /downloaders` — list registered plugins + their `capabilities`
- `GET /downloaders/{id}/search?q=...` — only valid if `supports_search`
- `GET /downloaders/{id}/formats?source=...`
- `POST /downloads` — body: `{downloader_id, source, format_selector, destination_path}`
- `GET /downloads/{id}` — progress/status
- `GET /interfaces` — list configured `MediaServerTarget`s (for the `$token$` autocomplete list)
- `GET /paths/suggest?path=...` — directory autofill

## Extensibility checklist (adding a new downloader)
1. Implement `Downloader` (search optional, format listing optional, download required).
2. Declare `capabilities` honestly — the frontend adapts automatically.
3. Register it in the `DownloaderRegistry` (a simple id → instance map, likely populated from config/env).
4. No changes needed to: path resolution, directory autocomplete, the API routes, or the frontend's core flow.

## Open items / future work
- Webapp → native wrap via Capacitor (per existing plan) — the API being pure JSON/DTO already makes this straightforward, no rework needed there.
- Decide whether download queue/progress uses polling or websockets once the frontend is built out.
- `MediaServerTarget` config source — likely env vars or a small settings file, TBD when the app is actually wired up on TrueNAS.
