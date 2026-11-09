// services/ui/vite.config.js
// Dev: `npm run dev` serves the UI on http://localhost:5173 and forwards /api (REST + WebSocket)
// to the API on 127.0.0.1:8000. Build: `npm run build` -> dist/, which the API serves at /.
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const API = process.env.COUNTER_API || "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 5173,
    proxy: {
      "/api": { target: API, ws: true, changeOrigin: true },
    },
  },
  build: { outDir: "dist", emptyOutDir: true, sourcemap: false },
});
