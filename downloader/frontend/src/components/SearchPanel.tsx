import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { fmtDur } from "../format";
import { waitForTerminal } from "../hooks/useJobPolling";
import type { DownloadOptions, Mode, SearchResult } from "../types";

interface SearchPanelProps {
  downloaderId: string;
  searchSupported: boolean;
  src: string;
  onSrcChange: (v: string) => void;
  currentShowTitle: string | null;
  setCurrentShowTitle: (t: string | null) => void;
  setPreviewThumbnail: (url: string | null) => void;
  maybeAutoFillDest: (title: string | null) => void;
  destination: string;
  mode: Mode;
  embedMeta: boolean;
  dub: boolean;
  setMsg: (text: string, isError?: boolean) => void;
  refreshJobs: () => void;
}

// Some backends (ani-cli) return two-level results: a top-level pick is a
// container (e.g. an anime) that needs a follow-up search(parent=source) to
// list its leaves (episodes) before there's an actual source to use. Only a
// leaf list (opts.leaf, and nothing in it is itself a container) offers the
// bulk "download all" affordance — a container-level list of anime titles
// never does.
export default function SearchPanel({
  downloaderId,
  searchSupported,
  src,
  onSrcChange,
  currentShowTitle,
  setCurrentShowTitle,
  setPreviewThumbnail,
  maybeAutoFillDest,
  destination,
  mode,
  embedMeta,
  dub,
  setMsg,
  refreshJobs,
}: SearchPanelProps) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResult[]>([]);
  const [resultsAreLeaf, setResultsAreLeaf] = useState(false);
  const [searching, setSearching] = useState(false);
  const [hasSearched, setHasSearched] = useState(false);
  const [bulkProgress, setBulkProgress] = useState<{ i: number; total: number } | null>(null);
  const bulkActiveRef = useRef(false);

  // Stale search state from a different backend can't be reused (source
  // tokens and container/leaf shape are backend-specific).
  useEffect(() => {
    setResults([]);
    setResultsAreLeaf(false);
    setHasSearched(false);
    setBulkProgress(null);
    bulkActiveRef.current = false;
  }, [downloaderId]);

  const isLeaf = resultsAreLeaf && results.length > 0 && !results.some((r) => r.is_container);

  async function handleSearch() {
    const q = query.trim();
    if (!q) return;
    setSearching(true);
    setMsg("searching…");
    setCurrentShowTitle(null); // fresh top-level search — old show context is stale
    setPreviewThumbnail(null); // ...and so is whatever was previously picked
    try {
      const rs = await api<SearchResult[]>(`/downloaders/${downloaderId}/search?q=${encodeURIComponent(q)}`);
      setResults(rs);
      setResultsAreLeaf(false);
      setHasSearched(true);
      setMsg("");
    } catch (e) {
      setMsg((e as Error).message, true);
    }
    setSearching(false);
  }

  async function handleResultClick(r: SearchResult) {
    if (r.is_container) {
      // Remember the show's own title (not the episode's) before drilling
      // in, and auto-fill the destination right now — not only once a leaf
      // is later clicked — otherwise bulk-download can fire with an empty
      // destination if the user drills in and hits it straight away.
      setCurrentShowTitle(r.title);
      maybeAutoFillDest(r.title);
      setMsg("loading…");
      try {
        const rs2 = await api<SearchResult[]>(
          `/downloaders/${downloaderId}/search?q=&parent=${encodeURIComponent(r.source)}`
        );
        setResults(rs2);
        setResultsAreLeaf(true);
        setHasSearched(true);
        setMsg("");
        // Every episode is tagged with the same show cover (see
        // anicli_plugin.py — one fetch for the anime just picked, not one
        // per episode) — so it's ready the moment the episode list is,
        // before any specific episode is even clicked.
        setPreviewThumbnail(rs2[0]?.thumbnail ?? null);
      } catch (e) {
        setMsg((e as Error).message, true);
      }
    } else {
      onSrcChange(r.source);
      maybeAutoFillDest(currentShowTitle);
      setPreviewThumbnail(r.thumbnail ?? null);
    }
  }

  async function downloadAllEpisodes() {
    if (bulkActiveRef.current || !results.length) return;
    if (mode === "manual") {
      setMsg(
        "switch to a preset like “Best video+audio” to download all episodes — a manual format pick is episode-specific",
        true
      );
      return;
    }
    bulkActiveRef.current = true;
    const total = results.length;
    const options: DownloadOptions = { embed_metadata: embedMeta, dub };
    for (let i = 0; i < total; i++) {
      if (!bulkActiveRef.current) break;
      setBulkProgress({ i, total });
      const r = results[i];
      try {
        const job = await api<{ id: string }>("/downloads", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            downloader_id: downloaderId,
            source: r.source,
            format_selector: { mode, format_id: null },
            destination_path: destination.trim(),
            options,
          }),
        });
        refreshJobs();
        await waitForTerminal(job.id);
      } catch (e) {
        setMsg(`episode ${i + 1}/${total} (${r.title}) failed to queue: ${(e as Error).message}`, true);
      }
    }
    bulkActiveRef.current = false;
    setBulkProgress(null);
    setMsg("all episodes queued ✓");
    refreshJobs();
  }

  function stopBulk() {
    bulkActiveRef.current = false;
  }

  return (
    <>
      {searchSupported && (
        <div id="searchBox">
          <label>Search</label>
          <div className="row">
            <input
              type="text"
              placeholder="search query…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") handleSearch();
              }}
            />
            <button style={{ flex: "0 0 auto" }} disabled={searching} onClick={handleSearch}>
              Search
            </button>
          </div>
          {hasSearched && (
            <div id="results">
              {isLeaf && (
                <div className="item bulk" id="bulkAll" onClick={bulkProgress ? undefined : downloadAllEpisodes}>
                  {bulkProgress ? (
                    <>
                      <span className="t">queuing episode {bulkProgress.i + 1} of {bulkProgress.total}…</span>
                      <span className="m">
                        <button
                          style={{ padding: "3px 10px", fontSize: 12 }}
                          onClick={(e) => {
                            e.stopPropagation();
                            stopBulk();
                          }}
                        >
                          Stop
                        </button>
                      </span>
                    </>
                  ) : (
                    <span className="t">⇩ Download all {results.length} episodes</span>
                  )}
                </div>
              )}
              {results.length > 0
                ? results.map((r) => (
                    <div className="item" key={r.source} onClick={() => handleResultClick(r)}>
                      <span className="t">
                        {r.title}
                        {r.is_container ? " ›" : ""}
                      </span>
                      <span className="m">
                        {r.uploader || ""} {r.duration_s ? fmtDur(r.duration_s) : ""}
                      </span>
                    </div>
                  ))
                : <div className="item muted">no results</div>}
            </div>
          )}
        </div>
      )}
      <label>Source URL</label>
      <input type="text" placeholder="https://…" value={src} onChange={(e) => onSrcChange(e.target.value)} />
    </>
  );
}
