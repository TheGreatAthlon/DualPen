import * as api from "./api";
import type { ChatMessage } from "./sync";

export interface ChatPanelCallbacks {
  /** Whether anyone else is currently in the document, per the awareness peer list. */
  hasOtherPeers: () => boolean;
  /** Whether a document is currently open at all, distinct from hasOtherPeers. */
  hasDocumentOpen: () => boolean;
  onSend: (body: string) => void;
  announce: (message: string) => void;
}

function formatTime(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

/**
 * The full chat panel: scrollable history (role="log" for append-friendly
 * live-region behavior per the project's accessibility plan) plus a
 * persistent compose box. Built on a native <dialog> (mirrors tree.ts's
 * rename dialog) so focus trapping and Escape-to-close come for free, but
 * created once and shown/hidden via showModal()/close() rather than
 * recreated per open, so message history persists across repeated
 * Shift+F2 presses within the same document session.
 */
export class ChatPanel {
  private callbacks: ChatPanelCallbacks;
  private dialog: HTMLDialogElement;
  private logEl: HTMLElement;
  private composeInput: HTMLInputElement;
  private docId: string | null = null;
  private messageIds = new Set<string>();

  constructor(callbacks: ChatPanelCallbacks) {
    this.callbacks = callbacks;

    this.dialog = document.createElement("dialog");
    this.dialog.className = "chat-dialog";
    this.dialog.setAttribute("aria-labelledby", "chat-dialog-heading");
    this.dialog.innerHTML = `
      <h2 id="chat-dialog-heading">Chat</h2>
      <div class="chat-log" id="chat-log" role="log" aria-label="Chat messages"></div>
      <form class="chat-compose" id="chat-compose-form">
        <label for="chat-compose-input" class="visually-hidden">Chat message</label>
        <input id="chat-compose-input" type="text" autocomplete="off" placeholder="Message" />
        <button type="submit">Send</button>
        <button type="button" id="chat-close-btn">Close</button>
      </form>
    `;
    document.body.appendChild(this.dialog);

    this.logEl = this.dialog.querySelector<HTMLElement>("#chat-log")!;
    this.composeInput = this.dialog.querySelector<HTMLInputElement>("#chat-compose-input")!;
    const form = this.dialog.querySelector<HTMLFormElement>("#chat-compose-form")!;
    const closeBtn = this.dialog.querySelector<HTMLButtonElement>("#chat-close-btn")!;

    form.addEventListener("submit", (e) => {
      e.preventDefault();
      this.trySend();
    });
    closeBtn.addEventListener("click", () => this.dialog.close());
  }

  private trySend(): void {
    const body = this.composeInput.value.trim();
    if (!body) return;
    if (!this.callbacks.hasDocumentOpen()) {
      this.callbacks.announce("No document is open.");
      return;
    }
    if (!this.callbacks.hasOtherPeers()) {
      this.callbacks.announce("No one else is editing this document.");
      return;
    }
    this.callbacks.onSend(body);
    this.composeInput.value = "";
  }

  /** Resets displayed history and loads the given document's chat log. */
  async loadForDocument(docId: string): Promise<void> {
    this.docId = docId;
    this.messageIds.clear();
    this.logEl.innerHTML = "";
    const history = await api.getDocumentChat(docId);
    for (const m of history) {
      this.appendMessage({
        id: m.id,
        docId: m.doc_id,
        userId: m.user_id,
        displayName: m.display_name,
        body: m.body,
        sentAt: m.sent_at,
      });
    }
  }

  /** Clears the panel when no document is open (e.g. after delete/logout). */
  clear(): void {
    this.docId = null;
    this.messageIds.clear();
    this.logEl.innerHTML = "";
    if (this.dialog.open) this.dialog.close();
  }

  receiveMessage(message: ChatMessage, currentUserId: number | null): void {
    if (message.docId !== this.docId) return;
    this.appendMessage(message);
    if (message.userId !== currentUserId) {
      this.callbacks.announce(`${message.displayName} says: ${message.body}`);
    }
  }

  private appendMessage(message: ChatMessage): void {
    if (this.messageIds.has(message.id)) return;
    this.messageIds.add(message.id);

    const wasScrolledToBottom =
      this.logEl.scrollHeight - this.logEl.scrollTop - this.logEl.clientHeight < 4;

    const item = document.createElement("p");
    item.className = "chat-message";
    const meta = document.createElement("span");
    meta.className = "chat-message-meta";
    meta.textContent = `${message.displayName} · ${formatTime(message.sentAt)}`;
    const body = document.createElement("span");
    body.className = "chat-message-body";
    body.textContent = message.body;
    item.append(meta, document.createElement("br"), body);
    this.logEl.appendChild(item);

    if (wasScrolledToBottom) {
      this.logEl.scrollTop = this.logEl.scrollHeight;
    }
  }

  get isOpen(): boolean {
    return this.dialog.open;
  }

  openFocused(): void {
    if (!this.dialog.open) this.dialog.showModal();
    this.composeInput.focus();
  }
}

/**
 * F2 quick-send: a minimal single-line composer dialog, independent of the
 * full ChatPanel above, per the plan's "checks awareness peer list first;
 * Enter sends, Escape cancels" design. Built once and reused like ChatPanel.
 */
export class QuickComposer {
  private dialog: HTMLDialogElement;
  private input: HTMLInputElement;
  private callbacks: {
    hasOtherPeers: () => boolean;
    hasDocumentOpen: () => boolean;
    onSend: (body: string) => void;
    announce: (message: string) => void;
  };

  constructor(callbacks: {
    hasOtherPeers: () => boolean;
    hasDocumentOpen: () => boolean;
    onSend: (body: string) => void;
    announce: (message: string) => void;
  }) {
    this.callbacks = callbacks;

    this.dialog = document.createElement("dialog");
    this.dialog.className = "chat-quick-dialog";
    this.dialog.setAttribute("aria-label", "Quick chat message");
    this.dialog.innerHTML = `
      <form method="dialog" id="chat-quick-form">
        <label for="chat-quick-input" class="visually-hidden">Quick chat message</label>
        <input id="chat-quick-input" type="text" autocomplete="off" placeholder="Quick message (Enter to send, Escape to cancel)" />
      </form>
    `;
    document.body.appendChild(this.dialog);

    this.input = this.dialog.querySelector<HTMLInputElement>("#chat-quick-input")!;
    const form = this.dialog.querySelector<HTMLFormElement>("#chat-quick-form")!;

    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const body = this.input.value.trim();
      this.dialog.close();
      if (body) this.callbacks.onSend(body);
    });
  }

  /** Checks the peer-list precondition and either opens the composer or
   * announces immediately, per the plan's "abort with an announced message
   * rather than sending into the void" F2 behavior. */
  openIfPeersPresent(): void {
    if (!this.callbacks.hasDocumentOpen()) {
      this.callbacks.announce("No document is open.");
      return;
    }
    if (!this.callbacks.hasOtherPeers()) {
      this.callbacks.announce("No one else is editing this document.");
      return;
    }
    this.input.value = "";
    this.dialog.showModal();
    this.input.focus();
  }
}
