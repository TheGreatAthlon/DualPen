import "./style.css";
import "./monaco-setup";
import * as monaco from "monaco-editor/editor/editor.api.js";
import { MonacoBinding } from "y-monaco";
import * as api from "./api";
import type { NodeOut } from "./api";
import { FileTree } from "./tree";
import { DocSyncConnection, CLOSE_REPLACED_BY_NEWER_SESSION } from "./sync";
import type { ChatMessage } from "./sync";
import { attachRemoteCursorStyles } from "./remote-cursors";
import {
  attachPresenceSounds,
  getPresenceSoundMuted,
  getPresenceSoundVolume,
  setPresenceSoundMuted,
  setPresenceSoundVolume,
  playChatNotifySound,
} from "./presence-sounds";
import { CollaboratorCycler } from "./jump-to-collaborator";
import { ChatPanel, QuickComposer } from "./chat";
import {
  SettingsPanel,
  applyAccessibilitySupportBodyClass,
  loadAccessibilitySupportPref,
  loadFontFamily,
  loadFontSize,
} from "./settings";
import { MarkdownPreviewPanel } from "./markdown-preview";
import { attachDocCollaboratorsList } from "./doc-collaborators";
import { PresenceRosterPanel } from "./presence-roster";
import { ShortcutsHelpPanel } from "./shortcuts-help";

const TRASH_FOLDER_NAME = "Trash";
// Idle threshold after the last keystroke before a peer's isTyping flips back
// to false, matching the "clean boolean edge, not a raw timestamp" design so
// peers don't each have to interpret staleness themselves.
const TYPING_IDLE_MS = 1500;
// Slightly longer than the presence roster's 5s poll since tree changes are
// rarer and this interval runs continuously for the whole logged-in session,
// not just while a dialog is open.
const TREE_POLL_INTERVAL_MS = 8000;

const app = document.querySelector<HTMLDivElement>("#app")!;

let currentUser: api.CurrentUser | null = null;
let currentDocument: NodeOut | null = null;
let fileTree: FileTree | null = null;
let monacoEditor: monaco.editor.IStandaloneCodeEditor | null = null;
let currentModel: monaco.editor.ITextModel | null = null;
let currentSync: DocSyncConnection | null = null;
let currentBinding: MonacoBinding | null = null;
let currentCursorListener: monaco.IDisposable | null = null;
let currentContentListener: monaco.IDisposable | null = null;
let detachRemoteCursorStyles: (() => void) | null = null;
let detachPresenceSounds: (() => void) | null = null;
let detachDocCollaboratorsList: (() => void) | null = null;
let typingIdleTimer: number | null = null;
let collaboratorCycler: CollaboratorCycler | null = null;
let chatPanel: ChatPanel | null = null;
let quickComposer: QuickComposer | null = null;
let settingsPanel: SettingsPanel | null = null;
let markdownPreviewPanel: MarkdownPreviewPanel | null = null;
let presenceRosterPanel: PresenceRosterPanel | null = null;
let shortcutsHelpPanel: ShortcutsHelpPanel | null = null;
let treeRefreshTimer: number | null = null;
// Tracks whether #move-status's live-region text was last set for "moving"
// or "not moving", so updateTreeToolbar() (which reruns on every arrow-key
// selection change while a move is pending) only re-announces on the actual
// mark-for-move/cancel/complete transition, not on every navigation.
let lastAnnouncedMoving = false;
// Bumped on every openDocument()/teardown call so a slow-to-connect previous
// document can't win a race and bind itself after the user has already
// moved on to a different one.
let openGeneration = 0;

function renderLogin(): void {
  app.innerHTML = `
    <main class="login-screen">
      <form id="login-form" aria-label="Log in">
        <h1>DualPen</h1>
        <label for="username">Username</label>
        <input id="username" name="username" type="text" autocomplete="username" required />
        <label for="password">Password</label>
        <input id="password" name="password" type="password" autocomplete="current-password" required />
        <button type="submit">Log in</button>
        <p id="login-error" role="alert"></p>
      </form>
    </main>
  `;

  const form = document.querySelector<HTMLFormElement>("#login-form")!;
  const errorEl = document.querySelector<HTMLParagraphElement>("#login-error")!;

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    errorEl.textContent = "";
    const username = (document.querySelector<HTMLInputElement>("#username")!).value;
    const password = (document.querySelector<HTMLInputElement>("#password")!).value;
    try {
      currentUser = await api.login(username, password);
      await renderApp();
    } catch (err) {
      errorEl.textContent = err instanceof api.ApiError ? err.message : "Login failed";
    }
  });
}

