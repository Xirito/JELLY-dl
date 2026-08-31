import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Production build is served by FastAPI itself (see downloader/app/main.py:
// "/" -> dist/index.html, "/assets" -> dist/assets) — no proxy needed there.
//
// `npm run dev` proxies the API routes to a real backend so you get HMR
// without a full Docker rebuild per change. Point BACKEND_URL at wherever
// the downloader container is actually reachable during development.
const BACKEND_URL = process.env.BACKEND_URL || "http://192.168.68.4:8790";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/downloaders": BACKEND_URL,
      "/downloads": BACKEND_URL,
      "/interfaces": BACKEND_URL,
      "/paths": BACKEND_URL,
    },
  },
});
