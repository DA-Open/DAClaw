import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "path";

// In dev, the backend gates /api with a session token. We pin it via
// DACLAW_WEB_DEV_TOKEN (also read by web_server.py) and attach it to every
// proxied /api request so `npm run dev` hot-reload can talk to the backend.
const DEV_TOKEN = process.env.DACLAW_WEB_DEV_TOKEN || "dev-daclaw-token";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  build: {
    outDir: "../daclaw_cli/web_dist",
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/api": {
        target: "http://127.0.0.1:9119",
        changeOrigin: true,
        configure: (proxy) => {
          proxy.on("proxyReq", (proxyReq) => {
            if (!proxyReq.getHeader("authorization")) {
              proxyReq.setHeader("authorization", `Bearer ${DEV_TOKEN}`);
            }
          });
        },
      },
    },
  },
});