function teardownSync(): void {
  currentCursorListener?.dispose();
  currentCursorListener = null;
  currentContentListener?.dispose();
  currentContentListener = null;
  if (typingIdleTimer !== null) {
    window.clearTimeout(typingIdleTimer);
    typingIdleTimer = null;
  }
  detachRemoteCursorStyles?.();
  detachRemoteCursorStyles = null;
  detachPresenceSounds?.();
  detachPresenceSounds = null;
  detachDocCollaboratorsList?.();
  detachDocCollaboratorsList = null;
  collaboratorCycler = null;
  chatPanel?.clear();
  currentBinding?.destroy();
  currentBinding = null;
  currentSync?.destroy();
  currentSync = null;
}

function setEditorModel(content: string): void {
  if (!monacoEditor) return;
  // Monaco models are heap objects the editor doesn't own outright; swapping
  // in a new one without disposing the old one leaks memory on every
  // document switch, since nothing else references it once detached.
  const previousModel = currentModel;
  currentModel = monaco.editor.createModel(content, "plaintext");
  monacoEditor.setModel(currentModel);
  previousModel?.dispose();
}

function clearEditor(placeholder: string): void {
  if (!monacoEditor) return;
  teardownSync();
  openGeneration += 1;
  const previousModel = currentModel;
  currentModel = monaco.editor.createModel(placeholder, "plaintext");
  monacoEditor.setModel(currentModel);
  previousModel?.dispose();
  monacoEditor.updateOptions({ readOnly: true });
}

async function openDocument(node: NodeOut): Promise<void> {
  const titleEl = document.querySelector<HTMLElement>("#editor-title");
  const statusEl = document.querySelector<HTMLElement>("#save-status");
  if (!monacoEditor || !titleEl || !statusEl) return;

  teardownSync();
  const generation = ++openGeneration;

  currentDocument = node;
  titleEl.textContent = node.name;
  monacoEditor.updateOptions({ readOnly: true });
  setEditorModel("Loading...");
  statusEl.textContent = "";

  void chatPanel?.loadForDocument(node.id);

  const sync = new DocSyncConnection(
    node.id,
    () => {
      if (generation !== openGeneration || !monacoEditor) return;
      setEditorModel("");
      currentBinding = new MonacoBinding(
        sync.ytext,
        currentModel!,
        new Set([monacoEditor]),
        sync.awareness,
      );
      detachRemoteCursorStyles = attachRemoteCursorStyles(sync.awareness);
      detachPresenceSounds = attachPresenceSounds(sync.awareness);
      const collaboratorsEl = document.querySelector<HTMLElement>("#doc-collaborators");
      if (collaboratorsEl) {
        detachDocCollaboratorsList = attachDocCollaboratorsList(sync.awareness, collaboratorsEl);
      }
      collaboratorCycler = new CollaboratorCycler();
      setLocalAwarenessUser(sync);
      currentCursorListener = monacoEditor.onDidChangeCursorSelection((e) => {
        setLocalAwarenessCursor(sync, e.selection.getPosition(), isTyping());
      });
      currentContentListener = monacoEditor.onDidChangeModelContent(() => {
        markTyping(sync);
      });
      const initialPosition = monacoEditor.getPosition();
      if (initialPosition) setLocalAwarenessCursor(sync, initialPosition, false);
      monacoEditor.updateOptions({ readOnly: false });
      statusEl.textContent = "";
      monacoEditor.focus();
    },
    (closeCode) => {
      if (generation !== openGeneration) return;
      teardownSync();
      currentDocument = null;
      titleEl.textContent = "No document open";
      setEditorModel("");
      statusEl.textContent =
        closeCode === CLOSE_REPLACED_BY_NEWER_SESSION
          ? "This document was closed because you opened another document in a different tab or window."
          : "Failed to load document";
    },
    (message: ChatMessage) => {
      if (generation !== openGeneration) return;
      chatPanel?.receiveMessage(message, currentUser?.id ?? null);
      if (message.userId !== currentUser?.id) playChatNotifySound();
    },
  );
  currentSync = sync;
}

