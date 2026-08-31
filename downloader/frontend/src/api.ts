// Thin fetch wrapper — mirrors the original vanilla-JS api() helper's
// error handling exactly (unwrap {detail: "..."} from FastAPI's
// HTTPException body, fall back to the HTTP status text).
export async function api<T>(path: string, opt?: RequestInit): Promise<T> {
  const r = await fetch(path, opt);
  if (!r.ok) {
    let detail: string;
    try {
      detail = (await r.json()).detail;
    } catch {
      detail = r.statusText;
    }
    throw new Error(detail);
  }
  return r.json() as Promise<T>;
}
