import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { fileURLToPath, URL } from "node:url";

// Relative base so the same build works at https://<user>.github.io/<repo>/ and locally,
// without hardcoding the repository name.
export default defineConfig({
  base: "./",
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      // The CV rubric, slop patterns and prompts are shared with the Python backend so the
      // two cannot drift. Imported rather than copied, so there is one source of truth.
      "@shared": fileURLToPath(new URL("../shared", import.meta.url)),
    },
  },
  server: {
    fs: { allow: [".", "../shared"] },
  },
  build: { outDir: "dist", sourcemap: false },
});
