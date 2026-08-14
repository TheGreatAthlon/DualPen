import { marked } from "marked";
import DOMPurify from "dompurify";

/**
 * Alt+R modal: renders the current document's markdown source as HTML,
 * built on a native <dialog> (mirrors settings.ts's SettingsPanel), created
 * once and reused. Content is collaboratively editable by other users, so
 * the parsed HTML is sanitized before insertion rather than trusted as-is.
 */
export class MarkdownPreviewPanel {
  private dialog: HTMLDialogElement;
  private bodyEl: HTMLElement;

  constructor() {
    this.dialog = document.createElement("dialog");
    this.dialog.className = "markdown-preview-dialog";
    this.dialog.setAttribute("aria-labelledby", "markdown-preview-heading");
    this.dialog.innerHTML = `
      <h2 id="markdown-preview-heading">Markdown preview</h2>
      <div id="markdown-preview-body" class="markdown-preview-body"></div>
      <div class="settings-buttons">
        <button type="button" id="markdown-preview-close-btn">Close</button>
      </div>
    `;
    document.body.appendChild(this.dialog);

    this.bodyEl = this.dialog.querySelector<HTMLElement>("#markdown-preview-body")!;
    this.dialog
      .querySelector<HTMLButtonElement>("#markdown-preview-close-btn")!
      .addEventListener("click", () => this.dialog.close());
  }

  open(markdownSource: string, docTitle: string): void {
    this.dialog.setAttribute("aria-label", `Markdown preview: ${docTitle}`);
    this.bodyEl.innerHTML = DOMPurify.sanitize(marked.parse(markdownSource, { async: false }));
    if (!this.dialog.open) this.dialog.showModal();
  }
}
