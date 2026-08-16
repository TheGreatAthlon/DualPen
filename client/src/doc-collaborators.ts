import type { Awareness } from "y-protocols/awareness";
import { listPeersOnLine } from "./jump-to-collaborator";

/**
 * Renders "Editing with: ..." into the given element, but only announces
 * peers on the local user's current line, and only when notifyCursorMoved()
 * is called for a vertical (Up/Down arrow) move - not on every awareness
 * change, which would otherwise fire (and re-announce via role="status") for
 * every keystroke and every remote peer's own cursor movement. See
 * bindVerticalArrowTracking() in main.ts for how vertical moves are
 * detected.
 */
export interface DocCollaboratorsList {
  notifyCursorMoved: (localLineNumber: number, wasVertical: boolean) => void;
  detach: () => void;
}

export function attachDocCollaboratorsList(
  awareness: Awareness,
  el: HTMLElement,
): DocCollaboratorsList {
  function notifyCursorMoved(localLineNumber: number, wasVertical: boolean): void {
    if (!wasVertical) return;
    const peers = listPeersOnLine(awareness, localLineNumber);
    el.textContent = peers.length === 0 ? "" : `Editing with: ${peers.map((p) => p.name).join(", ")}`;
  }

  return {
    notifyCursorMoved,
    detach: () => {
      el.textContent = "";
    },
  };
}
