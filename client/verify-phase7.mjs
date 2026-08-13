// Headless verification for Phase 7 (chat).
//
// Mirrors verify-phase5.mjs/verify-phase6.mjs's live-server pattern, but
// drives the exact wire format client/src/sync.ts's DocSyncConnection uses
// for chat frames (raw 0x02 byte + JSON, not lib0 varuint framing) so this
// proves the real client-compatible protocol works end to end against a
// live server - not just what the Python-side pytest suite exercises.
//
// What this can NOT verify: the chat panel/quick-composer DOM, F2/Shift+F2
// keyboard handling, dialog focus trapping, or the chat_message_notify
// sound actually playing - those need a real browser.

import WebSocket from "ws";
import * as Y from "yjs";
import * as syncProtocol from "y-protocols/sync";
import { Awareness } from "y-protocols/awareness";
import * as encoding from "lib0/encoding";
import * as decoding from "lib0/decoding";

const HTTP_BASE = "http://127.0.0.1:8799";
const WS_BASE = "ws://127.0.0.1:8799";
const MESSAGE_SYNC = 0;
const MESSAGE_AWARENESS = 1;
const MESSAGE_CHAT = 2;

let failures = 0;

function ok(label, cond) {
  if (cond) {
    console.log(`PASS: ${label}`);
  } else {
    console.error(`FAIL: ${label}`);
    failures += 1;
  }
}

