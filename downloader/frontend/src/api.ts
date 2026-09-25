// Thin fetch wrapper — mirrors the original vanilla-JS api() helper's
// error handling exactly (unwrap {detail: "..."} from FastAPI's
// HTTPException body, fall back to the HTTP status text).
//
// Cloudflare Access session expiry: once the CF_Authorization cookie
// expires, Access answers every request with a 302 to
// <team>.cloudflareaccess.com. A plain fetch() follows that cross-origin
// redirect, the login page has no CORS headers, and the browser kills it
// ("net::ERR_FAILED 200 (OK)") — the page just sits there broken because
// nothing ever navigates. With redirect: "manual" the redirect surfaces as
// an `opaqueredirect` response instead, and we answer it with a top-level
// reload: a real navigation, which Access CAN handle (login page, or a
// silent re-issue if the IdP session is still alive, then back here).
//
// Loop guard: if we already reloaded for this reason in the last 30s and
// still get redirected (e.g. Access misconfigured), stop reloading and
// surface an error instead of hammering the login page.

const REAUTH_KEY = "jelly-dl:reauth-at";
const REAUTH_COOLDOWN_MS = 30_000;
let reloading = false;

export class SessionExpiredError extends Error {
  constructor() {
    super("Session expired — reloading to sign in again…");
    this.name = "SessionExpiredError";
  }
}

function isAuthRedirect(r: Response): boolean {
  // opaqueredirect = any 3xx under redirect: "manual" (Access's login
  // redirect). The backend itself never issues redirects or 401/403, so
  // these can only come from Access sitting in front of it.
  return (
    r.type === "opaqueredirect" ||
    (r.status >= 300 && r.status < 400) ||
    r.status === 401 ||
    r.status === 403
  );
}

function readReauthAt(): number {
  try {
    return Number(sessionStorage.getItem(REAUTH_KEY)) || 0;
  } catch {
    return 0;
  }
}

function writeReauthAt(v: number | null) {
  try {
    if (v === null) sessionStorage.removeItem(REAUTH_KEY);
    else sessionStorage.setItem(REAUTH_KEY, String(v));
  } catch {
    // storage unavailable — the in-memory `reloading` flag still stops
    // parallel polls from each triggering their own reload
  }
}

function handleExpiredSession(): never {
  if (!reloading && Date.now() - readReauthAt() > REAUTH_COOLDOWN_MS) {
    reloading = true;
    writeReauthAt(Date.now());
    location.reload();
  }
  if (!reloading) {
    throw new Error("Not signed in — reload the page to sign in again.");
  }
  throw new SessionExpiredError();
}

export async function api<T>(path: string, opt?: RequestInit): Promise<T> {
  if (reloading) throw new SessionExpiredError();
  const r = await fetch(path, {
    credentials: "same-origin",
    ...opt,
    redirect: "manual",
  });
  if (isAuthRedirect(r)) handleExpiredSession();
  // Successful authenticated round trip: clear the loop guard so the next
  // expiry (tomorrow) gets its automatic reload again.
  if (r.ok && readReauthAt()) writeReauthAt(null);
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
