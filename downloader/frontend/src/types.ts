// Mirrors the DTOs in downloader/app/models.py — the frontend only ever
// sees these shapes, never anything backend/plugin-specific (see
// docs/downloaderwebapparchitecture.md: "presentation knows only DTOs +
// capability flags").

export interface DownloaderCapabilities {
  supports_search: boolean;
  supports_manual_format_select: boolean;
  supports_metadata_embed: boolean;
  supports_dub_toggle: boolean;
  // Which preset mode buttons make sense for this backend (e.g. ani-cli has
  // no separate audio-only/video-only stream to offer). Falls back to all
  // four when the backend doesn't restrict it.
  available_modes?: Mode[];
  // Optional pre-search step (nyaa_tor): resolve a free-text anime name
  // against anidb.app before composing the actual torrent search — gates
  // rendering AnimeTorrentPanel instead of the plain SearchPanel.
  supports_anime_lookup?: boolean;
}

export interface DownloaderInfo {
  id: string;
  name: string;
  capabilities: DownloaderCapabilities;
}

export interface AnimeMatch {
  id: string;
  title: string;
}

export interface AnimeDetails {
  title: string;
  cover?: string | null;
  title_variants: string[];
}

// Mirrors SeasonalShow in downloader/app/models.py -- see
// services/seasonal.py. `id` is namespaced ("anilist:123"/"mal:456"),
// exactly like NyaaTorDownloader's own anime ids, so a followed show can
// go straight to /downloaders/nyaa_tor/anime/{id} when the user picks
// "search" (see components/SeasonalCalendar.tsx).
export interface SeasonalShow {
  id: string;
  title: string;
  cover?: string | null;
  // 0=Monday..6=Sunday, in the server's local time. null = no
  // currently-scheduled next episode (not airing yet, or fully aired).
  day_of_week?: number | null;
  followed: boolean;
}

// What "Search" in the seasonal calendar's context menu hands up to
// App.tsx, which forwards it to whichever anime-capable panel is
// currently mounted (AnimeTorrentPanel or SearchPanel). `token` is bumped
// on every pick so the same show can be searched twice in a row and still
// re-trigger the receiving panel's effect.
export interface SeasonalSearchRequest {
  token: number;
  id: string;
  title: string;
}

// Mirrors ProviderStatus in downloader/app/models.py -- a passive read of
// the backend's in-memory health state (nyaa_tor_plugin.py's
// provider_status()), never a live ping to AniList/MyAnimeList itself.
// "active" = working and currently used, "standby" = working but not the
// one in use, "down" = not working, not configured, or never tried yet.
export interface ProviderStatus {
  service: string;
  status: "active" | "standby" | "down";
}

export interface SearchResult {
  source: string;
  title: string;
  is_container: boolean;
  uploader?: string | null;
  duration_s?: number | null;
  thumbnail?: string | null;
}

export interface FormatOption {
  format_id: string;
  label: string;
  filesize_approx?: number | null;
}

export interface MediaInterface {
  token: string;
  placeholder: string;
}

export type Mode = "best_video_audio" | "best_audio" | "best_video_only" | "manual";

export type JobStatus = "queued" | "running" | "finished" | "error" | "cancelled";

export interface DownloadProgress {
  percent?: number | null;
  speed_bps?: number | null;
  eta_s?: number | null;
  downloaded_bytes?: number | null;
  total_bytes?: number | null;
  filename?: string | null;
  status?: string | null; // e.g. "cancelling"
  // Torrent-only debug fields (nyaa_tor backend) -- unset for yt-dlp/ani-cli.
  // seeders/leechers are peers connected right now (not swarm totals);
  // state is qBittorrent's own raw state string (e.g. "downloading",
  // "stalledDL", "metaDL", "checkingResumeData") -- surfaced so a stalled
  // job is diagnosable from the queue instead of just showing "running".
  seeders?: number | null;
  leechers?: number | null;
  state?: string | null;
}

export interface JobInfo {
  id: string;
  status: JobStatus;
  title?: string | null;
  source: string;
  destination: string;
  progress?: DownloadProgress | null;
  result?: { error?: string | null } | null;
}

export interface FormatSelector {
  mode: Mode;
  format_id: string | null;
}

export interface DownloadOptions {
  embed_metadata: boolean;
  dub: boolean;
}

export interface DownloadRequest {
  downloader_id: string;
  source: string;
  format_selector: FormatSelector;
  destination_path: string;
  options: DownloadOptions;
}
