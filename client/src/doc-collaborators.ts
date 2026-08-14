import type { Awareness } from "y-protocols/awareness";
import { listJumpablePeers } from "./jump-to-collaborator";

/**
 * Renders "Editing with: ..." into the given element from awareness state,
 * re-rendering on every awareness change. Mirrors attachPresenceSounds()'s
 * shape (attach on doc open, call the returned detach function in
 * teardownSync()) but drives a visible/screen-reader-readable list instead
 * of audio.
 */
export function attachDocCollaboratorsList(awareness: Awareness, el: HTMLElement): () => void {
  function render(): void {
    const peers = listJumpablePeers(awareness);
    el.textContent =
      peers.length === 0 ? "" : `Editing with: ${peers.map((p) => p.name).join(", ")}`;
  }

  const onChange = () => render();
  awareness.on("change", onChange);
  render();

  return () => {
    awareness.off("change", onChange);
    el.textContent = "";
  };
}
