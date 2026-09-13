import { useEffect, useState } from "react";
import { api } from "../api";
import type { SeasonalShow } from "../types";

interface SeasonalCalendarProps {
  // Fired when the user picks "Search" on a followed show's context menu.
  // App.tsx forwards this to whichever anime-capable panel is currently
  // mounted -- this component never talks to a specific downloader itself,
  // it only knows the seasonal-follow list (shared by both).
  onSearch: (show: { id: string; title: string }) => void;
}

const DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

// JS Date.getDay() is 0=Sunday..6=Saturday; the backend's day_of_week
// (services/seasonal.py, mirroring Python's datetime.weekday()) is
// 0=Monday..6=Sunday. This converts the local "today" into that scheme so
// it lines up with the shows grouped by day_of_week below.
function todayIndex(): number {
  return (new Date().getDay() + 6) % 7;
}

function titleCase(s: string): string {
  return s.length ? s[0] + s.slice(1).toLowerCase() : s;
}

export default function SeasonalCalendar({ onSearch }: SeasonalCalendarProps) {
  const [view, setView] = useState<"calendar" | "browse">("calendar");

  const [followed, setFollowed] = useState<SeasonalShow[]>([]);
  const [followedErr, setFollowedErr] = useState("");

  const [season, setSeason] = useState<string | null>(null);
  const [year, setYear] = useState<number | null>(null);
  const [browsed, setBrowsed] = useState<SeasonalShow[]>([]);
  const [browsing, setBrowsing] = useState(false);
  const [browseErr, setBrowseErr] = useState("");

  const [menuFor, setMenuFor] = useState<SeasonalShow | null>(null);

  async function loadFollowed() {
    try {
      const rs = await api<SeasonalShow[]>("/seasonal/followed");
      setFollowed(rs);
      setFollowedErr("");
    } catch (e) {
      setFollowedErr((e as Error).message);
    }
  }

  // Followed list loads immediately (it's the default view); the current
  // season is fetched alongside it so "Follow more" has somewhere to
  // start browsing the moment it's opened, with no extra round-trip.
  useEffect(() => {
    loadFollowed();
    (async () => {
      try {
        const cur = await api<{ season: string; year: number }>("/seasonal/current");
        setSeason(cur.season);
        setYear(cur.year);
      } catch {
        // "Follow more" just won't have a season to browse yet -- the
        // calendar of already-followed shows still works fine without it.
      }
    })();
  }, []);

  async function loadBrowse(s: string, y: number) {
    setBrowsing(true);
    setBrowseErr("");
    try {
      const rs = await api<SeasonalShow[]>(`/seasonal/browse?season=${encodeURIComponent(s)}&year=${y}`);
      setBrowsed(rs);
    } catch (e) {
      setBrowseErr((e as Error).message);
    }
    setBrowsing(false);
  }

  useEffect(() => {
    if (view === "browse" && season && year !== null) loadBrowse(season, year);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view, season, year]);

  async function shiftSeason(delta: number) {
    if (!season || year === null) return;
    try {
      const adj = await api<{ season: string; year: number }>(
        `/seasonal/adjacent?season=${season}&year=${year}&delta=${delta}`
      );
      setSeason(adj.season);
      setYear(adj.year);
    } catch (e) {
      setBrowseErr((e as Error).message);
    }
  }

  async function follow(show: SeasonalShow) {
    if (!season || year === null) return;
    try {
      await api("/seasonal/follow", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          id: show.id, title: show.title, cover: show.cover,
          day_of_week: show.day_of_week ?? null, season, year,
        }),
      });
      setBrowsed((cur) => cur.map((s) => (s.id === show.id ? { ...s, followed: true } : s)));
      loadFollowed();
    } catch (e) {
      setBrowseErr((e as Error).message);
    }
  }

  async function unfollow(show: SeasonalShow) {
    setMenuFor(null);
    try {
      await api(`/seasonal/follow/${encodeURIComponent(show.id)}`, { method: "DELETE" });
      setBrowsed((cur) => cur.map((s) => (s.id === show.id ? { ...s, followed: false } : s)));
      loadFollowed();
    } catch (e) {
      setFollowedErr((e as Error).message);
    }
  }

  function pickSearch(show: SeasonalShow) {
    setMenuFor(null);
    onSearch({ id: show.id, title: show.title });
  }

  const today = todayIndex();
  const byDay: SeasonalShow[][] = Array.from({ length: 7 }, () => []);
  const unscheduled: SeasonalShow[] = [];
  for (const s of followed) {
    if (s.day_of_week === null || s.day_of_week === undefined) unscheduled.push(s);
    else byDay[s.day_of_week].push(s);
  }

  return (
    <div className="card seasonal">
      <div className="row seasonal-tabs">
        <button className={view === "calendar" ? "on" : ""} onClick={() => setView("calendar")}>
          This week
        </button>
        <button className={view === "browse" ? "on" : ""} onClick={() => setView("browse")}>
          Follow more
        </button>
      </div>

      {view === "calendar" && (
        <>
          {followedErr && <div className="err">{followedErr}</div>}
          {followed.length === 0 && !followedErr && (
            <div className="muted" style={{ marginTop: 8 }}>
              Nothing followed yet — switch to "Follow more" to browse this season.
            </div>
          )}
          {followed.length > 0 && (
            <div className="seasonal-grid">
              {DAY_LABELS.map((label, i) => (
                <div className={"seasonal-day" + (i === today ? " today" : "")} key={label}>
                  <div className="seasonal-day-label">{label}</div>
                  {byDay[i].map((s) => (
                    <div className="seasonal-cover" key={s.id} onClick={() => setMenuFor(s)}>
                      {s.cover ? <img src={s.cover} alt="" /> : <div className="seasonal-cover-blank" />}
                      <span className="seasonal-title">{s.title}</span>
                    </div>
                  ))}
                </div>
              ))}
            </div>
          )}
          {unscheduled.length > 0 && (
            <>
              <div className="seasonal-day-label" style={{ marginTop: 12 }}>
                Not yet scheduled
              </div>
              <div className="seasonal-unscheduled-row">
                {unscheduled.map((s) => (
                  <div className="seasonal-cover" key={s.id} onClick={() => setMenuFor(s)}>
                    {s.cover ? <img src={s.cover} alt="" /> : <div className="seasonal-cover-blank" />}
                    <span className="seasonal-title">{s.title}</span>
                  </div>
                ))}
              </div>
            </>
          )}
        </>
      )}

      {view === "browse" && (
        <>
          <div className="row seasonal-season-nav">
            <button style={{ flex: "0 0 auto" }} onClick={() => shiftSeason(-1)}>
              ‹
            </button>
            <span className="muted" style={{ flex: 1, textAlign: "center" }}>
              {season && year !== null ? `${titleCase(season)} ${year}` : "…"}
            </span>
            <button style={{ flex: "0 0 auto" }} onClick={() => shiftSeason(1)}>
              ›
            </button>
          </div>
          {browseErr && <div className="err">{browseErr}</div>}
          {browsing && <div className="muted">loading…</div>}
          {!browsing && (
            <div className="seasonal-browse-grid">
              {browsed.map((s) => (
                <div className="seasonal-browse-item" key={s.id}>
                  {s.cover ? <img src={s.cover} alt="" /> : <div className="seasonal-cover-blank" />}
                  <span className="seasonal-title">{s.title}</span>
                  <button className={s.followed ? "on" : ""} onClick={() => (s.followed ? unfollow(s) : follow(s))}>
                    {s.followed ? "Following ✓" : "+ Follow"}
                  </button>
                </div>
              ))}
            </div>
          )}
        </>
      )}

      {menuFor && (
        <div className="seasonal-menu-backdrop" onClick={() => setMenuFor(null)}>
          <div className="seasonal-menu" onClick={(e) => e.stopPropagation()}>
            <div className="seasonal-menu-title">{menuFor.title}</div>
            <button className="seasonal-menu-item" onClick={() => pickSearch(menuFor)}>
              Search
            </button>
            <button className="seasonal-menu-item" onClick={() => unfollow(menuFor)}>
              Remove
            </button>
            <button className="seasonal-menu-item" disabled title="Not implemented yet">
              Auto-download (coming soon)
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
