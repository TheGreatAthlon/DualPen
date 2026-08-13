import type { Awareness } from "y-protocols/awareness";

const STYLE_ELEMENT_ID = "collab-editor-remote-cursor-styles";

// Small fixed palette rather than an arbitrary HSL rotation so colors stay
// readable (sufficient contrast, not too close to the editor background) in
// both light and dark themes rather than landing on an unlucky hue.
const PALETTE = [
  "#e06c75",
  "#61afef",
  "#98c379",
  "#d19a66",
  "#c678dd",
  "#56b6c2",
  "#e5c07b",
  "#be5046",
];

function colorForClientId(clientId: number): string {
  return PALETTE[Math.abs(clientId) % PALETTE.length];
}

function ruleForClient(clientId: number, color: string): string {
  return [
    `.yRemoteSelection-${clientId} { background-color: ${color}33; }`,
    `.yRemoteSelectionHead-${clientId} { position: relative; border-left: 2px solid ${color}; }`,
    `.yRemoteSelectionHead-${clientId}::after { content: ""; position: absolute; top: -2px; left: -4px; width: 8px; height: 8px; border-radius: 50%; background-color: ${color}; }`,
  ].join("\n");
}

function getOrCreateStyleElement(): HTMLStyleElement {
  let el = document.getElementById(STYLE_ELEMENT_ID) as HTMLStyleElement | null;
  if (!el) {
    el = document.createElement("style");
    el.id = STYLE_ELEMENT_ID;
    document.head.appendChild(el);
  }
  return el;
}

/**
 * y-monaco generates per-clientID CSS class names for remote selections
 * (yRemoteSelection-<id>, yRemoteSelectionHead-<id>) but injects no actual
 * color rules for them. This keeps a <style> element in sync with the
 * awareness peer list so remote cursors/selections are actually visible,
 * and removes rules for peers who disconnect so the element doesn't grow
 * unbounded over a long session.
 */
export function attachRemoteCursorStyles(awareness: Awareness): () => void {
  const styleEl = getOrCreateStyleElement();
  const localClientId = awareness.clientID;

  const render = () => {
    const rules: string[] = [];
    awareness.getStates().forEach((_state, clientId) => {
      if (clientId === localClientId) return;
      rules.push(ruleForClient(clientId, colorForClientId(clientId)));
    });
    styleEl.textContent = rules.join("\n");
  };

  render();
  awareness.on("change", render);

  return () => {
    awareness.off("change", render);
  };
}
