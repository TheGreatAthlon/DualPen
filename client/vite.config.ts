import { defineConfig } from "vite";
import { fileURLToPath } from "node:url";

// y-monaco@0.1.6 imports "monaco-editor/esm/vs/editor/editor.api.js" directly.
// monaco-editor@0.56.0's package.json #exports only maps "./*" -> "./esm/vs/*",
// so that specifier resolves to the nonexistent "esm/vs/esm/vs/editor/editor.api.js"
// (double-nested) under Vite's bundler resolution. Alias the broken specifier
// straight to the real file - it's the same physical module our own code
// already loads via "monaco-editor/editor/editor.api.js", so no duplicate
// Monaco instance is created.
export default defineConfig({
  resolve: {
    alias: {
      "monaco-editor/esm/vs/editor/editor.api.js": fileURLToPath(
        new URL("./node_modules/monaco-editor/esm/vs/editor/editor.api.js", import.meta.url),
      ),
    },
  },
});