interface AwarenessUser {
  id: number;
  name: string;
}

interface AwarenessCursor {
  lineNumber: number;
  column: number;
  isTyping: boolean;
}

function setLocalAwarenessUser(sync: DocSyncConnection): void {
  if (!currentUser) return;
  const user: AwarenessUser = { id: currentUser.id, name: currentUser.display_name };
  sync.awareness.setLocalStateField("user", user);
}

function setLocalAwarenessCursor(
  sync: DocSyncConnection,
  position: monaco.Position,
  typing: boolean,
): void {
  const cursor: AwarenessCursor = {
    lineNumber: position.lineNumber,
    column: position.column,
    isTyping: typing,
  };
  sync.awareness.setLocalStateField("cursor", cursor);
}

function isTyping(): boolean {
  return typingIdleTimer !== null;
}

// Flips the local isTyping awareness field true on keystroke, then debounces
// it back to false after TYPING_IDLE_MS of no further edits, so peers
// receive a clean boolean edge rather than a raw timestamp to interpret.
function markTyping(sync: DocSyncConnection): void {
  const wasTyping = typingIdleTimer !== null;
  if (typingIdleTimer !== null) window.clearTimeout(typingIdleTimer);
  typingIdleTimer = window.setTimeout(() => {
    typingIdleTimer = null;
    const position = monacoEditor?.getPosition();
    if (position) setLocalAwarenessCursor(sync, position, false);
  }, TYPING_IDLE_MS);

  if (!wasTyping) {
    const position = monacoEditor?.getPosition();
    if (position) setLocalAwarenessCursor(sync, position, true);
  }
}

function announce(message: string): void {
  const el = document.querySelector<HTMLElement>("#tree-announcer");
  if (!el) return;
  // Clear first so identical consecutive messages still get announced by
  // screen readers (a live region only fires on a text change).
  el.textContent = "";
  window.setTimeout(() => {
    el.textContent = message;
  }, 30);
}

async function moveNode(nodeId: string, newParentId: string | null): Promise<void> {
  try {
    const updated = await api.updateNode(nodeId, {
      parent_id: newParentId ?? undefined,
      clear_parent: newParentId === null,
    });
    await refreshTree(updated.id);
  } catch (err) {
    const message = err instanceof api.ApiError ? err.message : "Move failed";
    announce(`Could not move item: ${message}`);
  }
}

async function renameNode(nodeId: string, newName: string): Promise<void> {
  try {
    const updated = await api.updateNode(nodeId, { name: newName });
    await refreshTree(updated.id);
    if (currentDocument?.id === updated.id) {
      currentDocument = updated;
      const titleEl = document.querySelector<HTMLElement>("#editor-title");
      if (titleEl) titleEl.textContent = updated.name;
    }
  } catch (err) {
    const message = err instanceof api.ApiError ? err.message : "Rename failed";
    announce(`Could not rename item: ${message}`);
  }
}

async function deleteToTrash(node: NodeOut): Promise<void> {
  if (!fileTree) return;

  const trash = fileTree.findRootFolderByName(TRASH_FOLDER_NAME);
  const inTrash = !!trash && fileTree.isDescendantOfNode(node.id, trash.id);

  if (inTrash) {
    await deletePermanently(node);
  } else {
    await moveToTrash(node);
  }
}

