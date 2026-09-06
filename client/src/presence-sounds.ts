import type { Awareness } from "y-protocols/awareness";

const VOLUME_STORAGE_KEY = "collab-editor:presenceSoundVolume";
const MUTED_STORAGE_KEY = "collab-editor:presenceSoundMuted";
const DEFAULT_VOLUME = 0.25;

// Minimum spacing between retriggers of the "elsewhere typing" one-shot per
// peer, so a peer typing continuously elsewhere doesn't turn into a rattle.
const ELSEWHERE_CLICK_MIN_INTERVAL_MS = 900;

// A user's awareness clientID changes on every reconnect (network blip, or
// them reopening the same document in their own tab), which briefly drops
// them out of the awareness state and back in under a new clientID. Holding
// the "left" chime for this long lets a same-user rejoin within the window
// cancel it outright instead of playing left+joined back to back.
//
// y-protocols' own Awareness also does this to genuinely idle (but still
// connected) peers as routine housekeeping: it removes anyone whose state
// hasn't been touched in outdatedTimeout (30s), then that peer's own client
// re-broadcasts its state on its next outdatedTimeout/2 (~15s) keepalive
// tick - so two people just sitting idle in a doc can drop and reappear on
// their own with nothing wrong. The debounce must comfortably outlast that
// up-to-~15s gap or this reads as a spurious leave+join.
const LEAVE_DEBOUNCE_MS = 16000;

// When you open a doc, the peers already in it arrive as awareness state
// shortly after (and sometimes across more than one frame) - not
// synchronously with attach. Anyone appearing within this window of attach is
// treated as "already here" (no join chime); only appearances after it count
// as real joins. Within the first ~1.5s you also can't meaningfully tell
// "was already here" from "joined right after me" - treating both as baseline
// is the behavior we want anyway.
const BASELINE_SETTLE_MS = 1500;

const SOUND_BASE = "/assets/sounds";

type PresenceState = "same-line-typing" | "same-line-idle" | "elsewhere-typing" | "silent";

interface RemoteAwarenessState {
  user?: { id: number; name: string };
  cursor?: { lineNumber: number; column: number; isTyping?: boolean };
}

interface PeerPlayback {
  state: PresenceState;
  loopSource: AudioBufferSourceNode | null;
  loopGain: GainNode | null;
  lastElsewhereClickAt: number;
}

let audioCtx: AudioContext | null = null;
let masterGain: GainNode | null = null;
let buffersPromise: Promise<Record<string, AudioBuffer>> | null = null;

function getAudioContext(): AudioContext {
  if (!audioCtx) {
    audioCtx = new AudioContext();
    masterGain = audioCtx.createGain();
    masterGain.gain.value = loadMuted() ? 0 : loadVolume();
    masterGain.connect(audioCtx.destination);
  }
  return audioCtx;
}

function loadVolume(): number {
  const raw = window.localStorage.getItem(VOLUME_STORAGE_KEY);
  const parsed = raw === null ? NaN : Number(raw);
  return Number.isFinite(parsed) ? Math.min(1, Math.max(0, parsed)) : DEFAULT_VOLUME;
}

function loadMuted(): boolean {
  return window.localStorage.getItem(MUTED_STORAGE_KEY) === "on";
}

export function setPresenceSoundVolume(volume: number): void {
  const clamped = Math.min(1, Math.max(0, volume));
  window.localStorage.setItem(VOLUME_STORAGE_KEY, String(clamped));
  if (masterGain && !loadMuted()) masterGain.gain.value = clamped;
}

export function setPresenceSoundMuted(muted: boolean): void {
  window.localStorage.setItem(MUTED_STORAGE_KEY, muted ? "on" : "off");
  if (masterGain) masterGain.gain.value = muted ? 0 : loadVolume();
}

export function getPresenceSoundVolume(): number {
  return loadVolume();
}

export function getPresenceSoundMuted(): boolean {
  return loadMuted();
}

function playBuffer(buffer: AudioBuffer): void {
  const ctx = getAudioContext();
  const source = ctx.createBufferSource();
  source.buffer = buffer;
  source.connect(masterGain!);
  source.start();
}

