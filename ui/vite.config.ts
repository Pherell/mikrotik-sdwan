import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Dev server talks to the API directly so the browser sees one origin and
    // CORS never enters the picture during development.
    //
    // "/api/" with the trailing slash, deliberately. Vite matches these keys
    // as loose prefixes, so a bare "/api" also captured UI routes like
    // /api-access and handed them to the backend, which 404s -- a divergence
    // from production, where Caddy's "/api/*" matches the literal prefix and
    // would have routed that page to the UI.
    proxy: {
      "/api/": { target: "http://localhost:8000", changeOrigin: true },
      "/healthz": { target: "http://localhost:8000", changeOrigin: true },
    },
  },
});