async function moveToTrash(node: NodeOut): Promise<void> {
  if (!fileTree) return;

  try {
    const trash = fileTree.findRootFolderByName(TRASH_FOLDER_NAME);
    const trashId = trash ? trash.id : (await api.createFolder(TRASH_FOLDER_NAME, null)).id;

    const pathPrefix = fileTree.getAncestorPath(node.id).join("-");
    const trashedName = pathPrefix ? `${pathPrefix}-${node.name}` : node.name;

    // Two separate calls (rename, then move) since the PATCH endpoint can do
    // both at once but we want the trashed name to reflect the *original*
    // location, which we only know before the move happens.
    const renamed = await api.updateNode(node.id, { name: trashedName });
    await api.updateNode(renamed.id, { parent_id: trashId });

    if (currentDocument && fileTree.isDescendantOfNode(currentDocument.id, node.id)) {
      currentDocument = null;
      const titleEl = document.querySelector<HTMLElement>("#editor-title");
      if (titleEl) titleEl.textContent = "No document open";
      clearEditor("");
    }

    await refreshTree(trashId);
    announce(`Moved ${node.name} to Trash.`);
  } catch (err) {
    const message = err instanceof api.ApiError ? err.message : "Delete failed";
    announce(`Could not delete ${node.name}: ${message}`);
  }
}

async function deletePermanently(node: NodeOut): Promise<void> {
  if (!fileTree) return;

  if (node.kind !== "folder" || !fileTree.isEmptyFolder(node.id)) {
    announce(
      node.kind !== "folder"
        ? `Cannot permanently delete ${node.name}: files cannot be deleted, only moved to Trash.`
        : `Cannot permanently delete ${node.name}: only empty folders can be permanently deleted.`,
    );
    return;
  }

  try {
    if (currentDocument && fileTree.isDescendantOfNode(currentDocument.id, node.id)) {
      currentDocument = null;
      const titleEl = document.querySelector<HTMLElement>("#editor-title");
      if (titleEl) titleEl.textContent = "No document open";
      clearEditor("");
    }

    await api.deleteNode(node.id);
    await refreshTree();
    announce(`Permanently deleted ${node.name}.`);
  } catch (err) {
    const message = err instanceof api.ApiError ? err.message : "Delete failed";
    announce(`Could not permanently delete ${node.name}: ${message}`);
  }
}

async function downloadZipExport(nodeId: string | null): Promise<void> {
  try {
    const { blob, filename } = await api.exportZip(nodeId);
    // Browsers only let a click on an <a download> element trigger a save;
    // the element is never inserted into visible layout, just attached long
    // enough for the synthetic click to fire.
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    announce(`Exported ${filename}.`);
  } catch (err) {
    const message = err instanceof api.ApiError ? err.message : "Export failed";
    announce(`Could not export: ${message}`);
  }
}

function updateTreeToolbar(active: NodeOut | null, markedForMove: NodeOut | null): void {
  const renameBtn = document.querySelector<HTMLButtonElement>("#rename-btn");
  const moveBtn = document.querySelector<HTMLButtonElement>("#move-btn");
  const pasteBtn = document.querySelector<HTMLButtonElement>("#paste-btn");
  const cancelBtn = document.querySelector<HTMLButtonElement>("#cancel-move-btn");
  const deleteBtn = document.querySelector<HTMLButtonElement>("#delete-btn");
  const exportSelectedBtn = document.querySelector<HTMLButtonElement>("#export-selected-zip-btn");
  const statusEl = document.querySelector<HTMLElement>("#move-status");
  if (!renameBtn || !moveBtn || !pasteBtn || !cancelBtn || !deleteBtn || !exportSelectedBtn || !statusEl) return;

  const moving = markedForMove !== null;
  const trash = fileTree?.findRootFolderByName(TRASH_FOLDER_NAME) ?? null;
  const activeInTrash = !!active && !!trash && fileTree!.isDescendantOfNode(active.id, trash.id);

  renameBtn.disabled = !active || moving;
  moveBtn.hidden = moving;
  moveBtn.disabled = !active;
  pasteBtn.hidden = !moving;
  pasteBtn.disabled = !moving || !active;
  cancelBtn.hidden = !moving;
  deleteBtn.hidden = moving;
  deleteBtn.disabled = !active;
  deleteBtn.textContent = activeInTrash ? "Delete permanently" : "Delete";
  exportSelectedBtn.disabled = !active || moving;

  // Only touch the live region's text on an actual moving-state transition
  // (marked for move / cancelled / completed) - reassigning it on every
  // arrow-key selection change while already moving would re-announce the
  // identical text on each keypress.
  if (moving !== lastAnnouncedMoving) {
    lastAnnouncedMoving = moving;
    statusEl.textContent = moving ? `Moving "${markedForMove.name}" — select a destination and click "Paste here".` : "";
  }
}

