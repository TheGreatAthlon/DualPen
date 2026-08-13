import EditorWorker from "monaco-editor/editor/editor.worker.js?worker";

declare global {
  interface Window {
    MonacoEnvironment?: { getWorker(moduleId: string, label: string): Worker };
  }
}

// Monaco's language services run in web workers. Vite doesn't know about
// Monaco's internal AMD-style worker loading, so the global entry point
// below is Monaco's documented hook for supplying workers under a bundler:
// https://github.com/microsoft/monaco-editor/blob/main/docs/integrate-esm.md
// We only ever create plain-text models in this app (no JSON/CSS/TS/HTML
// editing), so the generic editor worker is the only one that's ever needed.
self.MonacoEnvironment = {
  getWorker() {
    return new EditorWorker();
  },
};
