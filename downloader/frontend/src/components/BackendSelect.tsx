import type { DownloaderInfo } from "../types";

interface BackendSelectProps {
  downloaders: DownloaderInfo[];
  value: string;
  onChange: (id: string) => void;
}

export default function BackendSelect({ downloaders, value, onChange }: BackendSelectProps) {
  return (
    <>
      <label>Backend</label>
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        {downloaders.map((d) => (
          <option key={d.id} value={d.id}>
            {d.name}
          </option>
        ))}
      </select>
    </>
  );
}
