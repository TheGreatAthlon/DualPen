import * as api from "./api";

export function joinTokenFromHash(): string | null {
  const m = /^#join=([\w-]+)$/.exec(window.location.hash);
  return m ? m[1] : null;
}

export function clearJoinHash(): void {
  history.replaceState(null, "", window.location.pathname + window.location.search);
}

function shareUrl(token: string): string {
  return `${window.location.origin}${window.location.pathname}#join=${token}`;
}

/** Display-name form shown to someone opening a share link without a member session. */
export function renderJoinForm(
  app: HTMLElement,
  token: string,
  onJoined: (user: api.CurrentUser) => Promise<void>,
): void {
  app.innerHTML = `
    <main class="login-screen">
      <form id="join-form" aria-label="Join shared document">
        <h1>DualPen</h1>
        <p>You were invited to a shared document. Enter a name to join as a guest.</p>
        <label for="join-name">Your name</label>
        <input id="join-name" type="text" maxlength="64" autocomplete="nickname" required />
        <button type="submit">Join</button>
        <p id="join-error" role="alert"></p>
      </form>
    </main>
  `;
  const errorEl = app.querySelector<HTMLElement>("#join-error")!;
  app.querySelector<HTMLFormElement>("#join-form")!.addEventListener("submit", async (e) => {
    e.preventDefault();
    errorEl.textContent = "";
    const name = app.querySelector<HTMLInputElement>("#join-name")!.value.trim();
    try {
      const user = await api.joinShare(token, name);
      clearJoinHash();
      await onJoined(user);
    } catch (err) {
      errorEl.textContent = err instanceof api.ApiError ? err.message : "Could not join";
    }
  });
}

/** Members' dialog: create edit/view links for the open document, copy them, revoke them. */
export class ShareDialog {
  private dialog: HTMLDialogElement;
  private listEl: HTMLUListElement;
  private statusEl: HTMLElement;
  private docId: string | null = null;

  private opts: { announce: (msg: string) => void };

  constructor(opts: { announce: (msg: string) => void }) {
    this.opts = opts;
    this.dialog = document.createElement("dialog");
    this.dialog.className = "rename-dialog share-dialog";
    this.dialog.setAttribute("aria-labelledby", "share-heading");
    this.dialog.innerHTML = `
      <form method="dialog">
        <h2 id="share-heading">Share document</h2>
        <p>Anyone with a link can join as a guest, no account needed.</p>
        <fieldset>
          <legend>New link permission</legend>
          <label><input type="radio" name="share-mode" value="edit" checked /> Can edit</label>
          <label><input type="radio" name="share-mode" value="view" /> View only</label>
        </fieldset>
        <label for="share-expiry">Link expires</label>
        <select id="share-expiry">
          <option value="">Never</option>
          <option value="1">After 1 hour</option>
          <option value="24">After 1 day</option>
          <option value="168">After 7 days</option>
        </select>
        <button type="button" id="share-create-btn">Create link</button>
        <ul id="share-list" class="share-list" aria-label="Existing links"></ul>
        <p id="share-status" class="rename-dialog-error" role="status"></p>
        <div class="rename-dialog-buttons"><button type="submit">Close</button></div>
      </form>
    `;
    document.body.appendChild(this.dialog);
    this.listEl = this.dialog.querySelector("#share-list")!;
    this.statusEl = this.dialog.querySelector("#share-status")!;
    this.dialog.querySelector("#share-create-btn")!.addEventListener("click", () => void this.create());
  }

  async open(docId: string): Promise<void> {
    this.docId = docId;
    this.statusEl.textContent = "";
    this.listEl.replaceChildren();
    if (!this.dialog.open) this.dialog.showModal();
    await this.refresh();
  }

  private async refresh(): Promise<void> {
    if (!this.docId) return;
    try {
      this.render(await api.listShareLinks(this.docId));
    } catch (err) {
      this.statusEl.textContent = err instanceof api.ApiError ? err.message : "Could not load links";
    }
  }

  private async create(): Promise<void> {
    if (!this.docId) return;
    const mode = this.dialog.querySelector<HTMLInputElement>('input[name="share-mode"]:checked')!.value;
    try {
      const hours = this.dialog.querySelector<HTMLSelectElement>("#share-expiry")!.value;
      await api.createShareLink(this.docId, mode === "view", hours ? Number(hours) : null);
      this.opts.announce("Share link created");
      await this.refresh();
    } catch (err) {
      this.statusEl.textContent = err instanceof api.ApiError ? err.message : "Could not create link";
    }
  }

  private render(links: api.ShareLink[]): void {
    this.listEl.replaceChildren(
      ...links.map((link) => {
        const kind = link.read_only ? "View only" : "Can edit";
        const li = document.createElement("li");
        const label = document.createElement("span");
        label.textContent = link.expires_at ? `${kind}, expires ${new Date(link.expires_at).toLocaleString()}` : kind;
        const input = document.createElement("input");
        input.readOnly = true;
        input.value = shareUrl(link.token);
        input.setAttribute("aria-label", `${kind} link`);
        const copy = document.createElement("button");
        copy.type = "button";
        copy.textContent = "Copy";
        copy.addEventListener("click", async () => {
          try {
            await navigator.clipboard.writeText(input.value);
            this.opts.announce("Link copied");
          } catch {
            input.select();
            this.statusEl.textContent = "Press Ctrl+C to copy the selected link";
          }
        });
        const revoke = document.createElement("button");
        revoke.type = "button";
        revoke.textContent = "Revoke";
        revoke.setAttribute("aria-label", `Revoke ${kind} link`);
        revoke.addEventListener("click", async () => {
          if (!window.confirm("Revoke this link? Guests using it will be disconnected.")) return;
          try {
            await api.revokeShareLink(link.doc_id, link.token);
            this.opts.announce("Link revoked");
            await this.refresh();
          } catch (err) {
            this.statusEl.textContent = err instanceof api.ApiError ? err.message : "Could not revoke link";
          }
        });
        li.append(label, input, copy, revoke);
        return li;
      }),
    );
  }
}
