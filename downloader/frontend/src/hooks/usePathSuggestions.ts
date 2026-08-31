import { useCallback, useRef, useState } from "react";
import { api } from "../api";

// Debounced /paths/suggest lookups for the destination field's autocomplete
// dropdown. 200ms debounce matches the original.
export function usePathSuggestions(downloaderId: string) {
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const timer = useRef<number | undefined>(undefined);

  const request = useCallback(
    (path: string) => {
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(async () => {
        try {
          const s = await api<string[]>(
            `/paths/suggest?downloader_id=${encodeURIComponent(downloaderId)}&path=${encodeURIComponent(path)}`
          );
          setSuggestions(s);
        } catch {
          setSuggestions([]);
        }
      }, 200);
    },
    [downloaderId]
  );

  const clear = useCallback(() => setSuggestions([]), []);

  return { suggestions, request, clear };
}
