import type { FormatOption, Mode } from "../types";
import { fmtSize } from "../format";

const ALL_PRESETS: { mode: Mode; label: string }[] = [
  { mode: "best_video_audio", label: "Best video+audio" },
  { mode: "best_audio", label: "Best audio" },
  { mode: "best_video_only", label: "Best video only" },
  { mode: "manual", label: "Manual…" },
];

interface FormatPickerProps {
  availModes: Mode[];
  showManual: boolean;
  mode: Mode;
  onModeChange: (m: Mode) => void;
  formats: FormatOption[];
  manualId: string | null;
  onManualPick: (id: string) => void;
  formatsHint: string;
}

export default function FormatPicker({
  availModes,
  showManual,
  mode,
  onModeChange,
  formats,
  manualId,
  onManualPick,
  formatsHint,
}: FormatPickerProps) {
  return (
    <div className="card" id="fmtCard">
      <label>Format</label>
      <div className="presets" id="presets">
        {ALL_PRESETS.filter((p) => p.mode !== "manual" || showManual)
          .filter((p) => availModes.includes(p.mode))
          .map((p) => (
            <button
              key={p.mode}
              className={mode === p.mode ? "on" : ""}
              onClick={() => onModeChange(p.mode)}
            >
              {p.label}
            </button>
          ))}
      </div>
      {mode === "manual" && (
        <div id="formats">
          {formats.map((f) => (
            <div
              key={f.format_id}
              className={"item" + (manualId === f.format_id ? " sel" : "")}
              onClick={() => onManualPick(f.format_id)}
            >
              <span className="t">{f.label}</span>
              <span className="m">{f.filesize_approx ? fmtSize(f.filesize_approx) : ""}</span>
            </div>
          ))}
        </div>
      )}
      <div className="muted" id="fmtHint" style={{ marginTop: 6 }}>
        {formatsHint}
      </div>
    </div>
  );
}
