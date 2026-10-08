import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api";
import type {
  DownloaderInfo, FormatOption, MediaInterface, Mode, SeasonalSearchRequest, SeasonPlacement,
} from "./types";
import { useJobPolling } from "./hooks/useJobPolling";
import BackendSelect from "./components/BackendSelect";
import SearchPanel from "./components/SearchPanel";
import AnimeTorrentPanel from "./components/AnimeTorrentPanel";
import SeasonalCalendar from "./components/SeasonalCalendar";
import FormatPicker from "./components/FormatPicker";
import DestinationField from "./components/DestinationField";
import DownloadButton from "./components/DownloadButton";
import JobQueue from "./components/JobQueue";
import ArrowVideo from "./animation/arrow-scene.jsx";
import { APP_VERSION } from "./version";

const DEFAULT_MODES: Mode[] = ["best_video_audio", "best_audio", "best_video_only", "manual"];
// The only two backends that actually know how to resolve an anime title --
// nyaa_tor (AniList/MAL lookup + torrent search) and anicli (direct scrape).
// The seasonal-follow calendar only makes sense for these two (yt-dlp has
// no anime index at all, so a title search against it is never useful) --
// it only renders while one of them is selected; see the JSX below.
const ANIME_BACKEND_IDS = ["nyaa_tor", "anicli"];

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

  // "Search" from SeasonalCalendar's context menu -- forwarded to whichever
  // anime-capable panel is mounted below (only one of AnimeTorrentPanel/
  // SearchPanel renders at a time). Each panel decides for itself how to
  // use it (an id-based lookup vs. a plain title search) -- see their own
  // seasonalRequest effects. `token` is bumped on every pick, including a
  // repeat pick of the same show, so the effect re-fires every time.
  const [seasonalRequest, setSeasonalRequest] = useState<SeasonalSearchRequest | null>(null);
  const seasonalTokenRef = useRef(0);
  const handleSeasonalSearch = useCallback((show: { id: string; title: string }) => {
    seasonalTokenRef.current += 1;
    setSeasonalRequest({ token: seasonalTokenRef.current, id: show.id, title: show.title });
  }, []);
  // Cleared back to null by whichever panel's own seasonalRequest effect
  // actually handles it. Without this, a request handled while (say)
  // SearchPanel is mounted would still be sitting in state the next time
  // AnimeTorrentPanel mounts fresh (switching backends) -- a brand-new
  // mount's effect runs regardless of whether the token "changed" from
  // some prior render, so a stale request would silently re-fire a second
  // lookup the user never asked for just by switching backends afterward.
  const clearSeasonalRequest = useCallback(() => setSeasonalRequest(null), []);

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
  //
  // For anime the plain title is only a first guess -- a sequel's title
  // ("Oshi no Ko 2nd Season") is the wrong folder. GET /placement
  // (services/season_resolver.py) answers with "<Series>/Season NN"
  // instead; the title-only suggestion is written first so the field is
  // never empty if the user hits Download straight away, and the
  // resolver's answer replaces it when it lands.
  const [placement, setPlacement] = useState<SeasonPlacement | null>(null);
  const [placementPending, setPlacementPending] = useState(false);
  const [seasonNudged, setSeasonNudged] = useState(false);
  const placementRef = useRef<SeasonPlacement | null>(null);
  const placementKeyRef = useRef("");
  const placementSeqRef = useRef(0);

  // Writes a suggestion into the destination field -- but never over
  // something the user typed: only into an empty field, or one still
  // holding our own previous suggestion.
  const offerDest = useCallback((suggested: string) => {
    setDestination((cur) => {
      const trimmed = cur.trim();
      if (!trimmed || trimmed === lastAutoDestRef.current) {
        lastAutoDestRef.current = suggested;
        return suggested;
      }
      return cur;
    });
  }, []);

  const maybeAutoFillDest = useCallback(
    (showTitle: string | null, animeId?: string | null) => {
      if (!showTitle) return;
      const token = mediaTokens.includes("jellyfin") ? "jellyfin" : mediaTokens[0];
      if (!token) return;
      const key = `${token}|${animeId ?? ""}|${showTitle}`;
      if (key === placementKeyRef.current) {
        // Same show again (ani-cli calls this on every episode click):
        // re-offer what we already have, don't ask the backend again.
        if (placementRef.current) offerDest(placementRef.current.destination);
        return;
      }
      placementKeyRef.current = key;
      // Illegal path characters become a space, not an underscore — a title
      // like "Show: Subtitle" already has a space right after the colon, so
      // underscore produced an ugly "Show_ Subtitle"; collapsing whitespace
      // afterward turns that into a clean "Show Subtitle" instead of a
      // double space. (Same rule as the backend's folder_name().)
      const clean = showTitle
        .replace(/[\\/:*?"<>|]/g, " ")
        .replace(/\s+/g, " ")
        .trim();
      if (clean) offerDest(`$${token}$/shows/${clean}`);

      placementRef.current = null;
      setPlacement(null);
      setSeasonNudged(false);
      setPlacementPending(true);
      const seq = ++placementSeqRef.current;
      const params = new URLSearchParams({ title: showTitle, token });
      if (animeId) params.set("anime_id", animeId);
      api<SeasonPlacement>(`/placement?${params}`)
        .then((p) => {
          if (seq !== placementSeqRef.current) return; // superseded by a newer pick
          placementRef.current = p;
          setPlacement(p);
          offerDest(p.destination);
        })
        .catch(() => {
          // keep the title-only suggestion -- nothing better to offer
        })
        .finally(() => {
          if (seq === placementSeqRef.current) setPlacementPending(false);
        });
    },
    [mediaTokens, offerDest]
  );

  // The season buttons under the destination field. Only rendered while
  // the field still holds our own suggestion, so this never overwrites a
  // path the user typed.
  function shiftSeason(delta: number) {
    const p = placementRef.current;
    if (!p) return;
    const season = Math.max(0, p.season + delta);
    const destination = `${p.series_dir}/Season ${String(season).padStart(2, "0")}`;
    const next = { ...p, season, destination };
    placementRef.current = next;
    setPlacement(next);
    setSeasonNudged(true);
    lastAutoDestRef.current = destination;
    setDestination(destination);
  }
  const destIsOurs = !!destination.trim() && destination.trim() === lastAutoDestRef.current;

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
    // A destination we suggested for a show on the previous backend is
    // stale now -- drop it. (Left in place, it would also count as
    // "user-typed" once the ref below is cleared, and block every
    // auto-fill on the new backend until cleared by hand.) Anything the
    // user typed themselves stays.
    const prevAuto = lastAutoDestRef.current;
    setDestination((cur) => (prevAuto && cur.trim() === prevAuto ? "" : cur));
    lastAutoDestRef.current = "";
    placementSeqRef.current += 1; // drop any in-flight placement answer
    placementKeyRef.current = "";
    placementRef.current = null;
    setPlacement(null);
    setPlacementPending(false);
    setSeasonNudged(false);
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

  // Resubmits an errored job's original request as a new job (see
  // DownloadService.retry() on the backend) — the failed job itself is
  // untouched, so its "Resume" button stays put and can be clicked again if
  // the retry fails too. JobQueue awaits this to clear its own local
  // "resuming…" flag once the request settles either way.
  async function handleRetry(id: string) {
    try {
      await api(`/downloads/${id}/retry`, { method: "POST" });
      refreshJobs();
    } catch (e) {
      setMsg((e as Error).message, true);
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
      {ANIME_BACKEND_IDS.includes(downloaderId) && <SeasonalCalendar onSearch={handleSeasonalSearch} />}

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
            seasonalRequest={seasonalRequest}
            onSeasonalRequestHandled={clearSeasonalRequest}
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
            seasonalRequest={seasonalRequest}
            onSeasonalRequestHandled={clearSeasonalRequest}
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
          placement={destIsOurs ? placement : null}
          placementPending={placementPending && destIsOurs}
          seasonNudged={seasonNudged}
          onSeasonShift={shiftSeason}
        />
        <DownloadButton disabled={going} onClick={handleGo} msg={msg} msgIsError={msgIsError} />
      </div>

      <div className="card">
        <label>Queue</label>
        <div id="jobs">
          <JobQueue jobs={jobs} onCancel={handleCancel} onRetry={handleRetry} />
        </div>
      </div>

      <div className="muted" style={{ textAlign: "center", marginTop: 4 }}>
        {APP_VERSION}
      </div>
    </div>
  );
}
