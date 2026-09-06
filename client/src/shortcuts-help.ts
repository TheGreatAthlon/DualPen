const SHORTCUT_GROUPS: { title: string; items: [string, string][] }[] = [
  {
    title: "Editor & document",
    items: [
      ["F6", "Move focus between file tree and editor"],
      ["Alt+J", "Jump to next collaborator's cursor"],
      ["F2", "Quick chat composer"],
      ["Shift+F2", "Open full chat panel"],
      ["Ctrl+F", "Find in current document"],
      ["Ctrl+H", "Find and replace in current document"],
      ["F3 / Shift+F3", "Next / previous match"],
      ["Alt+R", "Open markdown preview of current document in a new tab"],
      ["Alt+W", "Announce who's online"],
      ["Ctrl+M", "Toggle Tab moves focus vs. inserts tab (editor)"],
      ["F1 / Alt+F1", "Show this help"],
    ],
  },
  {
    title: "File tree",
    items: [
      ["Arrow Up / Down", "Move to previous/next item"],
      ["Arrow Right", "Expand folder / move into it"],
      ["Arrow Left", "Collapse folder / move to parent"],
      ["Home / End", "Jump to first/last item"],
      ["Enter", "Open document / toggle folder"],
      ["Ctrl+X", "Mark item for move"],
      ["Ctrl+V", "Move marked item here"],
      ["F2", "Rename item"],
      ["Delete", "Delete item"],
      ["Escape", "Cancel pending move"],
    ],
  },
];

/**
 * F1/Alt+F1 reference modal: static list of every shortcut in the app,
 * across both the editor pane and the file tree. Same <dialog> pattern as
 * the other panels, but with no dynamic data to wire up.
 */
export class ShortcutsHelpPanel {
  private dialog: HTMLDialogElement;

  constructor() {
    const sections = SHORTCUT_GROUPS.map(
      (g) => `
        <h3>${g.title}</h3>
        <table class="shortcuts-table">
          ${g.items
            .map(([key, desc]) => `<tr><td><kbd>${key}</kbd></td><td>${desc}</td></tr>`)
            .join("")}
        </table>`,
    ).join("");

    this.dialog = document.createElement("dialog");
    this.dialog.className = "shortcuts-help-dialog";
    this.dialog.setAttribute("aria-labelledby", "shortcuts-help-heading");
    this.dialog.innerHTML = `
      <h2 id="shortcuts-help-heading">Keyboard shortcuts</h2>
      ${sections}
      <div class="settings-buttons">
        <button type="button" id="shortcuts-help-close-btn">Close</button>
      </div>
    `;
    document.body.appendChild(this.dialog);

    this.dialog
      .querySelector<HTMLButtonElement>("#shortcuts-help-close-btn")!
      .addEventListener("click", () => this.dialog.close());
  }

  open(): void {
    if (!this.dialog.open) this.dialog.showModal();
  }
}
