import { marked } from "marked";
import DOMPurify from "dompurify";

/**
 * Alt+R: renders the current document's markdown source as HTML and shows it
 * in a separate browser tab rather than an in-page modal.
 *
 * A modal <dialog> was the original surface, but screen readers ingest a
 * modal's entire accessibility subtree at once when it appears; for long or
 * structurally busy renders (roughly >5 KB of output) that causes a multi-
 * second freeze. A normal top-level document is read incrementally instead,
 * so we hand the render off to its own tab.
 *
 * Document content is collaboratively editable by other users, so the parsed
 * HTML is sanitized before it goes into the generated page.
 */

// Stable window name so repeated Alt+R presses reuse one preview tab instead
// of piling up new ones.
const PREVIEW_WINDOW_NAME = "markdown-preview";

export interface MarkdownPreviewOptions {
  announce: (message: string) => void;
}

export class MarkdownPreviewPanel {
  private announce: (message: string) => void;

  constructor(options: MarkdownPreviewOptions) {
    this.announce = options.announce;
  }

  open(markdownSource: string, docTitle: string): void {
    // Must be called synchronously from the Alt+R keydown handler (it is) so
    // the browser treats it as a user-initiated navigation, not a popup.
    // A stable window name means the first press opens a tab and later
    // presses reuse it. document.write() into that window (rather than a
    // blob: URL) avoids the browser restriction on pointing a *named*
    // window at a blob: URL.
    const win = window.open("", PREVIEW_WINDOW_NAME);
    if (!win) {
      this.announce(
        "Couldn't open the markdown preview tab. Check your browser's popup settings.",
      );
      return;
    }

    const bodyHtml = DOMPurify.sanitize(marked.parse(markdownSource, { async: false }));
    const html = buildPreviewDocument(bodyHtml, docTitle);
    win.document.open();
    win.document.write(html);
    win.document.close();
    win.focus();
  }
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/**
 * Wraps the sanitized render in a self-contained HTML document. The new tab
 * shares none of the app's CSS, so styling is inlined here and kept theme-
 * aware via prefers-color-scheme.
 */
function buildPreviewDocument(bodyHtml: string, docTitle: string): string {
  const safeTitle = escapeHtml(docTitle);
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<meta name="color-scheme" content="light dark" />
<title>Markdown preview: ${safeTitle}</title>
<style>
  :root { color-scheme: light dark; }
  body {
    margin: 0;
    padding: 2rem 1.25rem 4rem;
    font: 16px/1.6 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    color: #1a1a1a;
    background: #ffffff;
  }
  main { max-width: 46rem; margin: 0 auto; }
  h1.markdown-preview-doc-title {
    font-size: 1.1rem;
    font-weight: 600;
    color: #555;
    margin: 0 0 1.5rem;
    padding-bottom: 0.5rem;
    border-bottom: 1px solid #ddd;
  }
  main > :not(h1.markdown-preview-doc-title):first-of-type { margin-top: 0; }
  pre {
    overflow-x: auto;
    padding: 10px;
    background: #f4f4f4;
    border-radius: 6px;
  }
  code { font-family: ui-monospace, Menlo, Consolas, monospace; }
  table { border-collapse: collapse; }
  th, td { border: 1px solid #ccc; padding: 4px 8px; }
  img { max-width: 100%; }
  a { color: #0645ad; }
  @media (prefers-color-scheme: dark) {
    body { color: #e6e6e6; background: #1e1e1e; }
    h1.markdown-preview-doc-title { color: #aaa; border-bottom-color: #444; }
    pre { background: #2a2a2a; }
    th, td { border-color: #555; }
    a { color: #6ca0f6; }
  }
</style>
</head>
<body>
<main>
<h1 class="markdown-preview-doc-title">Markdown preview: ${safeTitle}</h1>
${bodyHtml}
</main>
</body>
</html>`;
}
