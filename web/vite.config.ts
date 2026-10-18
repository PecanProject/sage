import { resolve } from "node:path"
import babel from "@rolldown/plugin-babel"
import tailwindcss from "@tailwindcss/vite"
import react, { reactCompilerPreset } from "@vitejs/plugin-react"
import { defineConfig } from "vite"
import { viteStaticCopy } from "vite-plugin-static-copy"

// pdf.js fetches these at runtime by URL: image decoders (JBIG2, JPEG 2000), the standard fonts and CMaps a PDF
// doesn't embed, and colour profiles. pdf-viewer.tsx points pdf.js at the copies under /pdfjs.
const PDFJS_ASSETS = ["wasm", "standard_fonts", "cmaps", "iccs"].map((dir) => ({
  src: `node_modules/pdfjs-dist/${dir}/*`,
  dest: `pdfjs/${dir}`,
  rename: { stripBase: true as const },
}))

export default defineConfig({
  plugins: [
    react(),
    babel({ presets: [reactCompilerPreset()] }),
    tailwindcss(),
    viteStaticCopy({ targets: PDFJS_ASSETS }),
  ],
  resolve: {
    alias: {
      "@": resolve(import.meta.dirname, "./src"),
    },
  },
  build: {
    outDir: resolve(import.meta.dirname, "../sage/web_dist"),
    emptyOutDir: true,
    chunkSizeWarningLimit: 2000,
  },
  server: {
    host: process.env.SAGE_WEB_HOST ?? "127.0.0.1",
    port: Number(process.env.SAGE_WEB_PORT ?? 5173),
    strictPort: true,
    // Vite rejects unknown Host headers; allow the tailnet name when serving beyond localhost.
    allowedHosts: [".ts.net"],
    proxy: {
      "/api": { target: `http://127.0.0.1:${process.env.SAGE_API_PORT ?? "8765"}`, changeOrigin: true },
    },
  },
})
