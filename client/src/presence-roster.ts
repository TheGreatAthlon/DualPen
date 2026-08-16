import * as api from "./api";
import type { NodeOut } from "./api";

const POLL_INTERVAL_MS = 5000;

export interface PresenceRosterCallbacks {
  getCurrentUserId: () => number | null;
  getCurrentDocId: () => string | null;
  /** Resolves a doc_id to a full NodeOut via the already-loaded file tree, so activating an entry can open it. */
  getNodeById: (id: string) => NodeOut | null;
  onOpenDocument: (node: NodeOut) => void;
  announce: (message: string) => void;
}

interface DocGroup {
  docId: string;
  docName: string;
  docPath: string[];
  displayNames: string[];
}

function groupByDoc(entries: api.PresenceEntry[], selfId: number | null): DocGroup[] {
  const groups: DocGroup[] = [];
  const byDocId = new Map<string, DocGroup>();

  for (const entry of entries) {
    if (entry.user_id === selfId) continue;
    let group = byDocId.get(entry.doc_id);
    if (!group) {
      group = { docId: entry.doc_id, docName: entry.doc_name, docPath: entry.doc_path, displayNames: [] };
      byDocId.set(entry.doc_id, group);
      groups.push(group);
    }
    group.displayNames.push(entry.display_name);
  }

  return groups;
}

function describeGroup(group: DocGroup, currentDocId: string | null): string {
  const names = group.displayNames.join(", ");
  if (group.docId === currentDocId) {
    return `${names} — editing this document`;
  }
  const location = group.docPath.length > 0 ? ` in ${group.docPath.join("/")}` : "";
  return `${names} — editing ${group.docName}${location}`;
}

/**
 * Alt+... roster panel listing everyone else currently online app-wide and
 * which document each is in - distinct from doc-collaborators.ts (same
 * document only, driven by Yjs awareness). Backed by a poll against
 * GET /api/presence since cross-document visibility has no awareness/socket
 * of its own (see plan). Polling only runs while the dialog is open.
 *
 * Entries are grouped by document (one row per document, listing every peer
 * editing it) and rendered as a keyboard-navigable listbox: arrow keys/Home/
 * End move a roving-tabindex selection, Enter or a click jumps to that
 * document via onOpenDocument.
 */
export class PresenceRosterPanel {
  private callbacks: PresenceRosterCallbacks;
  private dialog: HTMLDialogElement;
  private listEl: HTMLElement;
  private pollTimer: number | null = null;
  private groups: DocGroup[] = [];
  private activeDocId: string | null = null;

  constructor(callbacks: PresenceRosterCallbacks) {
    this.callbacks = callbacks;

    this.dialog = document.createElement("dialog");
    this.dialog.className = "presence-roster-dialog";
    this.dialog.setAttribute("aria-labelledby", "presence-roster-heading");
    this.dialog.innerHTML = `
      <h2 id="presence-roster-heading">Who's online</h2>
      <ul id="presence-roster-list" class="presence-roster-list" role="listbox" aria-label="Who's online"></ul>
      <div class="settings-buttons">
        <button type="button" id="presence-roster-close-btn">Close</button>
      </div>
    `;
    document.body.appendChild(this.dialog);

    this.listEl = this.dialog.querySelector<HTMLElement>("#presence-roster-list")!;
    this.listEl.addEventListener("keydown", (e) => this.handleKeydown(e));
    this.dialog
      .querySelector<HTMLButtonElement>("#presence-roster-close-btn")!
      .addEventListener("click", () => this.dialog.close());
    this.dialog.addEventListener("close", () => this.stopPolling());
  }

  private getOptionEls(): HTMLLIElement[] {
    return Array.from(this.listEl.querySelectorAll<HTMLLIElement>('li[role="option"]'));
  }

  private setActiveDocId(docId: string | null, focus: boolean): void {
    this.activeDocId = docId;
    for (const el of this.getOptionEls()) {
      const isActive = el.dataset.docId === docId;
      el.tabIndex = isActive ? 0 : -1;
      el.setAttribute("aria-selected", String(isActive));
      if (isActive && focus) el.focus();
    }
  }

