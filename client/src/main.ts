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
import { SettingsPanel, loadAccessibilitySupportPref, loadFontFamily, loadFontSize } from "./settings";

const TRASH_FOLDER_NAME = "Trash";
// Idle threshold after the last keystroke before a peer's isTyping flips back
// to false, matching the "clean boolean edge, not a raw timestamp" design so
// peers don't each have to interpret staleness themselves.
const TYPING_IDLE_MS = 1500;

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
let typingIdleTimer: number | null = null;
let collaboratorCycler: CollaboratorCycler | null = null;
let chatPanel: ChatPanel | null = null;
let quickComposer: QuickComposer | null = null;
let settingsPanel: SettingsPanel | null = null;
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
        <h1>Collab Editor</h1>
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
  if (trash && fileTree.isDescendantOfNode(node.id, trash.id)) {
    announce(`${node.name} is already in Trash and cannot be deleted again.`);
    return;
  }

  try {
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
  deleteBtn.disabled = !active || activeInTrash;
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

async function renderApp(): Promise<void> {
  app.innerHTML = `
    <div class="app-shell">
      <header class="app-header">
        <h1>Collab Editor</h1>
        <div class="header-right">
          <span id="user-info"></span>
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
      onSend: (body) => currentSync?.sendChatMessage(body),
      announce,
    });
  }
  if (!quickComposer) {
    quickComposer = new QuickComposer({
      hasOtherPeers: hasOtherPeersInCurrentDoc,
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
  if (!currentSync) return;

  e.preventDefault();
  if (e.shiftKey) {
    chatPanel?.openFocused();
  } else {
    quickComposer?.openIfPeersPresent();
  }
});

async function init(): Promise<void> {
  try {
    currentUser = await api.me();
    await renderApp();
  } catch {
    renderLogin();
  }
}

void init();
