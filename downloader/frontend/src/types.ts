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
}

export interface DownloaderInfo {
  id: string;
  name: string;
  capabilities: DownloaderCapabilities;
}

export interface SearchResult {
  source: string;
  title: string;
  is_container: boolean;
  uploader?: string | null;
  duration_s?: number | null;
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