  private activateGroup(group: DocGroup): void {
    const node = this.callbacks.getNodeById(group.docId);
    if (!node) {
      this.callbacks.announce("Could not locate that document — it may have moved or been deleted.");
      return;
    }
    this.dialog.close();
    this.callbacks.onOpenDocument(node);
  }

  private handleKeydown(e: KeyboardEvent): void {
    const options = this.getOptionEls();
    if (options.length === 0) return;
    const index = options.findIndex((el) => el.dataset.docId === this.activeDocId);

    switch (e.key) {
      case "ArrowDown": {
        e.preventDefault();
        const next = options[Math.min(index + 1, options.length - 1)];
        if (next?.dataset.docId) this.setActiveDocId(next.dataset.docId, true);
        break;
      }
      case "ArrowUp": {
        e.preventDefault();
        const prev = options[Math.max(index - 1, 0)];
        if (prev?.dataset.docId) this.setActiveDocId(prev.dataset.docId, true);
        break;
      }
      case "Home": {
        e.preventDefault();
        if (options[0]?.dataset.docId) this.setActiveDocId(options[0].dataset.docId, true);
        break;
      }
      case "End": {
        e.preventDefault();
        const last = options[options.length - 1];
        if (last?.dataset.docId) this.setActiveDocId(last.dataset.docId, true);
        break;
      }
      case "Enter": {
        e.preventDefault();
        const group = this.groups.find((g) => g.docId === this.activeDocId);
        if (group) this.activateGroup(group);
        break;
      }
    }
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
    this.groups = groupByDoc(entries, selfId);

    // Preserve keyboard focus/position across a poll-driven re-render: a
    // background refresh must never yank focus back to the top of the list
    // while someone is mid-navigation or just reading. Only restore actual
    // DOM focus when the previously active document is still present;
    // otherwise clear the active id without stealing focus anywhere.
    const hadFocus = this.getOptionEls().some((el) => el === document.activeElement);
    const previousActiveDocId = this.activeDocId;

    this.listEl.innerHTML = "";
    if (this.groups.length === 0) {
      const li = document.createElement("li");
      li.textContent = "No one else is online.";
      this.listEl.appendChild(li);
      this.activeDocId = null;
      return;
    }

    for (const group of this.groups) {
      const li = document.createElement("li");
      li.setAttribute("role", "option");
      li.tabIndex = -1;
      li.setAttribute("aria-selected", "false");
      li.dataset.docId = group.docId;
      li.textContent = describeGroup(group, currentDocId);
      li.addEventListener("click", () => {
        this.setActiveDocId(group.docId, false);
        this.activateGroup(group);
      });
      this.listEl.appendChild(li);
    }

    const stillPresent = this.groups.some((g) => g.docId === previousActiveDocId);
    if (stillPresent) {
      this.setActiveDocId(previousActiveDocId, hadFocus);
    } else {
      // Land roving tabindex on the first item so Tab still reaches the list,
      // but only move DOM focus there if focus was already inside the list.
      this.setActiveDocId(this.groups[0].docId, hadFocus);
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

  /** Fetches presence once; announces "No one else is online." if empty rather
   * than opening an empty dialog, otherwise opens the navigable roster. Used
   * by the Alt+W shortcut so screen-reader users still get a quick spoken
   * answer when solo, but a real navigable list otherwise. */
  async openOrAnnounceIfEmpty(): Promise<void> {
    let entries: api.PresenceEntry[];
    try {
      entries = await api.presence();
    } catch {
      this.callbacks.announce("Could not load who's online.");
      return;
    }
    const selfId = this.callbacks.getCurrentUserId();
    const others = entries.filter((p) => p.user_id !== selfId);
    if (others.length === 0) {
      this.callbacks.announce("No one else is online.");
      return;
    }
    this.open();
  }
}
