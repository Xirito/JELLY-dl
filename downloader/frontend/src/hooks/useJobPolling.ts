import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { JobInfo } from "../types";

// Polls GET /downloads on an interval for the main queue list. A transient
// poll failure keeps the last known list rather than clearing it.
export function useJobPolling(intervalMs = 1500) {
  const [jobs, setJobs] = useState<JobInfo[]>([]);

  const poll = useCallback(async () => {
    try {
      const j = await api<JobInfo[]>("/downloads");
      setJobs(j);
      // Debug visibility for torrent jobs — seeders/leechers and
      // qBittorrent's raw state aren't shown anywhere else in the UI
      // beyond the queue's one-line summary. Open devtools while a
      // torrent job is running to see exactly what the backend is seeing
      // each poll (e.g. stuck at state=stalledDL with 0 seeders means the
      // torrent has no usable peers — not a bug in this app).
      for (const job of j) {
        if (job.progress?.state) {
          console.log(
            `[jelly-dl] job ${job.id} (${job.status}): state=${job.progress.state} ` +
              `seeds=${job.progress.seeders ?? "?"} leechs=${job.progress.leechers ?? "?"} ` +
              `${job.progress.percent ?? 0}% ${job.progress.speed_bps ?? 0} B/s`
          );
        }
      }
    } catch {
      // ignore — try again next tick
    }
  }, []);

  useEffect(() => {
    poll();
    const iv = setInterval(poll, intervalMs);
    return () => clearInterval(iv);
  }, [poll, intervalMs]);

  return { jobs, refresh: poll };
}

// Polls a single job until it leaves queued/running. Used only by bulk
// download to serialize episodes one at a time — independent of the main
// list poll above, which keeps running on its own schedule regardless.
export function waitForTerminal(id: string): Promise<JobInfo | null> {
  return new Promise((resolve) => {
    const iv = setInterval(async () => {
      try {
        const j = await api<JobInfo>(`/downloads/${id}`);
        if (j.status === "finished" || j.status === "error" || j.status === "cancelled") {
          clearInterval(iv);
          resolve(j);
        }
      } catch {
        clearInterval(iv);
        resolve(null);
      }
    }, 1200);
  });
}
