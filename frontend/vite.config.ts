import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The browser never talks to GitHub or Devin: a write-scoped PAT is readable in
// devtools, the Devin API will not CORS, and rate limit would scale with open
// tabs. Everything goes through the orchestrator.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: process.env.API_URL ?? "http://localhost:8000", changeOrigin: true },
      "/healthz": { target: process.env.API_URL ?? "http://localhost:8000", changeOrigin: true },
    },
  },
  build: { outDir: "dist", sourcemap: true },
});
