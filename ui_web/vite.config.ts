import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server proxies the backend (laser_hhrp_ui server on 127.0.0.1:8080).
const backend = process.env.LASER_HHRP_UI_BACKEND ?? "http://127.0.0.1:8080";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    proxy: {
      "/api": backend,
      "/ws": { target: backend.replace(/^http/, "ws"), ws: true },
    },
  },
  build: { outDir: "dist", sourcemap: false },
});
