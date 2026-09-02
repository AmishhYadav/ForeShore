import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { VitePWA } from "vite-plugin-pwa";
import path from "node:path";

// PLAN.md Phase 5, locked decision: offline is not optional. Service worker + IndexedDB
// cache geofence polygons, the last decision envelope (with its validity window), the
// last route, and pre-rendered TTS phrases. See src/shared/offline.ts for what actually
// gets cached; this config only registers the worker.
export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      registerType: "autoUpdate",
      includeAssets: ["favicon.svg"],
      manifest: {
        name: "FORESHORE",
        short_name: "FORESHORE",
        description: "Marine foresight for the small-boat fleet",
        theme_color: "#0b3d5c",
        background_color: "#0b3d5c",
        display: "standalone",
        icons: [],
      },
      workbox: {
        // Runtime-cached by src/shared/offline.ts's own IndexedDB layer instead of a
        // blanket network-first cache — geofence polygons and decision envelopes carry
        // a validity window the app must reason about, which a generic SW cache cannot.
        navigateFallback: "index.html",
      },
    }),
  ],
  resolve: {
    alias: {
      "@shared": path.resolve(__dirname, "./src/shared"),
    },
  },
  server: {
    port: 5173,
    proxy: {
      // Local dev convenience only — src/shared/api.ts talks to VITE_API_BASE directly
      // and does not depend on this proxy; it exists so a relative fetch("/api/...")
      // also works without CORS during dev.
      "/api": "http://localhost:8000",
      "/health": "http://localhost:8000",
      "/ws": { target: "ws://localhost:8000", ws: true },
    },
  },
});
