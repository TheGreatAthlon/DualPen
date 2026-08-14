import * as api from "./api";

const POLL_INTERVAL_MS = 5000;

export interface PresenceRosterCallbacks {
  getCurrentUserId: () => number | null;
  getCurrentDocId: () => string | null;
}

/**
 * Alt+... roster panel listing everyone else currently online app-wide and
 * which document each is in - distinct from doc-collaborators.ts (same
 * document only, driven by Yjs awareness). Backed by a poll against
 * GET /api/presence since cross-document visibility has no awareness/socket
 * of its own (see plan). Polling only runs while the dialog is open.
 */
export class PresenceRosterPanel {
  private callbacks: PresenceRosterCallbacks;
  private dialog: HTMLDialogElement;
  private listEl: HTMLElement;
  private pollTimer: number | null = null;

  constructor(callbacks: PresenceRosterCallbacks) {
    this.callbacks = callbacks;

    this.dialog = document.createElement("dialog");
    this.dialog.className = "presence-roster-dialog";
    this.dialog.setAttribute("aria-labelledby", "presence-roster-heading");
    this.dialog.innerHTML = `
      <h2 id="presence-roster-heading">Who's online</h2>
      <ul id="presence-roster-list" class="presence-roster-list"></ul>
      <div class="settings-buttons">
        <button type="button" id="presence-roster-close-btn">Close</button>
      </div>
    `;
    document.body.appendChild(this.dialog);

    this.listEl = this.dialog.querySelector<HTMLElement>("#presence-roster-list")!;
    this.dialog
      .querySelector<HTMLButtonElement>("#presence-roster-close-btn")!
      .addEventListener("click", () => this.dialog.close());
    this.dialog.addEventListener("close", () => this.stopPolling());
  }

  private async refresh(): Promise<void> {
    let entries: api.PresenceEntry[];
    try {
      entries = await api.presence();
    } catch {
      return;
    }

    const selfId = this.callbacks.getCurrentUserId();
    const currentDocId = this.callbacks.getCurrentDocId();
    const others = entries.filter((p) => p.user_id !== selfId);

    this.listEl.innerHTML = "";
    if (others.length === 0) {
      const li = document.createElement("li");
      li.textContent = "No one else is online.";
      this.listEl.appendChild(li);
      return;
    }

    for (const peer of others) {
      const li = document.createElement("li");
      li.textContent =
        peer.doc_id === currentDocId
          ? `${peer.display_name} — editing this document`
          : `${peer.display_name} — editing ${peer.doc_name}`;
      this.listEl.appendChild(li);
    }
  }

  private stopPolling(): void {
    if (this.pollTimer !== null) {
      window.clearInterval(this.pollTimer);
      this.pollTimer = null;
    }
  }

  open(): void {
    if (!this.dialog.open) this.dialog.showModal();
    void this.refresh();
    this.stopPolling();
    this.pollTimer = window.setInterval(() => void this.refresh(), POLL_INTERVAL_MS);
  }
}