/**
 * Standalone chat notification sound, independent of attachPresenceSounds'
 * per-document lifecycle (that instance may not even be attached, e.g. if
 * called before a document's sync connection finishes opening) - reuses the
 * same lazily-created AudioContext/masterGain so it automatically respects
 * the same mute/volume settings, per the plan's "notification doesn't
 * require the panel to be open" requirement.
 */
export async function playChatNotifySound(): Promise<void> {
  const buffers = await loadBuffers();
  playBuffer(buffers.chatNotify);
}

async function loadBuffers(): Promise<Record<string, AudioBuffer>> {
  if (!buffersPromise) {
    const ctx = getAudioContext();
    const files = {
      sameLineIdle: "collab_same_line_idle_tick_loop.wav",
      sameLineTyping: "collab_same_line_typing_loop.wav",
      elsewhereTyping: "collab_elsewhere_typing_click.wav",
      peerJoined: "collab_peer_joined_doc.wav",
      peerLeft: "collab_peer_left_doc.wav",
      chatNotify: "chat_message_notify.wav",
    };
    buffersPromise = Promise.all(
      Object.entries(files).map(async ([key, filename]) => {
        const response = await fetch(`${SOUND_BASE}/${filename}`);
        const arrayBuffer = await response.arrayBuffer();
        const audioBuffer = await ctx.decodeAudioData(arrayBuffer);
        return [key, audioBuffer] as const;
      }),
    ).then((entries) => Object.fromEntries(entries));
  }
  return buffersPromise;
}

function deriveState(
  local: RemoteAwarenessState | undefined,
  peer: RemoteAwarenessState | undefined,
): PresenceState {
  if (!peer?.cursor) return "silent";
  const sameLine = local?.cursor?.lineNumber === peer.cursor.lineNumber;
  const typing = peer.cursor.isTyping === true;
  if (sameLine && typing) return "same-line-typing";
  if (sameLine && !typing) return "same-line-idle";
  if (!sameLine && typing) return "elsewhere-typing";
  return "silent";
}

/**
 * Drives the four-state presence-sound derivation described in the project
 * plan: same-line-typing / same-line-idle / elsewhere-typing / silent, per
 * remote peer, recomputed on every awareness change. Loops only start/stop
 * on an actual state transition, never on every awareness tick, so a peer
 * merely moving within the same state doesn't restart audio.
 *
 * Mirrors attachRemoteCursorStyles()'s shape (attach on doc open, call the
 * returned detach function in teardownSync()).
 */
