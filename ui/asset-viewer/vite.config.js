import { defineConfig } from "vite";
import { viteSingleFile } from "vite-plugin-singlefile";


export default defineConfig({
  plugins: [viteSingleFile()],
  build: {
    cssMinify: true,
    emptyOutDir: true,
    minify: true,
    modulePreload: false,
    outDir: "dist",
    sourcemap: false,
    rollupOptions: {
      input: "index.html",
    },
  },
});