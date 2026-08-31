import { useState } from "react";
import type { JobInfo } from "../types";
import { fmtDur, fmtSize } from "../format";

interface JobQueueProps {
  jobs: JobInfo[];
  onCancel: (id: string) => void;
}

export default function JobQueue({ jobs, onCancel }: JobQueueProps) {
  // Optimistic local "cancelling" flags — set the instant Cancel is clicked,
  // ahead of the next poll picking up the server's own progress.status.
  const [cancellingLocal, setCancellingLocal] = useState<Set<string>>(new Set());

  function handleCancel(id: string) {
    setCancellingLocal((prev) => new Set(prev).add(id));
    onCancel(id);
  }

  if (!jobs.length) {
    return <div className="muted">no downloads yet</div>;
  }

  return (
    <>
      {jobs.map((j) => {
        const p = j.progress || {};
        const pct = p.percent != null ? p.percent : j.status === "finished" ? 100 : 0;
        const speed = p.speed_bps ? fmtSize(p.speed_bps) + "/s" : "";
        const eta = p.eta_s ? "eta " + fmtDur(p.eta_s) : "";
        const size = p.downloaded_bytes
          ? fmtSize(p.downloaded_bytes) + (p.total_bytes ? " / " + fmtSize(p.total_bytes) : "")
          : "";
        // Torrent-only debug info -- unset for yt-dlp/ani-cli jobs. state is
        // qBittorrent's own raw status word (e.g. "stalledDL", "metaDL"),
        // shown so a job stuck at 0% is diagnosable instead of just saying
        // "running".
        const peers =
          p.seeders != null || p.leechers != null ? `${p.seeders ?? 0}↑ ${p.leechers ?? 0}↓` : "";
        const torrentState = p.state || "";
        const cancellable = j.status === "queued" || j.status === "running";
        const cancelling = p.status === "cancelling" || cancellingLocal.has(j.id);
        const statusLabel = cancelling ? "cancelling…" : j.status;
        return (
          <div className="job" key={j.id}>
            <div className="top">
              <span className="name">{j.title || p.filename || j.source}</span>
              <span style={{ display: "flex", gap: 8, alignItems: "center", flex: "0 0 auto" }}>
                <span className={`st-${j.status}`}>{statusLabel}</span>
                {cancellable && (
                  <button
                    className="btn-cancel"
                    disabled={cancelling}
                    onClick={() => handleCancel(j.id)}
                  >
                    {cancelling ? "cancelling…" : "Cancel"}
                  </button>
                )}
              </span>
            </div>
            <div className="muted" style={{ fontSize: 12, marginTop: 2 }}>
              → {j.destination}
              {size ? " · " + size : ""}
              {speed ? " · " + speed : ""}
              {eta ? " · " + eta : ""}
              {torrentState ? " · " + torrentState : ""}
              {peers ? " · " + peers : ""}
            </div>
            <div className="bar">
              <i style={{ width: `${pct}%`, background: j.status === "error" ? "var(--err)" : undefined }} />
            </div>
            {j.result?.error && j.status !== "cancelled" && <div className="err">{j.result.error}</div>}
          </div>
        );
      })}
    </>
  );
}
