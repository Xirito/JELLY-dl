import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import { VitePWA } from "vite-plugin-pwa";

// Production build is served by FastAPI itself (see downloader/app/main.py:
// "/" -> dist/index.html, "/assets" -> dist/assets) — no proxy needed there.
//
// `npm run dev` proxies the API routes to a real backend so you get HMR
// without a full Docker rebuild per change. Point BACKEND_URL at wherever
// the downloader container is actually reachable during development.
const BACKEND_URL = process.env.BACKEND_URL || "http://192.168.68.4:8790";

export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      registerType: "autoUpdate",
      manifest: {
        name: "JELLY-dl",
        short_name: "JELLY-dl",
        description: "Search and queue downloads to the media library.",
        display: "standalone",
        start_url: "/",
        scope: "/",
        background_color: "#0f1218",
        theme_color: "#0f1218",
        orientation: "portrait",
        icons: [
          { src: "/icon-192.png", sizes: "192x192", type: "image/png", purpose: "any" },
          { src: "/icon-512.png", sizes: "512x512", type: "image/png", purpose: "any" },
          { src: "/maskable-icon-512.png", sizes: "512x512", type: "image/png", purpose: "maskable" },
        ],
      },
      // Deliberately no runtimeCaching entries: /downloaders, /downloads,
      // /interfaces, /paths must always hit the network live (job status,
      // search results). generateSW (the default strategy) only precaches
      // the built app shell — JS/CSS/HTML/icons — which is what we want.
    }),
  ],
  server: {
    proxy: {
      "/downloaders": BACKEND_URL,
      "/downloads": BACKEND_URL,
      "/interfaces": BACKEND_URL,
      "/paths": BACKEND_URL,
    },
  },
});
