import WebSocket from "ws";
import * as Y from "yjs";
import * as syncProtocol from "y-protocols/sync";
import { Awareness, encodeAwarenessUpdate, applyAwarenessUpdate } from "y-protocols/awareness";
import * as encoding from "lib0/encoding";
import * as decoding from "lib0/decoding";

const HTTP_BASE = "http://127.0.0.1:8799";
const WS_BASE = "ws://127.0.0.1:8799";
const MESSAGE_SYNC = 0;
const MESSAGE_AWARENESS = 1;
const CLOSE_REPLACED_BY_NEWER_SESSION = 4409;

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

async function getDocumentContent(cookie, docId) {
  const resp = await fetch(`${HTTP_BASE}/api/documents/${docId}/content`, {
    headers: { Cookie: `session_token=${cookie}` },
  });
  if (!resp.ok) throw new Error(`get content failed: ${resp.status}`);
  const body = await resp.json();
  return body.content;
}

// Mirrors client/src/sync.ts's DocSyncConnection hand-rolled protocol, since
// this is meant to exercise the same wire format the real browser frontend
// uses (rather than pycrdt's Python-side helpers already covered by pytest).
function connect(cookie, docId, { onClose } = {}) {
  return new Promise((resolve, reject) => {
    const ydoc = new Y.Doc();
    const ytext = ydoc.getText("content");
    const awareness = new Awareness(ydoc);
    let synced = false;
    let resolved = false;

    const ws = new WebSocket(`${WS_BASE}/ws/doc/${docId}`, {
      headers: { Cookie: `session_token=${cookie}` },
    });
    ws.binaryType = "arraybuffer";

    const conn = { ws, ydoc, ytext, awareness };

    ydoc.on("update", (update, origin) => {
      if (origin === conn) return;
      const encoder = encoding.createEncoder();
      encoding.writeVarUint(encoder, MESSAGE_SYNC);
      syncProtocol.writeUpdate(encoder, update);
      if (ws.readyState === WebSocket.OPEN) ws.send(encoding.toUint8Array(encoder));
    });

    awareness.on("update", ({ added, updated, removed }, origin) => {
      if (origin === conn) return;
      const changed = added.concat(updated, removed);
      const encoder = encoding.createEncoder();
      encoding.writeVarUint(encoder, MESSAGE_AWARENESS);
      encoding.writeVarUint8Array(encoder, encodeAwarenessUpdate(awareness, changed));
      if (ws.readyState === WebSocket.OPEN) ws.send(encoding.toUint8Array(encoder));
    });

    ws.on("open", () => {
      const encoder = encoding.createEncoder();
      encoding.writeVarUint(encoder, MESSAGE_SYNC);
      syncProtocol.writeSyncStep1(encoder, ydoc);
      ws.send(encoding.toUint8Array(encoder));
    });

    ws.on("message", (data) => {
      const bytes = new Uint8Array(data);
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
        const update = decoding.readVarUint8Array(decoder);
        applyAwarenessUpdate(awareness, update, conn);
      }
    });

    ws.on("close", (code) => {
      if (onClose) onClose(code);
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

function waitForClose(ws) {
  return new Promise((resolve) => {
    ws.on("close", (code) => resolve(code));
  });
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

async function main() {
  console.log("=== Test 1: two clients on the SAME doc converge ===");
  {
    const cookie = await login("verify_alice", "alicepass123");
    const docId = await createDocument(cookie, "convergence.txt");

    const clientA = await connect(cookie, docId);
    const clientB = await connect(cookie, docId);

    clientA.ytext.insert(0, "hello from A");
    await sleep(300);
    clientB.ytext.insert(clientB.ytext.length, " and B");
    await sleep(500);

    ok(
      "both clients converge to the same text",
      clientA.ytext.toString() === clientB.ytext.toString() &&
        clientA.ytext.toString() === "hello from A and B",
    );

    clientA.awareness.setLocalStateField("user", { id: 1, name: "Alice" });
    await sleep(300);
    const bobSeesAliceState = clientB.awareness.getStates().get(clientA.ydoc.clientID);
    ok(
      "awareness state (user field) propagates to the other client",
      !!bobSeesAliceState && bobSeesAliceState.user && bobSeesAliceState.user.name === "Alice",
    );

    clientA.ws.close();
    clientB.ws.close();
    await sleep(300);

    const persisted = await getDocumentContent(cookie, docId);
    ok("converged content persisted to disk", persisted === "hello from A and B");
  }

  console.log("\n=== Test 2: second doc force-closes first connection (same user) ===");
  {
    const cookie = await login("verify_bob", "bobpass123");
    const docA = await createDocument(cookie, "bob-a.txt");
    const docB = await createDocument(cookie, "bob-b.txt");

    let closeCode = null;
    const closePromise = new Promise((resolve) => {
      connect(cookie, docA, { onClose: (code) => resolve(code) }).then((conn) => {
        conn.ytext.insert(0, "edit before replacement");
      });
    });

    await sleep(400);
    const connB = await connect(cookie, docB);
    closeCode = await closePromise;

    ok(
      `first connection force-closed with code ${CLOSE_REPLACED_BY_NEWER_SESSION}`,
      closeCode === CLOSE_REPLACED_BY_NEWER_SESSION,
    );

    connB.ws.close();
    await sleep(400);

    const persisted = await getDocumentContent(cookie, docA);
    ok(
      "content edited before force-close was persisted",
      persisted === "edit before replacement",
    );
  }

  console.log("\n=== Test 3: same-doc reconnect does NOT force-close ===");
  {
    const cookie = await login("verify_alice", "alicepass123");
    const docId = await createDocument(cookie, "reconnect.txt");

    let gotClosed = false;
    const conn1 = await connect(cookie, docId, { onClose: () => (gotClosed = true) });
    const conn2 = await connect(cookie, docId);

    await sleep(500);
    ok("same-doc second connection does not force-close the first", !gotClosed);

    conn1.ws.close();
    conn2.ws.close();
    await sleep(200);
  }

  console.log(`\n${failures === 0 ? "ALL PASSED" : `${failures} FAILURE(S)`}`);
  process.exit(failures === 0 ? 0 : 1);
}

main().catch((err) => {
  console.error("Verification script crashed:", err);
  process.exit(1);
});