async function login(username, password) {
  const resp = await fetch(`${HTTP_BASE}/api/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!resp.ok) throw new Error(`login failed: ${resp.status}`);
  const setCookie = resp.headers.get("set-cookie");
  const match = /session_token=([^;]+)/.exec(setCookie ?? "");
  if (!match) throw new Error("no session_token cookie in login response");
  return match[1];
}

async function createDocument(cookie, name) {
  const resp = await fetch(`${HTTP_BASE}/api/documents`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Cookie: `session_token=${cookie}` },
    body: JSON.stringify({ name, parent_id: null }),
  });
  if (!resp.ok) throw new Error(`create doc failed: ${resp.status}`);
  const body = await resp.json();
  return body.id;
}

async function getChatHistory(cookie, docId) {
  const resp = await fetch(`${HTTP_BASE}/api/documents/${docId}/chat`, {
    headers: { Cookie: `session_token=${cookie}` },
  });
  if (!resp.ok) throw new Error(`get chat history failed: ${resp.status}`);
  return resp.json();
}

// Mirrors client/src/sync.ts's DocSyncConnection wire format, including the
// raw (non-lib0-framed) 0x02 chat frame encoding.
function connect(cookie, docId) {
  return new Promise((resolve, reject) => {
    const ydoc = new Y.Doc();
    const ytext = ydoc.getText("content");
    const awareness = new Awareness(ydoc);
    let synced = false;
    let resolved = false;
    const chatMessages = [];
    const chatListeners = [];

    const ws = new WebSocket(`${WS_BASE}/ws/doc/${docId}`, {
      headers: { Cookie: `session_token=${cookie}` },
    });
    ws.binaryType = "arraybuffer";
    const conn = { ws, ydoc, ytext, awareness, chatMessages };

    conn.onChatMessage = (fn) => chatListeners.push(fn);
    conn.sendChatMessage = (body) => {
      const json = Buffer.from(JSON.stringify({ body }), "utf-8");
      const frame = Buffer.concat([Buffer.from([MESSAGE_CHAT]), json]);
      ws.send(frame);
    };

    ws.on("open", () => {
      const encoder = encoding.createEncoder();
      encoding.writeVarUint(encoder, MESSAGE_SYNC);
      syncProtocol.writeSyncStep1(encoder, ydoc);
      ws.send(encoding.toUint8Array(encoder));
    });

    ws.on("message", (data) => {
      const bytes = new Uint8Array(data);
      if (bytes.length > 0 && bytes[0] === MESSAGE_CHAT) {
        const message = JSON.parse(Buffer.from(bytes.subarray(1)).toString("utf-8"));
        chatMessages.push(message);
        for (const fn of chatListeners) fn(message);
        return;
      }

      const decoder = decoding.createDecoder(bytes);
      const messageType = decoding.readVarUint(decoder);
      if (messageType === MESSAGE_SYNC) {
        const encoder = encoding.createEncoder();
        encoding.writeVarUint(encoder, MESSAGE_SYNC);
        const innerType = syncProtocol.readSyncMessage(decoder, encoder, ydoc, conn);
        if (encoding.length(encoder) > 1) ws.send(encoding.toUint8Array(encoder));
        if (!synced && innerType === syncProtocol.messageYjsSyncStep2) {
          synced = true;
          if (!resolved) {
            resolved = true;
            resolve(conn);
          }
        }
      } else if (messageType === MESSAGE_AWARENESS) {
        // Not needed for these tests - awareness state itself isn't asserted on.
      }
    });

    ws.on("close", (code) => {
      if (!resolved) {
        resolved = true;
        reject(new Error(`closed before sync (code ${code})`));
      }
    });
    ws.on("error", (err) => {
      if (!resolved) {
        resolved = true;
        reject(err);
      }
    });
  });
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function waitForChatMessage(conn, predicate, timeoutMs = 5000) {
  return new Promise((resolve, reject) => {
    const existing = conn.chatMessages.find(predicate);
    if (existing) {
      resolve(existing);
      return;
    }
    const timer = setTimeout(() => reject(new Error("timed out waiting for chat message")), timeoutMs);
    conn.onChatMessage((message) => {
      if (predicate(message)) {
        clearTimeout(timer);
        resolve(message);
      }
    });
  });
}

async function main() {
  console.log("=== Test 1: chat message broadcasts with server-stamped identity, sender included ===");
  {
    const cookieA = await login("verify_alice", "alicepass123");
    const cookieB = await login("verify_bob", "bobpass123");
    const docId = await createDocument(cookieA, "chat-verify.txt");

    const connA = await connect(cookieA, docId);
    const connB = await connect(cookieB, docId);

    // Only {body} goes over the wire - no identity claimed by the client.
    connA.sendChatMessage("hello from alice, via verify script");

    const receivedByB = await waitForChatMessage(connB, (m) => m.body.startsWith("hello from alice"));
    ok("recipient sees the message with server-stamped displayName", receivedByB.displayName === "Verify Alice");
    ok("recipient sees the correct docId", receivedByB.docId === docId);

    const echoedToA = await waitForChatMessage(connA, (m) => m.body.startsWith("hello from alice"));
    ok("sender is echoed their own message with the same id", echoedToA.id === receivedByB.id);

    connA.ws.close();
    connB.ws.close();
    await sleep(300);

    const history = await getChatHistory(cookieA, docId);
    ok("message is persisted and retrievable via REST history", history.some((m) => m.id === receivedByB.id));
  }

  console.log("\n=== Test 2: malformed chat frame doesn't break the connection ===");
  {
    const cookieA = await login("verify_alice", "alicepass123");
    const cookieB = await login("verify_bob", "bobpass123");
    const docId = await createDocument(cookieA, "chat-malformed.txt");

    const connA = await connect(cookieA, docId);
    const connB = await connect(cookieB, docId);

    connA.ws.send(Buffer.concat([Buffer.from([MESSAGE_CHAT]), Buffer.from("not json")]));
    connA.sendChatMessage("still works after malformed frame");

    const received = await waitForChatMessage(connB, (m) => m.body === "still works after malformed frame");
    ok("connection survives a malformed chat frame and later messages still deliver", !!received);

    connA.ws.close();
    connB.ws.close();
  }

  console.log(`\n${failures === 0 ? "ALL PASSED" : `${failures} FAILURE(S)`}`);
  process.exit(failures === 0 ? 0 : 1);
}

main().catch((err) => {
  console.error("Verification script crashed:", err);
  process.exit(1);
});
