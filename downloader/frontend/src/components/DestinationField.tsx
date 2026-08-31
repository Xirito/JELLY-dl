import { useEffect, useRef } from "react";
import type { MediaInterface } from "../types";
import { usePathSuggestions } from "../hooks/usePathSuggestions";

interface DestinationFieldProps {
  downloaderId: string;
  value: string;
  onChange: (v: string) => void;
  interfaces: MediaInterface[];
  showMetaToggle: boolean;
  embedMeta: boolean;
  onEmbedMetaChange: (v: boolean) => void;
  showDubToggle: boolean;
  dub: boolean;
  onDubChange: (v: boolean) => void;
}

export default function DestinationField({
  downloaderId,
  value,
  onChange,
  interfaces,
  showMetaToggle,
  embedMeta,
  onEmbedMetaChange,
  showDubToggle,
  dub,
  onDubChange,
}: DestinationFieldProps) {
  const { suggestions, request, clear } = usePathSuggestions(downloaderId);
  const wrapRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function onDocClick(e: MouseEvent) {
      if (!wrapRef.current?.contains(e.target as Node)) clear();
    }
    document.addEventListener("click", onDocClick);
    return () => document.removeEventListener("click", onDocClick);
  }, [clear]);

  const tokenHint = interfaces.length
    ? "tokens: " + interfaces.map((i) => i.placeholder).join(", ") + " → media libraries; plain paths → downloader storage"
    : "plain paths are relative to downloader storage";

  return (
    <>
      <label>
        Destination <span style={{ textTransform: "none" }}>— <span className="muted">{tokenHint}</span></span>
      </label>
      <div className="sugg" ref={wrapRef}>
        <input
          type="text"
          placeholder="$jellyfin$/shows/…  or  podcasts/…"
          autoComplete="off"
          value={value}
          onChange={(e) => {
            onChange(e.target.value);
            request(e.target.value);
          }}
        />
        {suggestions.length > 0 && (
          <div className="sugg-list">
            {suggestions.map((s) => (
              <div
                key={s}
                onClick={() => {
                  onChange(s + "/");
                  clear();
                  request(s + "/");
                }}
              >
                {s}
              </div>
            ))}
          </div>
        )}
      </div>
      {showMetaToggle && (
        <div style={{ marginTop: 6 }}>
          {/* .check-row (styles.css) sizes this to a ~44px tap target and
              enlarges the checkbox itself for comfortable mobile tapping. */}
          <label className="check-row">
            <input type="checkbox" checked={embedMeta} onChange={(e) => onEmbedMetaChange(e.target.checked)} />
            Embed metadata (title, artist, date, chapters)
          </label>
        </div>
      )}
      {showDubToggle && (
        <div style={{ marginTop: 6 }}>
          <label className="check-row">
            <input type="checkbox" checked={dub} onChange={(e) => onDubChange(e.target.checked)} />
            Dubbed (English audio) — default is subbed
          </label>
        </div>
      )}
    </>
  );
}
