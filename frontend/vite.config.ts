import path from "node:path"
import {defineConfig} from "vitest/config"
import vue from "@vitejs/plugin-vue"

// `make web` builds straight into the Python package, which serves it.
// That directory is gitignored: built assets are never committed.
const outDir = path.resolve(import.meta.dirname, "../src/jailbee/web/static")

export default defineConfig({
    plugins: [vue()],
    base: "./",
    build: {
        outDir,
        emptyOutDir: true,
    },
    test: {
        environment: "jsdom",
        include: ["src/**/*.test.ts"],
        setupFiles: ["src/test-setup.ts"],
    },
    server: {
        // `npm run dev` next to `jailbee remote web serve` (default port 8910).
        proxy: {
            "/api": {target: "http://127.0.0.1:8910", ws: true},
        },
    },
})
