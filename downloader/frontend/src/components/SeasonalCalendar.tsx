import { useEffect, useRef, useState } from "react";
import type { KeyboardEvent, MouseEvent, PointerEvent } from "react";
import { api } from "../api";
import type { SeasonalShow } from "../types";

interface SeasonalCalendarProps {
  // Fired when the user selects a followed show (left click / tap, or
  // "Search" in its context menu). App.tsx forwards this to whichever
  // anime-capable panel is currently mounted -- this component never talks
  // to a specific downloader itself, it only knows the seasonal-follow list
  // (shared by both).
  onSearch: (show: { id: string; title: string }) => void;
  // The show last selected from here, outlined in the calendar. Owned by
  // App.tsx so it can clear it when the backend changes (the panel the
  // selection was loaded into is reset then).
  selectedId?: string | null;
}

const DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

// Touch has no right-click: a long press opens the context menu instead.
// Android Chrome already turns a long press into a `contextmenu` event, iOS
// Safari doesn't -- this timer covers both (opening the same menu twice is
// harmless).
const LONG_PRESS_MS = 500;
const LONG_PRESS_SLOP_PX = 10;

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

export default function SeasonalCalendar({ onSearch, selectedId }: SeasonalCalendarProps) {
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

  // -- cover interactions ---------------------------------------------------
  // Left click / tap selects (same as "Search" in the menu); right click --
  // or a long press on touch -- opens the context menu.
  const pressTimer = useRef<number | null>(null);
  const pressStart = useRef<{ x: number; y: number } | null>(null);
  // A long press opens the menu while the finger is still down. If the
  // browser then turns the release into a click, that click must neither
  // select the show nor land on the menu's backdrop (closing the menu it
  // just opened) -- so clicks are ignored for a moment after a long press.
  // Time-boxed rather than "skip the next click": most browsers send no
  // click after a long press, and the user's next real tap must still work.
  const suppressClicksUntil = useRef(0);
  const clickSuppressed = () => Date.now() < suppressClicksUntil.current;

  function cancelPress() {
    if (pressTimer.current !== null) window.clearTimeout(pressTimer.current);
    pressTimer.current = null;
    pressStart.current = null;
  }

  useEffect(() => cancelPress, []);

  function coverHandlers(s: SeasonalShow) {
    return {
      onClick: () => {
        if (!clickSuppressed()) pickSearch(s);
      },
      onContextMenu: (e: MouseEvent) => {
        e.preventDefault(); // no browser menu / image "Save as" on top of ours
        // Android's own long-press -> contextmenu beats our timer: same
        // click guard as the timer path below.
        if (pressStart.current) suppressClicksUntil.current = Date.now() + 700;
        cancelPress();
        setMenuFor(s);
      },
      onPointerDown: (e: PointerEvent) => {
        if (e.pointerType !== "touch") return;
        pressStart.current = { x: e.clientX, y: e.clientY };
        pressTimer.current = window.setTimeout(() => {
          pressTimer.current = null;
          suppressClicksUntil.current = Date.now() + 700;
          setMenuFor(s);
        }, LONG_PRESS_MS);
      },
      onPointerMove: (e: PointerEvent) => {
        // a scroll/swipe through the day strip, not a press
        const p = pressStart.current;
        if (p && Math.hypot(e.clientX - p.x, e.clientY - p.y) > LONG_PRESS_SLOP_PX) cancelPress();
      },
      onPointerUp: cancelPress,
      onPointerCancel: cancelPress,
      onPointerLeave: cancelPress,
      onKeyDown: (e: KeyboardEvent) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          pickSearch(s);
        }
        // the ContextMenu key / Shift+F10 fire onContextMenu by themselves
      },
    };
  }

  function renderCover(s: SeasonalShow) {
    return (
      <div
        className={"seasonal-cover" + (s.id === selectedId ? " selected" : "")}
        key={s.id}
        role="button"
        tabIndex={0}
        aria-pressed={s.id === selectedId}
        title={`${s.title} — click to select, right-click for options`}
        {...coverHandlers(s)}
      >
        {s.cover ? <img src={s.cover} alt="" draggable={false} /> : <div className="seasonal-cover-blank" />}
        <span className="seasonal-title">{s.title}</span>
      </div>
    );
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
                  {byDay[i].map(renderCover)}
                </div>
              ))}
            </div>
          )}
          {unscheduled.length > 0 && (
            <>
              <div className="seasonal-day-label" style={{ marginTop: 12 }}>
                Not yet scheduled
              </div>
              <div className="seasonal-unscheduled-row">{unscheduled.map(renderCover)}</div>
            </>
          )}
          {followed.length > 0 && (
            <div className="seasonal-hint muted">
              Click a show to load it · right-click or long-press for more
            </div>
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
        <div className="seasonal-menu-backdrop" onClick={() => !clickSuppressed() && setMenuFor(null)}>
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
