import { useState } from "react";
import type { ScheduleControls } from "../types";

interface AddToScheduleButtonProps {
  schedule: ScheduleControls;
  // nyaa picks carry an "anilist:"/"mal:" id; ani-cli picks only a title
  // (the server matches it on AniList).
  id: string | null;
  title: string;
}

// Loose title comparison -- the schedule stores AniList's title, a search
// result may differ in case/punctuation.
function norm(s: string): string {
  return s.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim();
}

// "+ Add to schedule" for a show found through search (it may never appear
// in "Follow more" -- an older show, or one AniList files differently).
// Turns into "In schedule ✓" once the schedule has it. Parents key this by
// the show, so the local state resets when the pick changes.
export default function AddToScheduleButton({ schedule, id, title }: AddToScheduleButtonProps) {
  const [adding, setAdding] = useState(false);
  const [addedId, setAddedId] = useState<string | null>(null);

  const inSchedule = schedule.followed.some(
    (f) => (id && f.id === id) || (addedId && f.id === addedId) || norm(f.title) === norm(title)
  );

  if (inSchedule) {
    return (
      <button type="button" className="schedule-btn on" disabled>
        In schedule ✓
      </button>
    );
  }
  return (
    <button
      type="button"
      className="schedule-btn"
      disabled={adding}
      onClick={async () => {
        setAdding(true);
        const show = await schedule.add(id, title);
        if (show) setAddedId(show.id);
        setAdding(false);
      }}
    >
      {adding ? "adding…" : "+ Add to schedule"}
    </button>
  );
}
