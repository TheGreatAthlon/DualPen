import type { Awareness } from "y-protocols/awareness";

interface RemoteAwarenessState {
  user?: { id: number; name: string };
  cursor?: { lineNumber: number; column: number; isTyping?: boolean };
}

export interface CollaboratorPeer {
  clientId: number;
  name: string;
  lineNumber: number;
  column: number;
}

/**
 * Deterministically-sorted (by clientID) list of other peers who currently
 * have a known cursor position, excluding ourselves. Stable ordering is what
 * makes repeated Alt+J presses cycle predictably rather than jumping around
 * as unrelated awareness fields change.
 */
export function listJumpablePeers(awareness: Awareness): CollaboratorPeer[] {
  const localClientId = awareness.clientID;
  const states = awareness.getStates() as Map<number, RemoteAwarenessState>;
  const peers: CollaboratorPeer[] = [];

  for (const [clientId, state] of states) {
    if (clientId === localClientId) continue;
    if (!state.cursor || !state.user) continue;
    peers.push({
      clientId,
      name: state.user.name,
      lineNumber: state.cursor.lineNumber,
      column: state.cursor.column,
    });
  }

  peers.sort((a, b) => a.clientId - b.clientId);
  return peers;
}

/**
 * Same as listJumpablePeers, but filtered to peers whose cursor is on the
 * given line. Mirrors presence-sounds.ts's deriveState() same-line check
 * (exact line-number equality, no tolerance band).
 */
export function listPeersOnLine(awareness: Awareness, lineNumber: number): CollaboratorPeer[] {
  return listJumpablePeers(awareness).filter((p) => p.lineNumber === lineNumber);
}

/**
 * Tracks the cycle position for repeated Alt+J presses within one document
 * session. A fresh instance per openDocument() call (like openGeneration)
 * means switching documents naturally resets the cycle.
 */
export class CollaboratorCycler {
  private lastPeerIds: number[] = [];
  private index = 0;

  next(awareness: Awareness): CollaboratorPeer | null {
    const peers = listJumpablePeers(awareness);
    if (peers.length === 0) {
      this.lastPeerIds = [];
      this.index = 0;
      return null;
    }

    const peerIds = peers.map((p) => p.clientId);
    const samePeerSet =
      peerIds.length === this.lastPeerIds.length &&
      peerIds.every((id, i) => id === this.lastPeerIds[i]);

    if (!samePeerSet) {
      this.lastPeerIds = peerIds;
      this.index = 0;
    } else {
      this.index = (this.index + 1) % peers.length;
    }

    return peers[this.index];
  }
}