async function refreshTree(focusId?: string): Promise<void> {
  const treeContainer = document.querySelector<HTMLElement>("#tree-container");
  if (!treeContainer) return;
  const flat = await api.getTree();
  if (!fileTree) {
    fileTree = new FileTree(treeContainer, {
      onSelectDocument: (node) => void openDocument(node),
      onMoveNode: (nodeId, newParentId) => void moveNode(nodeId, newParentId),
      onRenameNode: (nodeId, newName) => void renameNode(nodeId, newName),
      onDeleteNode: (node) => void deleteToTrash(node),
      onSelectionChange: updateTreeToolbar,
      announce,
    });
  }
  fileTree.render(flat, focusId);
}

function setUpTreePolling(): void {
  // Guarded singleton like setUpChat/setUpSettings/etc. above - renderApp()
  // re-runs on every login, and stacking a second setInterval would double
  // (then triple, ...) the refresh rate on repeated logins in the same page
  // session. FileTree.render() already preserves expanded/active/focus state
  // across re-renders (see tree.ts), so polling never disrupts an
  // in-progress rename/move or steals focus.
  if (treeRefreshTimer !== null) return;
  treeRefreshTimer = window.setInterval(() => void refreshTree(), TREE_POLL_INTERVAL_MS);
}

async function renderApp(): Promise<void> {
  app.innerHTML = `
    <div class="app-shell">
      <header class="app-header">
        <h1>DualPen</h1>
        <div class="header-right">
          <span id="user-info"></span>
          <button id="presence-btn" type="button">Who's online</button>
          <button id="logout-btn" type="button">Log out</button>
        </div>
      </header>
      <div class="panes">
        <section id="tree-pane" aria-label="File tree">
          <div class="tree-toolbar">
            <button id="new-folder-btn" type="button">New folder</button>
            <button id="new-doc-btn" type="button">New document</button>
            <button id="import-zip-btn" type="button">Import zip</button>
            <input type="file" id="import-zip-input" accept=".zip" class="visually-hidden" />
            <button id="export-zip-btn" type="button">Export zip</button>
          </div>
          <div class="tree-toolbar tree-toolbar-secondary">
            <button id="rename-btn" type="button" disabled>Rename</button>
            <button id="move-btn" type="button" disabled>Move</button>
            <button id="paste-btn" type="button" disabled hidden>Paste here</button>
            <button id="cancel-move-btn" type="button" hidden>Cancel move</button>
            <button id="delete-btn" type="button" disabled>Delete</button>
            <button id="export-selected-zip-btn" type="button" disabled>Export selected as zip</button>
          </div>
          <p id="move-status" class="tree-move-status" role="status"></p>
          <div id="tree-container"></div>
          <div id="tree-announcer" role="status" class="visually-hidden"></div>
        </section>
        <section id="editor-pane" aria-label="Document editor">
          <div class="editor-toolbar">
            <span id="editor-title">No document open</span>
            <span id="tab-focus-indicator" role="status">Tab moves focus: OFF</span>
            <span id="doc-collaborators" role="status"></span>
            <button id="settings-btn" type="button">Settings</button>
            <span id="save-status" role="status"></span>
          </div>
          <div id="editor-container" role="none"></div>
        </section>
      </div>
    </div>
  `;

  const userInfo = document.querySelector<HTMLElement>("#user-info")!;
  userInfo.textContent = currentUser ? `Signed in as ${currentUser.display_name}` : "";

  document.querySelector<HTMLButtonElement>("#logout-btn")!.addEventListener("click", async () => {
    await api.logout();
    currentUser = null;
    currentDocument = null;
    fileTree = null;
    teardownSync();
    openGeneration += 1;
    currentModel?.dispose();
    currentModel = null;
    monacoEditor?.dispose();
    monacoEditor = null;
    if (treeRefreshTimer !== null) {
      window.clearInterval(treeRefreshTimer);
      treeRefreshTimer = null;
    }
    renderLogin();
  });

  document.querySelector<HTMLButtonElement>("#new-folder-btn")!.addEventListener("click", async () => {
    const name = window.prompt("Folder name:");
    if (!name) return;
    const parentId = fileTree?.getSelectedFolderId() ?? null;
    await api.createFolder(name, parentId);
    await refreshTree();
  });

  document.querySelector<HTMLButtonElement>("#new-doc-btn")!.addEventListener("click", async () => {
    const name = window.prompt("Document name:");
    if (!name) return;
    const parentId = fileTree?.getSelectedFolderId() ?? null;
    const node = await api.createDocument(name, parentId);
    await refreshTree();
    await openDocument(node);
  });

  const importZipInput = document.querySelector<HTMLInputElement>("#import-zip-input")!;
  document.querySelector<HTMLButtonElement>("#import-zip-btn")!.addEventListener("click", () => {
    importZipInput.click();
  });
  importZipInput.addEventListener("change", async () => {
    const file = importZipInput.files?.[0];
    importZipInput.value = "";
    if (!file) return;
    const parentId = fileTree?.getSelectedFolderId() ?? null;
    try {
      const result = await api.importZip(file, parentId);
      await refreshTree(result.root.id);
      const skippedNote =
        result.skipped.length > 0
          ? ` ${result.skipped.length} file(s) could not be imported (not readable text): ${result.skipped.join(", ")}.`
          : "";
      announce(`Imported "${result.root.name}".${skippedNote}`);
    } catch (err) {
      const message = err instanceof api.ApiError ? err.message : "Import failed";
      announce(`Could not import zip: ${message}`);
    }
  });

  document.querySelector<HTMLButtonElement>("#export-zip-btn")!.addEventListener("click", async () => {
    await downloadZipExport(null);
  });

  document.querySelector<HTMLButtonElement>("#export-selected-zip-btn")!.addEventListener("click", async () => {
    const active = fileTree?.getActiveNode();
    if (active) await downloadZipExport(active.id);
  });

  document.querySelector<HTMLButtonElement>("#rename-btn")!.addEventListener("click", () => {
    fileTree?.renameActiveItem();
  });

  document.querySelector<HTMLButtonElement>("#move-btn")!.addEventListener("click", () => {
    const active = fileTree?.getActiveNode();
    if (active) fileTree?.markForMove(active.id);
  });

  document.querySelector<HTMLButtonElement>("#paste-btn")!.addEventListener("click", () => {
    fileTree?.pasteIntoSelection();
  });

  document.querySelector<HTMLButtonElement>("#cancel-move-btn")!.addEventListener("click", () => {
    fileTree?.cancelMove();
  });

  document.querySelector<HTMLButtonElement>("#delete-btn")!.addEventListener("click", () => {
    const active = fileTree?.getActiveNode();
    if (active) void deleteToTrash(active);
  });

  setUpMonaco();
  setUpChat();
  setUpSettings();
  setUpMarkdownPreview();
  setUpPresenceRoster();
  setUpShortcutsHelp();
  setUpTreePolling();

  await refreshTree();
}

