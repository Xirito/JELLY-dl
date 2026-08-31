import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api";
import type { DownloaderInfo, FormatOption, MediaInterface, Mode } from "./types";
import { useJobPolling } from "./hooks/useJobPolling";
import BackendSelect from "./components/BackendSelect";
import SearchPanel from "./components/SearchPanel";
import AnimeTorrentPanel from "./components/AnimeTorrentPanel";
import FormatPicker from "./components/FormatPicker";
import DestinationField from "./components/DestinationField";
import DownloadButton from "./components/DownloadButton";
import JobQueue from "./components/JobQueue";
import ArrowVideo from "./animation/arrow-scene.jsx";
import { APP_VERSION } from "./version";

const DEFAULT_MODES: Mode[] = ["best_video_audio", "best_audio", "best_video_only", "manual"];

export default function App() {
  const [downloaders, setDownloaders] = useState<DownloaderInfo[]>([]);
  const [downloaderId, setDownloaderId] = useState("");
  const [interfaces, setInterfaces] = useState<MediaInterface[]>([]);

  const [src, setSrc] = useState("");
  const [currentShowTitle, setCurrentShowTitle] = useState<string | null>(null);
  // Set once something is actually picked — a video (yt-dlp) or a show
  // (ani-cli, as soon as its episode list loads) — so there's a visual
  // "yes, this is what I'm about to download" right before committing.
  // Never populated from the results list itself; see SearchPanel.
  const [previewThumbnail, setPreviewThumbnail] = useState<string | null>(null);
  const [destination, setDestination] = useState("");
  const lastAutoDestRef = useRef("");

  const [mode, setMode] = useState<Mode>("best_video_audio");
  const [manualId, setManualId] = useState<string | null>(null);
  const [formats, setFormats] = useState<FormatOption[]>([]);
  const [formatsHint, setFormatsHint] = useState("");

  const [embedMeta, setEmbedMeta] = useState(false);
  const [dub, setDub] = useState(false);

  const [msg, setMsgRaw] = useState("");
  const [msgIsError, setMsgIsError] = useState(false);
  const [going, setGoing] = useState(false);

  const { jobs, refresh: refreshJobs } = useJobPolling();

  const setMsg = useCallback((text: string, isError = false) => {
    setMsgRaw(text);
    setMsgIsError(isError);
  }, []);

  const mediaTokens = useMemo(() => interfaces.map((i) => i.token), [interfaces]);

  const caps = useMemo(
    () => downloaders.find((d) => d.id === downloaderId)?.capabilities ?? null,
    [downloaders, downloaderId]
  );
  const availModes = useMemo<Mode[]>(() => caps?.available_modes ?? DEFAULT_MODES, [caps]);

  // Suggests "$jellyfin$/shows/<Show Title>" (or the first configured media
  // token if "jellyfin" isn't one) once a show is known. Never clobbers a
  // destination the user typed or edited themselves — only fills an empty
  // field, or one still holding our own previous suggestion. Called from
  // both the container-drill and the leaf-click handlers in SearchPanel —
  // one shared function, so neither path can "forget" to call it.
  const maybeAutoFillDest = useCallback(
    (showTitle: string | null) => {
      if (!showTitle) return;
      const token = mediaTokens.includes("jellyfin") ? "jellyfin" : mediaTokens[0];
      if (!token) return;
      const clean = showTitle.replace(/[\\/:*?"<>|]/g, "_").trim();
      if (!clean) return;
      const suggested = `$${token}$/shows/${clean}`;
      setDestination((cur) => {
        const trimmed = cur.trim();
        if (!trimmed || trimmed === lastAutoDestRef.current) {
          lastAutoDestRef.current = suggested;
          return suggested;
        }
        return cur;
      });
    },
    [mediaTokens]
  );

  // Initial load: backends + media-target interfaces.
  useEffect(() => {
    (async () => {
      try {
        const dls = await api<DownloaderInfo[]>("/downloaders");
        setDownloaders(dls);
        if (dls.length) setDownloaderId(dls[0].id);
        const ifs = await api<MediaInterface[]>("/interfaces");
        setInterfaces(ifs);
      } catch (e) {
        setMsg((e as Error).message, true);
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Search results/source and any picked manual format are backend-specific
  // tokens — stale ones from the previous backend can't be reused. (Results
  // list itself is reset locally inside SearchPanel, keyed on downloaderId.)
  useEffect(() => {
    if (!downloaderId) return;
    setFormats([]);
    setFormatsHint("");
    setManualId(null);
    setSrc("");
    setCurrentShowTitle(null);
    setPreviewThumbnail(null);
    lastAutoDestRef.current = "";
    setMsg("");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [downloaderId]);

  // Which preset mode buttons make sense varies by backend — fall back to
  // the first one it does support if the current pick isn't offered.
  useEffect(() => {
    if (!availModes.includes(mode)) {
      setMode(availModes[0] ?? "best_video_audio");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [availModes]);

  const loadFormats = useCallback(async () => {
    if (!src.trim()) {
      setMsg("enter a source URL first", true);
      return;
    }
    setFormatsHint("loading formats…");
    try {
      const fs = await api<FormatOption[]>(
        `/downloaders/${downloaderId}/formats?source=${encodeURIComponent(src)}`
      );
      setFormats(fs);
      setFormatsHint(fs.length + " formats — click one");
    } catch (e) {
      setFormats([]);
      setFormatsHint("");
      setMsg((e as Error).message, true);
    }
  }, [downloaderId, src, setMsg]);

  function handleModeChange(m: Mode) {
    setMode(m);
    if (m === "manual") {
      loadFormats();
    } else {
      setFormats([]);
      setFormatsHint("");
    }
  }

  async function handleGo() {
    if (!src.trim()) {
      setMsg("enter a source URL", true);
      return;
    }
    if (mode === "manual" && !manualId) {
      setMsg("pick a format first", true);
      return;
    }
    setGoing(true);
    setMsg("queuing…");
    try {
      await api("/downloads", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          downloader_id: downloaderId,
          source: src,
          format_selector: { mode, format_id: mode === "manual" ? manualId : null },
          destination_path: destination.trim(),
          options: { embed_metadata: embedMeta, dub },
        }),
      });
      setMsg("queued ✓");
      refreshJobs();
    } catch (e) {
      setMsg((e as Error).message, true);
    }
    setGoing(false);
  }

  async function handleCancel(id: string) {
    try {
      await api(`/downloads/${id}/cancel`, { method: "POST" });
      refreshJobs();
    } catch {
      // leave the optimistic "cancelling…" state — next poll will reconcile
    }
  }

  return (
    <div className="wrap">
      <h1>
        <img src="/logo.png" alt="" className="h1-icon" />
        Jelly Downloader
      </h1>
      <div className="bigLogoReplacement">
        {previewThumbnail ? (
          <img
            src={previewThumbnail}
            alt=""
            onError={() => {
              // Broken/blocked image (dead CDN link, ad-blocker, etc.) —
              // fall back to the animation rather than show a broken box.
              setPreviewThumbnail(null);
            }}
          />
        ) : (
          <ArrowVideo showControls={false} />
        )}
      </div>
      <div className="card">
        <BackendSelect downloaders={downloaders} value={downloaderId} onChange={setDownloaderId} />
        {caps?.supports_anime_lookup ? (
          <AnimeTorrentPanel
            downloaderId={downloaderId}
            src={src}
            onSrcChange={setSrc}
            setPreviewThumbnail={setPreviewThumbnail}
            maybeAutoFillDest={maybeAutoFillDest}
            setMsg={setMsg}
          />
        ) : (
          <SearchPanel
            downloaderId={downloaderId}
            searchSupported={!!caps?.supports_search}
            src={src}
            onSrcChange={setSrc}
            currentShowTitle={currentShowTitle}
            setCurrentShowTitle={setCurrentShowTitle}
            setPreviewThumbnail={setPreviewThumbnail}
            maybeAutoFillDest={maybeAutoFillDest}
            destination={destination}
            mode={mode}
            embedMeta={embedMeta}
            dub={dub}
            setMsg={setMsg}
            refreshJobs={refreshJobs}
          />
        )}
      </div>

      <FormatPicker
        availModes={availModes}
        showManual={!!caps?.supports_manual_format_select}
        mode={mode}
        onModeChange={handleModeChange}
        formats={formats}
        manualId={manualId}
        onManualPick={setManualId}
        formatsHint={formatsHint}
      />

      <div className="card">
        <DestinationField
          downloaderId={downloaderId}
          value={destination}
          onChange={setDestination}
          interfaces={interfaces}
          showMetaToggle={!!caps?.supports_metadata_embed}
          embedMeta={embedMeta}
          onEmbedMetaChange={setEmbedMeta}
          showDubToggle={!!caps?.supports_dub_toggle}
          dub={dub}
          onDubChange={setDub}
        />
        <DownloadButton disabled={going} onClick={handleGo} msg={msg} msgIsError={msgIsError} />
      </div>

      <div className="card">
        <label>Queue</label>
        <div id="jobs">
          <JobQueue jobs={jobs} onCancel={handleCancel} />
        </div>
      </div>

      <div className="muted" style={{ textAlign: "center", marginTop: 4 }}>
        {APP_VERSION}
      </div>
    </div>
  );
}
