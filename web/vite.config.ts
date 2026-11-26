/// <reference types="vitest/config" />
import { fileURLToPath } from "node:url";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The API this app talks to during `npm run dev`. The built app is same-origin and needs no address.
const API = process.env["WEB_API_TARGET"] ?? "http://localhost:8000";

export default defineConfig(({ command, isPreview }) => ({
  // Built assets live under /static/, which the front door (deploy/shared/caddy/gateway.caddyfile and
  // services/authlimit/routes.json) already serves without a token. The page itself is `/`. `preview` serves the build, so it needs the
  // build's base although Vite calls it "serve".
  base: command === "build" || isPreview ? "/static/" : "/",
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  server: { proxy: Object.fromEntries(["/chat", "/ingest", "/health", "/usage"].map((path) => [path, API])) },
  build: { outDir: "dist", emptyOutDir: true },
  test: { environment: "jsdom", setupFiles: ["./src/test/setup.ts"], restoreMocks: true, unstubGlobals: true },
}));