function hasOtherPeersInCurrentDoc(): boolean {
  if (!currentSync) return false;
  const localClientId = currentSync.awareness.clientID;
  for (const clientId of currentSync.awareness.getStates().keys()) {
    if (clientId !== localClientId) return true;
  }
  return false;
}

function setUpChat(): void {
  // Dialogs attach themselves to document.body, outside the #app subtree
  // that renderApp() replaces on every call - build them once (guarded,
  // since renderApp() re-runs on every login) so re-logging-in doesn't
  // stack duplicate dialogs in the DOM.
  if (!chatPanel) {
    chatPanel = new ChatPanel({
      hasOtherPeers: hasOtherPeersInCurrentDoc,
      hasDocumentOpen: () => currentDocument !== null,
      onSend: (body) => currentSync?.sendChatMessage(body),
      announce,
    });
  }
  if (!quickComposer) {
    quickComposer = new QuickComposer({
      hasOtherPeers: hasOtherPeersInCurrentDoc,
      hasDocumentOpen: () => currentDocument !== null,
      onSend: (body) => currentSync?.sendChatMessage(body),
      announce,
    });
  }
}

function setUpMonaco(): void {
  const container = document.querySelector<HTMLElement>("#editor-container");
  if (!container) return;

  currentModel = monaco.editor.createModel("", "plaintext");
  monacoEditor = monaco.editor.create(container, {
    model: currentModel,
    automaticLayout: true,
    readOnly: true,
    accessibilitySupport: loadAccessibilitySupportPref() ? "on" : "off",
    fontFamily: loadFontFamily(),
    fontSize: loadFontSize(),
  });

  updateTabFocusIndicator(monacoEditor.getOption(monaco.editor.EditorOption.tabFocusMode));
  monacoEditor.onDidChangeConfiguration((e: monaco.editor.ConfigurationChangedEvent) => {
    if (!monacoEditor) return;
    if (e.hasChanged(monaco.editor.EditorOption.tabFocusMode)) {
      updateTabFocusIndicator(monacoEditor.getOption(monaco.editor.EditorOption.tabFocusMode));
    }
  });

  // Monaco's own Ctrl+M binding for toggleTabFocusMode already works while
  // the editor has focus; nothing extra needed there. F6 is intercepted
  // separately below since it must work regardless of focus location.
  monacoEditor.addCommand(monaco.KeyCode.F6, () => {
    focusTreePane();
  });
}