export function attachPresenceSounds(awareness: Awareness): () => void {
  const localClientId = awareness.clientID;
  // Loop/one-shot playback state is naturally per awareness clientID (one
  // WebSocket connection's Y.Doc), but join/leave membership must be tracked
  // per *user id* instead: a peer's clientID is a random id minted fresh
  // per connection, so a brief reconnect (network blip, or the peer
  // reopening the same doc) mints a new clientID even though it's the same
  // person - keying on clientID alone reported that as a leave+join pair.
  const peers = new Map<number, PeerPlayback>();
  let knownUserIds = new Set<number>();
  const pendingLeaves = new Map<number, ReturnType<typeof setTimeout>>();
  let destroyed = false;
  // The peers already present when we open the doc trickle in as awareness
  // state over the first fraction of a second after attach, not all at once
  // on the first render. So instead of a single "first render is the
  // baseline" flag, treat every render within BASELINE_SETTLE_MS of attach as
  // baseline: keep re-seeding knownUserIds from it silently, and only start
  // chiming joins/leaves once that window has passed.
  const attachedAt = Date.now();

  function stopLoop(playback: PeerPlayback): void {
    if (playback.loopSource) {
      try {
        playback.loopSource.stop();
      } catch {
        // Already stopped - fine to ignore.
      }
      playback.loopSource.disconnect();
      playback.loopSource = null;
    }
    playback.loopGain?.disconnect();
    playback.loopGain = null;
  }

  function startLoop(buffer: AudioBuffer, playback: PeerPlayback): void {
    const ctx = getAudioContext();
    const source = ctx.createBufferSource();
    source.buffer = buffer;
    source.loop = true;
    const gain = ctx.createGain();
    gain.gain.value = 1;
    source.connect(gain);
    gain.connect(masterGain!);
    source.start();
    playback.loopSource = source;
    playback.loopGain = gain;
  }

  async function render(): Promise<void> {
    const buffers = await loadBuffers();
    if (destroyed) return;

    const states = awareness.getStates() as Map<number, RemoteAwarenessState>;
    const local = states.get(localClientId);

    // Drop loop/one-shot bookkeeping for connections (clientIDs) that are no
    // longer present, regardless of whether their user is still around under
    // a different clientID - a stale connection's audio must still stop.
    for (const [clientId, playback] of peers) {
      if (!states.has(clientId) || clientId === localClientId) {
        stopLoop(playback);
        peers.delete(clientId);
      }
    }

    // Join/leave chimes are computed separately, from the set of distinct
    // user ids present this render vs. last render - not from clientIDs -
    // so a reconnect (old clientID gone, new clientID for the same user
    // appearing in the same or next render) nets out to no event instead of
    // a spurious leave+join pair.
    const currentUserIds = new Set<number>();
    for (const [clientId, peerState] of states) {
      if (clientId === localClientId) continue;
      if (peerState.user) currentUserIds.add(peerState.user.id);
    }

    const inBaselineWindow = Date.now() - attachedAt < BASELINE_SETTLE_MS;

    if (!inBaselineWindow) {
      for (const userId of knownUserIds) {
        if (currentUserIds.has(userId) || pendingLeaves.has(userId)) continue;
        // Hold the "left" chime briefly rather than playing it immediately,
        // so a same-user reconnect (new clientID for the same user id,
        // arriving moments later) can cancel it below instead of the user
        // hearing left+joined in quick succession.
        const timer = setTimeout(() => {
          pendingLeaves.delete(userId);
          if (!destroyed) playBuffer(buffers.peerLeft);
        }, LEAVE_DEBOUNCE_MS);
        pendingLeaves.set(userId, timer);
      }
      for (const userId of currentUserIds) {
        const pending = pendingLeaves.get(userId);
        if (pending !== undefined) {
          // Same user reappeared before their pending "left" fired - this
          // was a reconnect, not a real leave, so cancel it and don't chime
          // "joined" either.
          clearTimeout(pending);
          pendingLeaves.delete(userId);
          continue;
        }
        if (!knownUserIds.has(userId)) playBuffer(buffers.peerJoined);
      }
    }
    knownUserIds = currentUserIds;

    for (const [clientId, peerState] of states) {
      if (clientId === localClientId) continue;
      const nextState = deriveState(local, peerState);
      let playback = peers.get(clientId);
      if (!playback) {
        playback = { state: "silent", loopSource: null, loopGain: null, lastElsewhereClickAt: 0 };
        peers.set(clientId, playback);
      }

      if (nextState === playback.state) {
        if (nextState === "elsewhere-typing") {
          const now = performance.now();
          if (now - playback.lastElsewhereClickAt >= ELSEWHERE_CLICK_MIN_INTERVAL_MS) {
            playback.lastElsewhereClickAt = now;
            playBuffer(buffers.elsewhereTyping);
          }
        }
        continue;
      }

      stopLoop(playback);
      playback.state = nextState;
      if (nextState === "same-line-typing") {
        startLoop(buffers.sameLineTyping, playback);
      } else if (nextState === "same-line-idle") {
        startLoop(buffers.sameLineIdle, playback);
      } else if (nextState === "elsewhere-typing") {
        playback.lastElsewhereClickAt = performance.now();
        playBuffer(buffers.elsewhereTyping);
      }
    }
  }

  const onChange = () => void render();
  awareness.on("change", onChange);
  void render();
  // One render right as the baseline window closes, so knownUserIds reflects
  // the settled roster even if no awareness "change" happens to land right
  // after it - the next real join/leave is then diffed against the right set.
  const settleTimer = setTimeout(() => void render(), BASELINE_SETTLE_MS);

  return () => {
    destroyed = true;
    awareness.off("change", onChange);
    clearTimeout(settleTimer);
    for (const playback of peers.values()) stopLoop(playback);
    peers.clear();
    for (const timer of pendingLeaves.values()) clearTimeout(timer);
    pendingLeaves.clear();
  };
}
