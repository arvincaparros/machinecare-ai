import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The RAG API runs separately on port 8001:
//   cd backend && ..\.venv\Scripts\python.exe -m uvicorn app.api.main:app --port 8001
// Both the ask endpoints (JSON and multipart) and the liveness check are
// proxied, so the browser only ever talks to the Vite dev server (no CORS
// configuration needed).
const RAG_API_TARGET = "http://127.0.0.1:8001";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: RAG_API_TARGET,
        changeOrigin: true,
      },
      "/health": {
        target: RAG_API_TARGET,
        changeOrigin: true,
      },
    },
  },
});