function updateTabFocusIndicator(on: boolean): void {
  const el = document.querySelector<HTMLElement>("#tab-focus-indicator");
  if (el) el.textContent = `Tab moves focus: ${on ? "ON" : "OFF"}`;
}

function setUpSettings(): void {
  // Dialog attaches itself to document.body, outside the #app subtree that
  // renderApp() replaces on every call - build it once (guarded, since
  // renderApp() re-runs on every login) so re-logging-in doesn't stack
  // duplicate dialogs in the DOM (mirrors setUpChat()).
  if (!settingsPanel) {
    settingsPanel = new SettingsPanel({
      onAccessibilitySupportChange: (on) => {
        monacoEditor?.updateOptions({ accessibilitySupport: on ? "on" : "off" });
      },
      onFontChange: (family, size) => {
        monacoEditor?.updateOptions({ fontFamily: family, fontSize: size });
      },
      onPresenceMutedChange: setPresenceSoundMuted,
      onPresenceVolumeChange: setPresenceSoundVolume,
      getPresenceMuted: getPresenceSoundMuted,
      getPresenceVolume: getPresenceSoundVolume,
    });
  }

  document.querySelector<HTMLButtonElement>("#settings-btn")!.addEventListener("click", () => {
    settingsPanel?.openFocused();
  });
}

function setUpMarkdownPreview(): void {
  // Same guarded-singleton reasoning as setUpSettings()/setUpChat() above:
  // attaches to document.body outside the #app subtree, so build once.
  if (!markdownPreviewPanel) {
    markdownPreviewPanel = new MarkdownPreviewPanel();
  }
}

function setUpPresenceRoster(): void {
  // Same guarded-singleton reasoning as the other panels above.
  if (!presenceRosterPanel) {
    presenceRosterPanel = new PresenceRosterPanel({
      getCurrentUserId: () => currentUser?.id ?? null,
      getCurrentDocId: () => currentDocument?.id ?? null,
      getNodeById: (id) => fileTree?.getNode(id) ?? null,
      onOpenDocument: (node) => void openDocument(node),
      announce,
    });
  }

  document.querySelector<HTMLButtonElement>("#presence-btn")!.addEventListener("click", () => {
    presenceRosterPanel?.open();
  });
}

function setUpShortcutsHelp(): void {
  // Same guarded-singleton reasoning as the other panels above.
  if (!shortcutsHelpPanel) {
    shortcutsHelpPanel = new ShortcutsHelpPanel();
  }
}

function focusEditorPane(): void {
  monacoEditor?.focus();
}

function focusTreePane(): void {
  fileTree?.focusTree();
}

