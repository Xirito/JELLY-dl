import { useEffect, useState } from "react";
import { api } from "../api";
import type { AnimeDetails, AnimeMatch, SearchResult } from "../types";

interface AnimeTorrentPanelProps {
  downloaderId: string;
  src: string;
  onSrcChange: (v: string) => void;
  setPreviewThumbnail: (url: string | null) => void;
  maybeAutoFillDest: (title: string | null) => void;
  setMsg: (text: string, isError?: boolean) => void;
}

// Fansub/release-group tags, not torrent sites — nyaa.si stays the one
// indexer this searches (see torrent_providers.py on the backend for that
// meaning of "provider"). These just become a "[Tag] " prefix on the
// torrent search box below; the backend never sees or validates them, a
// torrent search is just a query string like any other.
const GROUP_TAGS = ["MTBB", "GJM", "Judas", "EMBER", "Erai-raws", "SubsPlease"];

export default function AnimeTorrentPanel({
  downloaderId,
  src,
  onSrcChange,
  setPreviewThumbnail,
  maybeAutoFillDest,
  setMsg,
}: AnimeTorrentPanelProps) {
  const [animeQuery, setAnimeQuery] = useState("");
  const [animeResults, setAnimeResults] = useState<AnimeMatch[]>([]);
  const [animeSearching, setAnimeSearching] = useState(false);
  const [animeSearched, setAnimeSearched] = useState(false);

  const [pickedAnime, setPickedAnime] = useState<AnimeDetails | null>(null);
  const [variantIdx, setVariantIdx] = useState(0);
  const [groupTag, setGroupTag] = useState<string | null>(null);
  const [torrentQuery, setTorrentQuery] = useState("");

  const [torrentResults, setTorrentResults] = useState<SearchResult[]>([]);
  const [torrentSearching, setTorrentSearching] = useState(false);
  const [torrentSearched, setTorrentSearched] = useState(false);
  const [pickedMagnet, setPickedMagnet] = useState<string | null>(null);

  // Stale state from a different backend can't be reused (anime ids and
  // search results are backend-specific).
  useEffect(() => {
    setAnimeQuery("");
    setAnimeResults([]);
    setAnimeSearching(false);
    setAnimeSearched(false);
    resetPicked();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [downloaderId]);

  function resetPicked() {
    setPickedAnime(null);
    setVariantIdx(0);
    setGroupTag(null);
    setTorrentQuery("");
    setTorrentResults([]);
    setTorrentSearching(false);
    setTorrentSearched(false);
    setPickedMagnet(null);
  }

  function composeQuery(tag: string | null, idx: number, anime: AnimeDetails) {
    const base = anime.title_variants[idx] ?? anime.title;
    setTorrentQuery(tag ? `[${tag}] ${base}` : base);
  }

  async function handleAnimeSearch() {
    const q = animeQuery.trim();
    if (!q) return;
    setAnimeSearching(true);
    setMsg("searching anidb…");
    resetPicked();
    try {
      const rs = await api<AnimeMatch[]>(`/downloaders/${downloaderId}/anime-search?q=${encodeURIComponent(q)}`);
      setAnimeResults(rs);
      setAnimeSearched(true);
      setMsg("");
    } catch (e) {
      setMsg((e as Error).message, true);
    }
    setAnimeSearching(false);
  }

  async function handlePickAnime(m: AnimeMatch) {
    setMsg("loading…");
    try {
      const details = await api<AnimeDetails>(`/downloaders/${downloaderId}/anime/${encodeURIComponent(m.id)}`);
      setPickedAnime(details);
      setVariantIdx(0);
      setGroupTag(null);
      setTorrentQuery(details.title);
      setTorrentResults([]);
      setTorrentSearched(false);
      setPickedMagnet(null);
      setPreviewThumbnail(details.cover ?? null);
      maybeAutoFillDest(details.title);
      setMsg("");
    } catch (e) {
      setMsg((e as Error).message, true);
    }
  }

  function handlePickVariant(idx: number) {
    if (!pickedAnime) return;
    setVariantIdx(idx);
    composeQuery(groupTag, idx, pickedAnime);
  }

  function handlePickTag(tag: string | null) {
    if (!pickedAnime) return;
    setGroupTag(tag);
    composeQuery(tag, variantIdx, pickedAnime);
  }

  async function handleTorrentSearch() {
    const q = torrentQuery.trim();
    if (!q) return;
    setTorrentSearching(true);
    setMsg("searching torrents…");
    try {
      const rs = await api<SearchResult[]>(`/downloaders/${downloaderId}/search?q=${encodeURIComponent(q)}`);
      setTorrentResults(rs);
      setTorrentSearched(true);
      setMsg("");
    } catch (e) {
      setMsg((e as Error).message, true);
    }
    setTorrentSearching(false);
  }

  function handlePickTorrent(r: SearchResult) {
    setPickedMagnet(r.source);
    onSrcChange(r.source);
    // The anime's own cover (set on pick, above) keeps showing — nyaa
    // results never carry a thumbnail of their own to replace it with.
  }

  return (
    <>
      <label>Anime</label>
      <div className="row">
        <input
          type="text"
          placeholder="anime name…"
          value={animeQuery}
          onChange={(e) => setAnimeQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") handleAnimeSearch();
          }}
        />
        <button style={{ flex: "0 0 auto" }} disabled={animeSearching} onClick={handleAnimeSearch}>
          Search
        </button>
      </div>

      {animeSearched && !pickedAnime && (
        <div id="animeResults">
          {animeResults.length > 0
            ? animeResults.map((m) => (
                <div className="item" key={m.id} onClick={() => handlePickAnime(m)}>
                  <span className="t">{m.title}</span>
                </div>
              ))
            : <div className="item muted">no results</div>}
        </div>
      )}

      {pickedAnime && (
        <>
          <div className="muted" style={{ marginTop: 10, display: "flex", justifyContent: "space-between", gap: 8 }}>
            <span>
              Confirmed: <strong style={{ color: "var(--text)" }}>{pickedAnime.title}</strong>
            </span>
            <span
              style={{ cursor: "pointer", textDecoration: "underline", flex: "0 0 auto" }}
              onClick={resetPicked}
            >
              change
            </span>
          </div>

          {pickedAnime.title_variants.length > 1 && (
            <>
              <label>Title to use</label>
              <div className="presets">
                {pickedAnime.title_variants.map((v, i) => (
                  <button key={v} className={variantIdx === i ? "on" : ""} onClick={() => handlePickVariant(i)}>
                    {v}
                  </button>
                ))}
              </div>
            </>
          )}

          <label>Provider</label>
          <div className="presets">
            <button className={groupTag === null ? "on" : ""} onClick={() => handlePickTag(null)}>
              No provider
            </button>
            {GROUP_TAGS.map((tag) => (
              <button key={tag} className={groupTag === tag ? "on" : ""} onClick={() => handlePickTag(tag)}>
                [{tag}]
              </button>
            ))}
          </div>

          <label>Torrent search</label>
          <div className="row">
            <input
              type="text"
              value={torrentQuery}
              onChange={(e) => setTorrentQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") handleTorrentSearch();
              }}
            />
            <button style={{ flex: "0 0 auto" }} disabled={torrentSearching} onClick={handleTorrentSearch}>
              Search
            </button>
          </div>

          {torrentSearched && (
            <div id="torrentResults">
              {torrentResults.length > 0
                ? torrentResults.map((r) => (
                    <div
                      className={"item" + (pickedMagnet === r.source ? " sel" : "")}
                      key={r.source}
                      onClick={() => handlePickTorrent(r)}
                    >
                      <span className="t">{r.title}</span>
                      <span className="m">{r.uploader || ""}</span>
                    </div>
                  ))
                : <div className="item muted">no results</div>}
            </div>
          )}
        </>
      )}

      <label>Source URL</label>
      <input type="text" placeholder="magnet:?…" value={src} onChange={(e) => onSrcChange(e.target.value)} />
    </>
  );
}
