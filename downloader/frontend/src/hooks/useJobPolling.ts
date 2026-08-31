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
