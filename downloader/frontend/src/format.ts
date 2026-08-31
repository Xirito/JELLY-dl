export function fmtSize(b: number): string {
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (b >= 1024 && i < 3) {
    b /= 1024;
    i++;
  }
  return b.toFixed(b >= 10 || i === 0 ? 0 : 1) + u[i];
}

export function fmtDur(s: number): string {
  s = Math.round(s);
  const m = Math.floor(s / 60);
  const h = Math.floor(m / 60);
  if (h) return `${h}h${String(m % 60).padStart(2, "0")}m`;
  if (m) return `${m}m${String(s % 60).padStart(2, "0")}s`;
  return `${s}s`;
}
