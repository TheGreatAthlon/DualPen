import * as encoding from "lib0/encoding";
import * as decoding from "lib0/decoding";
import * as Y from "yjs";
import * as syncProtocol from "y-protocols/sync";
import { Awareness, applyAwarenessUpdate, encodeAwarenessUpdate } from "y-protocols/awareness";

const MESSAGE_SYNC = 0;
const MESSAGE_AWARENESS = 1;
// Not part of the Yjs sync/awareness protocols - our own addition, carrying
// a JSON payload (not lib0-framed) rather than CRDT state. Mirrors
// MESSAGE_TYPE_CHAT in server/app/routers/sync.py; must stay in sync with it.
const MESSAGE_CHAT = 2;

// Same-origin by default, mirroring api.ts's API_BASE - derives ws:// or
// wss:// from the page's own protocol so it works automatically behind a
// TLS-terminating reverse proxy. Override at build time with VITE_WS_BASE
// for a split-origin deployment.
const WS_BASE =
  import.meta.env.VITE_WS_BASE ??
  `${window.location.protocol === "https:" ? "wss:" : "ws:"}//${window.location.host}`;

// Mirrors the backend's custom close codes in server/app/routers/sync.py.
export const CLOSE_UNAUTHORIZED = 4401;
export const CLOSE_NOT_FOUND = 4404;
export const CLOSE_REPLACED_BY_NEWER_SESSION = 4409;

export interface ChatMessage {
  id: string;
  docId: string;
  userId: number;
  displayName: string;
  body: string;
  sentAt: string;
}

export class DocSyncConnection {
  readonly ydoc: Y.Doc;
  readonly ytext: Y.Text;
  readonly awareness: Awareness;

  private ws: WebSocket | null = null;
  private closedByCaller = false;

  private readonly onSynced: () => void;
  private readonly onError: (closeCode: number | null) => void;
  private readonly onChatMessage: (message: ChatMessage) => void;

  private synced = false;

  constructor(
    docId: string,
    onSynced: () => void,
    onError: (closeCode: number | null) => void,
    onChatMessage: (message: ChatMessage) => void = () => {},
  ) {
    this.ydoc = new Y.Doc();
    this.ytext = this.ydoc.getText("content");
    this.awareness = new Awareness(this.ydoc);
    this.onSynced = onSynced;
    this.onError = onError;
    this.onChatMessage = onChatMessage;

    this.ydoc.on("update", (update: Uint8Array, origin: unknown) => {
      if (origin === this) return;
      this.sendUpdate(update);
    });

    this.awareness.on(
      "update",
      (
        { added, updated, removed }: { added: number[]; updated: number[]; removed: number[] },
        origin: unknown,
      ) => {
        if (origin === this) return;
        this.sendAwarenessUpdate(added.concat(updated, removed));
      },
    );

    this.connect(docId);
  }

  private connect(docId: string): void {
    const ws = new WebSocket(`${WS_BASE}/ws/doc/${docId}`);
    ws.binaryType = "arraybuffer";
    this.ws = ws;

    ws.addEventListener("open", () => {
      const encoder = encoding.createEncoder();
      encoding.writeVarUint(encoder, MESSAGE_SYNC);
      syncProtocol.writeSyncStep1(encoder, this.ydoc);
      ws.send(encoding.toUint8Array(encoder));
    });

    ws.addEventListener("message", (event) => {
      const data = new Uint8Array(event.data as ArrayBuffer);
      this.handleMessage(data);
    });

    ws.addEventListener("close", (event) => {
      if (!this.closedByCaller) {
        this.onError(event.code);
      }
    });

    ws.addEventListener("error", () => {
      this.onError(null);
    });
  }

  private handleMessage(data: Uint8Array): void {
    if (data.length > 0 && data[0] === MESSAGE_CHAT) {
      this.handleChatFrame(data);
      return;
    }

    const decoder = decoding.createDecoder(data);
    const messageType = decoding.readVarUint(decoder);

    if (messageType === MESSAGE_SYNC) {
      const encoder = encoding.createEncoder();
      encoding.writeVarUint(encoder, MESSAGE_SYNC);
      // readSyncMessage returns the inner sync sub-type it just processed:
      // messageYjsSyncStep1 (server asking for our state - we just replied
      // with our own step2), messageYjsSyncStep2 (server's reply to *our*
      // step1, carrying its real content), or messageYjsUpdate. Only step2
      // (or a later update) means content has actually arrived - firing
      // onSynced() on the bare step1 handshake-opener would bind Monaco to
      // an empty Y.Text before the server's real content ever shows up.
      const innerType = syncProtocol.readSyncMessage(decoder, encoder, this.ydoc, this);
      if (encoding.length(encoder) > 1 && this.ws) {
        this.ws.send(encoding.toUint8Array(encoder));
      }
      if (!this.synced && innerType === syncProtocol.messageYjsSyncStep2) {
        this.synced = true;
        this.onSynced();
      }
    } else if (messageType === MESSAGE_AWARENESS) {
      const update = decoding.readVarUint8Array(decoder);
      applyAwarenessUpdate(this.awareness, update, this);
    }
  }

  private handleChatFrame(data: Uint8Array): void {
    try {
      const json = new TextDecoder().decode(data.subarray(1));
      const message = JSON.parse(json) as ChatMessage;
      this.onChatMessage(message);
    } catch {
      // Malformed frame from a future/older server version - ignore rather
      // than crash the whole sync connection over a non-critical feature.
    }
  }

  sendChatMessage(body: string): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    const json = new TextEncoder().encode(JSON.stringify({ body }));
    const frame = new Uint8Array(1 + json.length);
    frame[0] = MESSAGE_CHAT;
    frame.set(json, 1);
    this.ws.send(frame);
  }

  private sendUpdate(update: Uint8Array): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    const encoder = encoding.createEncoder();
    encoding.writeVarUint(encoder, MESSAGE_SYNC);
    syncProtocol.writeUpdate(encoder, update);
    this.ws.send(encoding.toUint8Array(encoder));
  }

  private sendAwarenessUpdate(changedClients: number[]): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    const encoder = encoding.createEncoder();
    encoding.writeVarUint(encoder, MESSAGE_AWARENESS);
    encoding.writeVarUint8Array(encoder, encodeAwarenessUpdate(this.awareness, changedClients));
    this.ws.send(encoding.toUint8Array(encoder));
  }

  destroy(): void {
    this.closedByCaller = true;
    // Awareness.destroy() sets local state to null, which fires the
    // "update" listener above and broadcasts a proper disconnect to any
    // peers before we tear down the socket.
    this.awareness.destroy();
    this.ws?.close();
    this.ws = null;
    this.ydoc.destroy();
  }
}
