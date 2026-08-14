import type { NodeOut } from "./api";

interface TreeNode extends NodeOut {
  children: TreeNode[];
}

export interface TreeCallbacks {
  onSelectDocument: (node: NodeOut) => void;
  onMoveNode: (nodeId: string, newParentId: string | null) => void;
  onRenameNode: (nodeId: string, newName: string) => void;
  onDeleteNode: (node: NodeOut) => void;
  announce: (message: string) => void;
  /** Fires whenever the active item or the marked-for-move item changes, so a toolbar can update button state. */
  onSelectionChange?: (active: NodeOut | null, markedForMove: NodeOut | null) => void;
}

// Mirrors the server-side rule in server/app/schemas.py (validate_node_name)
// so the dialog can reject bad input immediately; the server remains the
// real enforcement point since this check is trivially bypassable.
const INVALID_NAME_CHARS = /[\\/:*?"<>|\x00-\x1f]/;

export function validateNodeName(name: string): string | null {
  const trimmed = name.trim().replace(/^\.+|\.+$/g, "");
  if (!trimmed) return "Name cannot be empty.";
  if (INVALID_NAME_CHARS.test(name)) return 'Name cannot contain \\ / : * ? " < > | or control characters.';
  return null;
}

function buildTree(flat: NodeOut[]): TreeNode[] {
  const byId = new Map<string, TreeNode>();
  for (const n of flat) byId.set(n.id, { ...n, children: [] });

  const roots: TreeNode[] = [];
  for (const n of byId.values()) {
    if (n.parent_id && byId.has(n.parent_id)) {
      byId.get(n.parent_id)!.children.push(n);
    } else {
      roots.push(n);
    }
  }

  const sortChildren = (nodes: TreeNode[]) => {
    nodes.sort((a, b) => (a.kind !== b.kind ? (a.kind === "folder" ? -1 : 1) : a.name.localeCompare(b.name)));
    for (const n of nodes) sortChildren(n.children);
  };
  sortChildren(roots);
  return roots;
}

// Standard W3C APG tree "type-ahead" timing: characters typed within this
// window accumulate into one search string (so typing "do" quickly matches
// "document2"); a pause longer than this starts a fresh search instead.
const TYPEAHEAD_RESET_MS = 500;

export class FileTree {
  private container: HTMLElement;
  private callbacks: TreeCallbacks;
  private expanded = new Set<string>();
  private nodesById = new Map<string, NodeOut>();
  private treeEl: HTMLUListElement | null = null;
  private activeId: string | null = null;
  private markedForMoveId: string | null = null;
  private typeaheadBuffer = "";
  private typeaheadLastKeyAt = 0;

  constructor(container: HTMLElement, callbacks: TreeCallbacks) {
    this.container = container;
    this.callbacks = callbacks;
  }

  render(flat: NodeOut[], focusId?: string | null): void {
    // A full rebuild destroys whatever tree item currently has DOM focus.
    // If the tree (or nothing in particular) had focus before the rebuild,
    // restore it afterward so a refresh never silently kicks focus out to
    // <body> — only skip this when focus was deliberately elsewhere (e.g.
    // the user is typing in the editor pane) so we don't steal it.
    const hadTreeFocus = !!this.treeEl && this.treeEl.contains(document.activeElement);

    this.nodesById = new Map(flat.map((n) => [n.id, n]));
    const roots = buildTree(flat);

    this.container.innerHTML = "";
    const tree = document.createElement("ul");
    tree.setAttribute("role", "tree");
    tree.setAttribute("aria-label", "Documents and folders");
    tree.className = "file-tree";
    tree.addEventListener("keydown", (e) => this.handleKeydown(e));
    this.treeEl = tree;

    for (const root of roots) this.renderNode(root, tree, 1);

    this.container.appendChild(tree);

    if (focusId !== undefined) this.activeId = focusId;
    this.ensureRovingTabindex();
    if (hadTreeFocus || focusId) {
      this.setActive(this.activeId ?? "", true);
    }
    this.notifySelectionChange();
  }

  private notifySelectionChange(): void {
    const active = this.activeId ? (this.nodesById.get(this.activeId) ?? null) : null;
    const marked = this.markedForMoveId ? (this.nodesById.get(this.markedForMoveId) ?? null) : null;
    this.callbacks.onSelectionChange?.(active, marked);
  }

  private renderNode(node: TreeNode, parentEl: HTMLElement, level: number): void {
    const li = document.createElement("li");
    li.setAttribute("role", "treeitem");
    li.setAttribute("id", `treeitem-${node.id}`);
    li.setAttribute("aria-level", String(level));
    li.setAttribute("aria-selected", "false");
    li.dataset.nodeId = node.id;
    li.dataset.kind = node.kind;
    li.tabIndex = -1;

    const label = document.createElement("span");
    label.className = "tree-label";
    const markedPrefix = node.id === this.markedForMoveId ? "[Moving] " : "";
    label.textContent = markedPrefix + node.name;
    li.appendChild(label);

    if (node.id === this.markedForMoveId) {
      li.classList.add("tree-marked-for-move");
    }

    li.addEventListener("click", (e) => {
      e.stopPropagation();
      this.setActive(node.id);
      if (node.kind === "folder") {
        this.toggleExpanded(node.id);
      } else {
        this.callbacks.onSelectDocument(node);
      }
    });

    if (node.kind === "folder") {
      const isExpanded = this.expanded.has(node.id);
      li.setAttribute("aria-expanded", String(isExpanded));

      const group = document.createElement("ul");
      group.setAttribute("role", "group");
      group.hidden = !isExpanded;
      if (node.children.length > 0) {
        for (const child of node.children) this.renderNode(child, group, level + 1);
      } else {
        const empty = document.createElement("li");
        empty.className = "tree-empty";
        empty.textContent = "No documents";
        group.appendChild(empty);
      }
      li.appendChild(group);
    }

    parentEl.appendChild(li);
  }

  private getAllItems(): HTMLLIElement[] {
    if (!this.treeEl) return [];
    return Array.from(this.treeEl.querySelectorAll<HTMLLIElement>('[role="treeitem"]'));
  }

  private getVisibleItems(): HTMLLIElement[] {
    return this.getAllItems().filter((el) => {
      let parent = el.parentElement;
      while (parent && parent !== this.treeEl) {
        if (parent instanceof HTMLElement && parent.hidden) return false;
        parent = parent.parentElement;
      }
      return true;
    });
  }

  /** Resolves nodeId to a visible <li> for focus/tabindex purposes: itself
   * if visible, otherwise the nearest ancestor folder that is (walking up
   * through collapsed groups), otherwise the first visible item as a last
   * resort. Needed because a node can become hidden out from under a
   * previously-valid activeId (e.g. it was just moved into a folder that's
   * currently collapsed) - a stale reference to a hidden node must never be
   * treated as a valid focus/tabindex target. */
  private resolveVisibleTarget(nodeId: string | null): HTMLLIElement | undefined {
    const visible = this.getVisibleItems();
    if (visible.length === 0) return undefined;

    let current = nodeId ? this.nodesById.get(nodeId) : undefined;
    while (current) {
      const el = visible.find((item) => item.dataset.nodeId === current!.id);
      if (el) return el;
      current = current.parent_id ? this.nodesById.get(current.parent_id) : undefined;
    }
    return visible[0];
  }

  private ensureRovingTabindex(): void {
    const items = this.getAllItems();
    if (items.length === 0) return;

    // The roving-tabindex target must be a *visible* item - an element with
    // tabindex="0" inside a collapsed (hidden) group is unreachable by Tab
    // at all, and since every other item is tabindex="-1", that leaves
    // nothing in the tree reachable by keyboard. This can happen whenever
    // activeId points at a node nested inside a currently-collapsed
    // ancestor (e.g. an item just moved into a collapsed folder).
    const activeEl = this.resolveVisibleTarget(this.activeId);
    if (!activeEl) return;

    for (const item of items) item.tabIndex = -1;
    activeEl.tabIndex = 0;
    this.activeId = activeEl.dataset.nodeId ?? null;
  }

  /** The folder that new items should be created inside, based on the current selection. */
  getSelectedFolderId(): string | null {
    if (!this.activeId) return null;
    const active = this.nodesById.get(this.activeId);
    if (!active) return null;
    if (active.kind === "folder") return active.id;
    return active.parent_id;
  }

  /** Ancestor folder names from root to (excluding) the node itself, e.g. ["Work", "Projects"]. */
  getAncestorPath(nodeId: string): string[] {
    const path: string[] = [];
    let current = this.nodesById.get(nodeId);
    while (current?.parent_id) {
      const parent = this.nodesById.get(current.parent_id);
      if (!parent) break;
      path.unshift(parent.name);
      current = parent;
    }
    return path;
  }

  isDescendantOfNode(nodeId: string, ancestorId: string): boolean {
    return this.isDescendantOf(nodeId, ancestorId) || nodeId === ancestorId;
  }

  isEmptyFolder(nodeId: string): boolean {
    const node = this.nodesById.get(nodeId);
    if (!node || node.kind !== "folder") return false;
    for (const candidate of this.nodesById.values()) {
      if (candidate.parent_id === nodeId) return false;
    }
    return true;
  }

  findRootFolderByName(name: string): NodeOut | null {
    for (const node of this.nodesById.values()) {
      if (node.kind === "folder" && node.parent_id === null && node.name === name) return node;
    }
    return null;
  }

  private setActive(nodeId: string, focus = true): void {
    // Resolve to a visible target first: nodeId itself if visible, otherwise
    // the nearest visible ancestor (see resolveVisibleTarget) - a hidden
    // node (e.g. just moved into a currently-collapsed folder) must never
    // become the tabindex=0/focus target, since browsers can't focus a
    // hidden element and every other item would already be tabindex=-1.
    const targetEl = this.resolveVisibleTarget(nodeId);
    const targetId = targetEl?.dataset.nodeId ?? nodeId;

    const items = this.getAllItems();
    for (const item of items) {
      const isActive = item.dataset.nodeId === targetId;
      item.tabIndex = isActive ? 0 : -1;
      item.setAttribute("aria-selected", String(isActive));
    }
    this.activeId = targetId;
    this.notifySelectionChange();
    if (focus) {
      targetEl?.focus();
    }
  }

  private toggleExpanded(nodeId: string): void {
    if (this.expanded.has(nodeId)) {
      this.expanded.delete(nodeId);
    } else {
      this.expanded.add(nodeId);
    }
    const flat = Array.from(this.nodesById.values());
    this.render(flat, nodeId);
  }

  private handleKeydown(e: KeyboardEvent): void {
    const target = e.target as HTMLLIElement;
    if (!target || target.getAttribute("role") !== "treeitem") return;

    const nodeId = target.dataset.nodeId;
    if (!nodeId) return;
    const node = this.nodesById.get(nodeId);
    if (!node) return;

    const visible = this.getVisibleItems();
    const index = visible.indexOf(target);

    switch (e.key) {
      case "ArrowDown": {
        e.preventDefault();
        this.resetTypeahead();
        const next = visible[index + 1];
        if (next?.dataset.nodeId) this.setActive(next.dataset.nodeId);
        break;
      }
      case "ArrowUp": {
        e.preventDefault();
        this.resetTypeahead();
        const prev = visible[index - 1];
        if (prev?.dataset.nodeId) this.setActive(prev.dataset.nodeId);
        break;
      }
      case "ArrowRight": {
        e.preventDefault();
        this.resetTypeahead();
        if (node.kind === "folder") {
          if (!this.expanded.has(nodeId)) {
            this.toggleExpanded(nodeId);
          } else {
            const next = visible[index + 1];
            if (next?.dataset.nodeId) this.setActive(next.dataset.nodeId);
          }
        }
        break;
      }
      case "ArrowLeft": {
        e.preventDefault();
        this.resetTypeahead();
        if (node.kind === "folder" && this.expanded.has(nodeId)) {
          this.toggleExpanded(nodeId);
        } else {
          const parentGroup = target.parentElement;
          const parentItem = parentGroup?.closest('[role="treeitem"]') as HTMLLIElement | null;
          if (parentItem?.dataset.nodeId) this.setActive(parentItem.dataset.nodeId);
        }
        break;
      }
      case "Home": {
        e.preventDefault();
        this.resetTypeahead();
        const first = visible[0];
        if (first?.dataset.nodeId) this.setActive(first.dataset.nodeId);
        break;
      }
      case "End": {
        e.preventDefault();
        this.resetTypeahead();
        const last = visible[visible.length - 1];
        if (last?.dataset.nodeId) this.setActive(last.dataset.nodeId);
        break;
      }
      case "Enter": {
        e.preventDefault();
        this.resetTypeahead();
        if (node.kind === "document") {
          this.callbacks.onSelectDocument(node);
        } else {
          this.toggleExpanded(nodeId);
        }
        break;
      }
      case "x":
      case "X": {
        if (!e.ctrlKey) {
          this.handleTypeahead(e, visible, index);
          break;
        }
        e.preventDefault();
        this.markForMove(nodeId);
        break;
      }
      case "F2": {
        e.preventDefault();
        this.openRenameDialog(node);
        break;
      }
      case "v":
      case "V": {
        if (!e.ctrlKey) {
          this.handleTypeahead(e, visible, index);
          break;
        }
        e.preventDefault();
        this.completeMove(nodeId);
        break;
      }
      case "Escape": {
        if (!this.markedForMoveId) break;
        e.preventDefault();
        this.cancelMove();
        break;
      }
      case "Delete": {
        if (this.markedForMoveId) break;
        e.preventDefault();
        this.callbacks.onDeleteNode(node);
        break;
      }
      default: {
        this.handleTypeahead(e, visible, index);
        break;
      }
    }
  }

  /** Clears any in-progress type-ahead search. Called by every navigation/
   * action key that isn't itself part of a type-ahead sequence (arrows,
   * Home/End, Enter) so a stale buffer from an earlier search never gets
   * silently appended to after focus has moved for an unrelated reason. */
  private resetTypeahead(): void {
    this.typeaheadBuffer = "";
    this.typeaheadLastKeyAt = 0;
  }

  /** W3C APG tree "type-ahead": typing a printable character moves focus to
   * the next visible item whose name starts with it (wrapping around, and
   * starting the search just after the current item so repeated presses of
   * the same letter cycle through all matches). Characters typed in quick
   * succession accumulate into a longer search string instead. */
  private handleTypeahead(e: KeyboardEvent, visible: HTMLLIElement[], index: number): void {
    if (e.key.length !== 1 || e.ctrlKey || e.metaKey || e.altKey) return;

    const now = performance.now();
    if (now - this.typeaheadLastKeyAt > TYPEAHEAD_RESET_MS) {
      this.typeaheadBuffer = "";
    }
    this.typeaheadLastKeyAt = now;
    this.typeaheadBuffer += e.key.toLowerCase();
    e.preventDefault();

    // Repeated presses of a single character (buffer is that character N
    // times over) cycle through matches one at a time rather than requiring
    // an exact N-character-prefix match.
    const isRepeatedSingleChar =
      this.typeaheadBuffer.length > 1 &&
      this.typeaheadBuffer.split("").every((c) => c === this.typeaheadBuffer[0]);
    const query = isRepeatedSingleChar ? this.typeaheadBuffer[0] : this.typeaheadBuffer;
    // A single-character query always searches starting just *after* the
    // current item, even on the very first keystroke of a fresh sequence -
    // otherwise, when the currently active item already starts with that
    // letter, the search would match it immediately and appear to do
    // nothing, while a moment later the "repeated" cycling path (which does
    // offset) would correctly advance - the same keystroke behaving
    // differently depending on where focus happened to already be. A
    // multi-character query (e.g. "er") is a refinement of an
    // already-in-progress search and searches from the current position, so
    // it can keep matching the same item if it still fits the longer prefix.
    const searchOffset = query.length === 1 ? 1 : 0;

    const n = visible.length;
    for (let i = 1; i <= n; i++) {
      const candidate = visible[(index + searchOffset + i - 1 + n) % n];
      const candidateId = candidate.dataset.nodeId;
      const candidateNode = candidateId ? this.nodesById.get(candidateId) : undefined;
      if (candidateNode && candidateNode.name.toLowerCase().startsWith(query)) {
        this.setActive(candidateNode.id);
        return;
      }
    }
  }

  /** Marks a node to be moved; a later call to completeMove()/pasteIntoSelection() relocates it. */
  markForMove(nodeId: string): void {
    const node = this.nodesById.get(nodeId);
    if (!node) return;
    this.markedForMoveId = nodeId;
    this.rerenderInPlace();
    this.callbacks.announce(`${node.name} marked to move. Select a destination and choose "Paste here", or press Escape to cancel.`);
  }

  cancelMove(): void {
    if (!this.markedForMoveId) return;
    const cancelledName = this.nodesById.get(this.markedForMoveId)?.name ?? "item";
    this.markedForMoveId = null;
    this.rerenderInPlace();
    this.callbacks.announce(`Move cancelled for ${cancelledName}.`);
  }

  /** Completes a pending move using the currently active/selected item as the destination. */
  pasteIntoSelection(): void {
    if (!this.activeId) return;
    this.completeMove(this.activeId);
  }

  renameActiveItem(): void {
    if (!this.activeId) return;
    const node = this.nodesById.get(this.activeId);
    if (node) this.openRenameDialog(node);
  }

  getActiveNode(): NodeOut | null {
    return this.activeId ? (this.nodesById.get(this.activeId) ?? null) : null;
  }

  /** Moves DOM focus to the current roving-tabindex item (or the first visible item if none is active yet/visible). */
  focusTree(): void {
    this.resolveVisibleTarget(this.activeId)?.focus();
  }

  getMarkedForMoveNode(): NodeOut | null {
    return this.markedForMoveId ? (this.nodesById.get(this.markedForMoveId) ?? null) : null;
  }

  private completeMove(destinationNodeId: string): void {
    const markedId = this.markedForMoveId;
    if (!markedId) return;

    const markedNode = this.nodesById.get(markedId);
    const destinationNode = this.nodesById.get(destinationNodeId);
    if (!markedNode || !destinationNode) return;

    // A collapsed folder is a paste target at its own level (sibling of the
    // folder), matching how it looks/behaves as a single line in the tree;
    // only an *expanded* folder (visibly showing its contents) is a target
    // to paste inside. Without this, a collapsed folder is indistinguishable
    // from "paste inside" and un-expandable destinations (e.g. a lone
    // root-level Trash folder) become impossible to paste next to.
    const destinationFolderId =
      destinationNode.kind === "folder" && this.expanded.has(destinationNode.id)
        ? destinationNode.id
        : destinationNode.parent_id;

    if (markedId === destinationFolderId) {
      this.callbacks.announce(`${markedNode.name} is already in that folder.`);
      return;
    }
    if (destinationFolderId !== null && this.isDescendantOf(destinationFolderId, markedId)) {
      this.callbacks.announce(`Cannot move ${markedNode.name} into its own subfolder.`);
      return;
    }

    this.markedForMoveId = null;
    this.callbacks.onMoveNode(markedId, destinationFolderId);
  }

  private isDescendantOf(candidateId: string, ancestorId: string): boolean {
    let current = this.nodesById.get(candidateId);
    while (current?.parent_id) {
      if (current.parent_id === ancestorId) return true;
      current = this.nodesById.get(current.parent_id);
    }
    return false;
  }

  private rerenderInPlace(): void {
    const flat = Array.from(this.nodesById.values());
    this.render(flat, this.activeId);
  }

  private openRenameDialog(node: NodeOut): void {
    const dialog = document.createElement("dialog");
    dialog.className = "rename-dialog";

    const form = document.createElement("form");
    form.method = "dialog";

    const heading = document.createElement("h2");
    heading.id = "rename-dialog-heading";
    heading.textContent = `Rename "${node.name}"`;
    form.appendChild(heading);

    const label = document.createElement("label");
    label.htmlFor = "rename-dialog-input";
    label.textContent = "New name";
    form.appendChild(label);

    const input = document.createElement("input");
    input.type = "text";
    input.id = "rename-dialog-input";
    input.value = node.name;
    input.required = true;
    form.appendChild(input);

    const errorEl = document.createElement("p");
    errorEl.className = "rename-dialog-error";
    errorEl.setAttribute("role", "alert");
    form.appendChild(errorEl);

    const buttonRow = document.createElement("div");
    buttonRow.className = "rename-dialog-buttons";
    const saveBtn = document.createElement("button");
    saveBtn.type = "submit";
    saveBtn.textContent = "Rename";
    const cancelBtn = document.createElement("button");
    cancelBtn.type = "button";
    cancelBtn.textContent = "Cancel";
    buttonRow.appendChild(saveBtn);
    buttonRow.appendChild(cancelBtn);
    form.appendChild(buttonRow);

    dialog.setAttribute("aria-labelledby", "rename-dialog-heading");
    dialog.appendChild(form);
    document.body.appendChild(dialog);

    const closeDialog = () => {
      dialog.close();
      dialog.remove();
      this.setActive(node.id, true);
    };

    cancelBtn.addEventListener("click", closeDialog);
    dialog.addEventListener("cancel", closeDialog);

    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const newName = input.value;
      const error = validateNodeName(newName);
      if (error) {
        errorEl.textContent = error;
        input.focus();
        return;
      }
      const trimmed = newName.trim().replace(/^\.+|\.+$/g, "");
      dialog.close();
      dialog.remove();
      if (trimmed !== node.name) {
        this.callbacks.onRenameNode(node.id, trimmed);
      } else {
        this.setActive(node.id, true);
      }
    });

    dialog.showModal();
    input.focus();
    input.select();
  }
}
