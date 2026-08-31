interface DownloadButtonProps {
  disabled: boolean;
  onClick: () => void;
  msg: string;
  msgIsError: boolean;
}

export default function DownloadButton({ disabled, onClick, msg, msgIsError }: DownloadButtonProps) {
  return (
    <div style={{ marginTop: 14, display: "flex", gap: 10, alignItems: "center" }}>
      <button className="primary" disabled={disabled} onClick={onClick}>
        Download
      </button>
      <span className="muted" style={{ color: msgIsError ? "var(--err)" : "var(--muted)" }}>
        {msg}
      </span>
    </div>
  );
}