// Registered once at module scope (not per renderApp() call) since renderApp
// re-runs on every login and would otherwise stack duplicate listeners.
window.addEventListener("keydown", (e) => {
  if (e.key !== "F6") return;
  const treeEl = document.querySelector<HTMLElement>("#tree-container");
  const editorEl = document.querySelector<HTMLElement>("#editor-container");
  const active = document.activeElement;
  const treeHasFocus = !!treeEl && !!active && treeEl.contains(active);
  const editorHasFocus = !!editorEl && !!active && editorEl.contains(active);

  // Monaco's own F6 command (registered in setUpMonaco) handles the case
  // where the editor already has focus, so this window-level listener only
  // needs to cover the tree-focused (and neither-focused) cases — but
  // Monaco's internal textarea can still let this bubble up in some
  // browsers, so guard against double-handling explicitly.
  if (editorHasFocus) return;

  e.preventDefault();
  if (treeHasFocus) {
    focusEditorPane();
  } else {
    focusTreePane();
  }
});

// Registered once at module scope, like the F6 handler above, so it works
// regardless of which pane currently has focus (though it's only meaningful
// while a document with an active sync connection is open).
window.addEventListener("keydown", (e) => {
  if (e.key !== "j" && e.key !== "J") return;
  if (!e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
  if (!monacoEditor || !currentSync || !collaboratorCycler) return;

  const peer = collaboratorCycler.next(currentSync.awareness);
  if (!peer) {
    announce("No one else is editing this document.");
    return;
  }

  e.preventDefault();
  const position = { lineNumber: peer.lineNumber, column: peer.column };
  monacoEditor.setPosition(position);
  monacoEditor.revealPositionInCenter(position);
  monacoEditor.focus();
  announce(`Jumped to ${peer.name}, line ${peer.lineNumber}`);
});

// F2: quick-send composer. Shift+F2: full chat panel. Both registered once
// at module scope like F6/Alt+J above. The chat panel gets its own
// Escape-to-close behavior (native <dialog> "cancel" event) instead of
// joining the F6 pane cycle, per the plan's two-pane mental model.
window.addEventListener("keydown", (e) => {
  if (e.key !== "F2") return;
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if (!currentSync || !currentDocument) return;

  // The file tree has its own F2 handler (rename) scoped to its treeitems;
  // when focus is there, this global handler must stand down instead of
  // also popping the chat composer/panel on the same keypress.
  const treeContainer = document.querySelector<HTMLElement>("#tree-container");
  if (treeContainer?.contains(document.activeElement)) return;

  e.preventDefault();
  if (e.shiftKey) {
    chatPanel?.openFocused();
  } else {
    quickComposer?.openIfPeersPresent();
  }
});

// Alt+R: markdown preview of the current document. Registered once at
// module scope like the other global shortcuts above.
window.addEventListener("keydown", (e) => {
  if (e.key !== "r" && e.key !== "R") return;
  if (!e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
  if (!currentModel || !currentDocument) return;

  e.preventDefault();
  markdownPreviewPanel?.open(currentModel.getValue(), currentDocument.name);
});

// Alt+W: who's online. Announces "No one else is online." to the live
// region when solo (so screen-reader users get a quick spoken answer without
// a dialog popping up for nothing); otherwise opens the navigable roster
// panel instead of just reading a summary aloud. Registered once at module
// scope like the others.
window.addEventListener("keydown", (e) => {
  if (e.key !== "w" && e.key !== "W") return;
  if (!e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;

  e.preventDefault();
  void presenceRosterPanel?.openOrAnnounceIfEmpty();
});

// F1 (or Alt+F1 as a fallback, since some browsers/OSes intercept bare F1
// for their own help before JS ever sees it): keyboard shortcuts help.
// Registered once at module scope like the other global shortcuts above,
// so it works regardless of focus location (including when a tree item
// has focus, since tree.ts's own keydown handler only handles keys on
// treeitem elements and doesn't claim F1).
window.addEventListener("keydown", (e) => {
  if (e.key !== "F1") return;
  if (e.ctrlKey || e.metaKey || e.shiftKey) return; // altKey optionally set, both bare and Alt+F1 accepted
  e.preventDefault();
  shortcutsHelpPanel?.open();
});

async function init(): Promise<void> {
  try {
    currentUser = await api.me();
    await renderApp();
  } catch {
    renderLogin();
  }
}

applyAccessibilitySupportBodyClass(loadAccessibilitySupportPref());
void init();
