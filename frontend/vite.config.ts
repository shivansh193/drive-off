import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const api = "http://localhost:8000";
export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy: { "/api": api, "/clips": api, "/v1": api, "/docs": api, "/openapi.json": api } },
  build: { chunkSizeWarningLimit: 6000 },
});
