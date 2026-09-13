import { useEffect, useState } from "react";
import { api } from "../api";
import type { ProviderStatus } from "../types";
import malLogo from "../assets/mal-logo.png";
import anilistLogo from "../assets/anilist-logo.png";

interface ProviderStatusBarProps {
  downloaderId: string;
  // Bumped by the parent right after a real anime-search/anime-lookup call
  // resolves -- those are the only moments a provider's health can
  // actually change. This bar makes no request of its own to AniList or
  // MyAnimeList, only to our own backend's already-in-memory status (see
  // nyaa_tor_plugin.py's provider_status()) -- so re-fetching here is
  // free, unlike a real health-check ping would be.
  refreshSignal: number;
}

const LOGOS: Record<string, { src: string; label: string }> = {
  anilist: { src: anilistLogo, label: "AniList" },
  mal: { src: malLogo, label: "MyAnimeList" },
};

const DOT_COLOR: Record<ProviderStatus["status"], string> = {
  active: "var(--ok)",
  standby: "var(--warn)",
  down: "var(--err)",
};

const DOT_TITLE: Record<ProviderStatus["status"], string> = {
  active: "working, currently in use",
  standby: "working, but not currently in use",
  down: "not working (or not set up)",
};

export default function ProviderStatusBar({ downloaderId, refreshSignal }: ProviderStatusBarProps) {
  const [statuses, setStatuses] = useState<ProviderStatus[]>([]);

  useEffect(() => {
    if (!downloaderId) {
      setStatuses([]);
      return;
    }
    let cancelled = false;
    async function load() {
      try {
        const rs = await api<ProviderStatus[]>(`/downloaders/${downloaderId}/provider-status`);
        if (!cancelled) setStatuses(rs);
      } catch {
        // Purely cosmetic -- a failed status fetch just leaves the last
        // known dots showing, never surfaces as an error banner.
      }
    }
    load();
    // Light periodic re-poll of OUR OWN backend (no cost to anilist/mal) so
    // a status set by another tab/request eventually shows up here too,
    // not just right after this tab's own searches.
    const id = window.setInterval(load, 20000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [downloaderId, refreshSignal]);

  if (!statuses.length) return null;

  return (
    <div className="provider-status">
      {statuses.map((s) => {
        const logo = LOGOS[s.service];
        if (!logo) return null;
        return (
          <div className="provider-badge" key={s.service} title={`${logo.label}: ${DOT_TITLE[s.status]}`}>
            <img src={logo.src} alt={logo.label} />
            <span className="dot" style={{ background: DOT_COLOR[s.status] }} />
          </div>
        );
      })}
    </div>
  );
}
