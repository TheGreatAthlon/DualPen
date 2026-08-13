// Headless verification for Phase 6 (presence sounds + jump-to-collaborator).
//
// This exercises the same live server + hand-rolled wire protocol as
// verify-phase5.mjs, extended to prove out the parts Phase 6 actually added:
// the isTyping awareness field propagates between real clients, and the
// pure state-derivation / peer-ordering logic (mirrored here from
// presence-sounds.ts / jump-to-collaborator.ts, which are ES modules meant
// to run in a browser DOM/AudioContext, not Node) produces the right
// results for realistic awareness snapshots.
//
// What this can NOT verify: that the sounds are actually audible, correctly
// mixed, and glitch-free, or that Alt+J actually moves the Monaco cursor and
// announces via the live region - that needs a real browser with speakers
// and a person listening.

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

// Mirrors client/src/sync.ts's DocSyncConnection wire format.
function connect(cookie, docId) {
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

// --- Mirrors presence-sounds.ts's deriveState() ---
function deriveState(local, peer) {
  if (!peer?.cursor) return "silent";
  const sameLine = local?.cursor?.lineNumber === peer.cursor.lineNumber;
  const typing = peer.cursor.isTyping === true;
  if (sameLine && typing) return "same-line-typing";
  if (sameLine && !typing) return "same-line-idle";
  if (!sameLine && typing) return "elsewhere-typing";
  return "silent";
}

// --- Mirrors jump-to-collaborator.ts's listJumpablePeers() ordering ---
function listJumpablePeers(states, localClientId) {
  const peers = [];
  for (const [clientId, state] of states) {
    if (clientId === localClientId) continue;
    if (!state.cursor || !state.user) continue;
    peers.push({ clientId, name: state.user.name, lineNumber: state.cursor.lineNumber });
  }
  peers.sort((a, b) => a.clientId - b.clientId);
  return peers;
}

async function main() {
  console.log("=== Test 1: isTyping awareness field propagates between real clients ===");
  {
    const cookie = await login("verify_alice", "alicepass123");
    const docId = await createDocument(cookie, "presence.txt");

    const clientA = await connect(cookie, docId);
    const clientB = await connect(cookie, docId);

    clientA.awareness.setLocalStateField("user", { id: 1, name: "Alice" });
    clientA.awareness.setLocalStateField("cursor", { lineNumber: 5, column: 1, isTyping: true });
    await sleep(300);

    const bobSeesAlice = clientB.awareness.getStates().get(clientA.ydoc.clientID);
    ok(
      "peer sees isTyping:true on cursor field",
      !!bobSeesAlice?.cursor && bobSeesAlice.cursor.isTyping === true && bobSeesAlice.cursor.lineNumber === 5,
    );

    clientA.awareness.setLocalStateField("cursor", { lineNumber: 5, column: 1, isTyping: false });
    await sleep(300);
    const bobSeesAliceIdle = clientB.awareness.getStates().get(clientA.ydoc.clientID);
    ok("isTyping flips back to false and propagates", bobSeesAliceIdle.cursor.isTyping === false);

    clientA.ws.close();
    clientB.ws.close();
    await sleep(200);
  }

  console.log("\n=== Test 2: presence-sound state derivation (pure logic, mirrors presence-sounds.ts) ===");
  {
    const local = { cursor: { lineNumber: 10, column: 1 } };
    ok(
      "same line + typing -> same-line-typing",
      deriveState(local, { cursor: { lineNumber: 10, isTyping: true } }) === "same-line-typing",
    );
    ok(
      "same line + idle -> same-line-idle",
      deriveState(local, { cursor: { lineNumber: 10, isTyping: false } }) === "same-line-idle",
    );
    ok(
      "different line + typing -> elsewhere-typing",
      deriveState(local, { cursor: { lineNumber: 99, isTyping: true } }) === "elsewhere-typing",
    );
    ok(
      "different line + idle -> silent",
      deriveState(local, { cursor: { lineNumber: 99, isTyping: false } }) === "silent",
    );
    ok("no peer cursor yet -> silent", deriveState(local, {}) === "silent");
  }

  console.log("\n=== Test 3: peer join/leave detection (mirrors presence-sounds.ts user-id + debounce logic) ===");
  {
    // Mirrors attachPresenceSounds()'s render(): tracked by user id (not
    // clientID, since a reconnect mints a fresh clientID for the same
    // person), first pass establishes a baseline (no chimes), and a "left"
    // chime is held for LEAVE_DEBOUNCE_MS so a same-user reappearance in
    // that window (a reconnect, or y-protocols' own ~15s idle-peer
    // drop/keepalive cycle) cancels it instead of playing left+joined.
    const LEAVE_DEBOUNCE_MS = 16000;

    function simulate(snapshots) {
      // snapshots: [{ tMs, states: Map<clientId, {user?: {id}}> }, ...]
      let knownUserIds = new Set();
      const pendingLeaves = new Map(); // userId -> fireAtMs
      let baselineEstablished = false;
      const events = [];

      function flushDueTimers(nowMs) {
        for (const [userId, fireAtMs] of [...pendingLeaves]) {
          if (nowMs >= fireAtMs) {
            pendingLeaves.delete(userId);
            events.push(`left:${userId}@${nowMs}`);
          }
        }
      }

      for (const { tMs, states } of snapshots) {
        flushDueTimers(tMs);

        const currentUserIds = new Set([...states.values()].filter((s) => s.user).map((s) => s.user.id));
        const isFirstRender = !baselineEstablished;
        baselineEstablished = true;

        if (!isFirstRender) {
          for (const userId of knownUserIds) {
            if (currentUserIds.has(userId) || pendingLeaves.has(userId)) continue;
            pendingLeaves.set(userId, tMs + LEAVE_DEBOUNCE_MS);
          }
          for (const userId of currentUserIds) {
            if (pendingLeaves.has(userId)) {
              pendingLeaves.delete(userId);
              continue;
            }
            if (!knownUserIds.has(userId)) events.push(`joined:${userId}@${tMs}`);
          }
        }
        knownUserIds = currentUserIds;
      }
      return events;
    }

    const u = (id) => ({ user: { id } });

    // Case A: genuine join then genuine leave (no reconnect involved).
    const eventsA = simulate([
      { tMs: 0, states: new Map([[10, u(1)]]) }, // baseline: user 1 already present
      { tMs: 100, states: new Map([[10, u(1)], [11, u(2)]]) }, // user 2 joins
      { tMs: 200, states: new Map([[11, u(2)]]) }, // user 1's clientID disappears
      { tMs: 200 + LEAVE_DEBOUNCE_MS + 1000, states: new Map([[11, u(2)]]) }, // still gone well past the debounce
    ]);
    ok(
      "genuine join fires exactly one joined event, baseline peer doesn't",
      eventsA.filter((e) => e.startsWith("joined:2@")).length === 1 && !eventsA.some((e) => e.startsWith("joined:1@")),
    );
    ok(
      "genuine leave (never comes back) fires exactly one left event after the debounce",
      eventsA.filter((e) => e.startsWith("left:1@")).length === 1,
    );

    // Case B: same user's clientID churns (reconnect / idle awareness
    // drop+keepalive) well within the debounce window - must net to silence.
    const eventsB = simulate([
      { tMs: 0, states: new Map([[20, u(1)], [21, u(2)]]) }, // baseline
      { tMs: 5000, states: new Map([[21, u(2)]]) }, // user 1's old clientID vanishes
      { tMs: 12000, states: new Map([[22, u(1)], [21, u(2)]]) }, // user 1 reappears under a new clientID, still inside the window
      { tMs: 40000, states: new Map([[22, u(1)], [21, u(2)]]) }, // long after - nothing pending, no event should fire
    ]);
    ok("same-user reconnect within the debounce window fires no left or joined event", eventsB.length === 0);
  }

  console.log("\n=== Test 4: jump-to-collaborator peer ordering is deterministic ===");
  {
    const states = new Map([
      [5, { user: { name: "Carol" }, cursor: { lineNumber: 1 } }],
      [2, { user: { name: "Bob" }, cursor: { lineNumber: 2 } }],
      [9, { user: { name: "Dave" }, cursor: { lineNumber: 3 } }],
      [1, { user: { name: "Self" }, cursor: { lineNumber: 4 } }],
      [7, { cursor: { lineNumber: 5 } }], // no user field yet - excluded
    ]);
    const peers = listJumpablePeers(states, 1);
    ok(
      "excludes self and peers without a user field, sorted by clientID",
      peers.map((p) => p.clientId).join(",") === "2,5,9",
    );
  }

  console.log(`\n${failures === 0 ? "ALL PASSED" : `${failures} FAILURE(S)`}`);
  process.exit(failures === 0 ? 0 : 1);
}

main().catch((err) => {
  console.error("Verification script crashed:", err);
  process.exit(1);
});
