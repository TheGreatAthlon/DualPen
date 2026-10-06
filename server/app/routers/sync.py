import asyncio
import hashlib
import json
import logging
import weakref
from contextlib import asynccontextmanager

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pycrdt import Doc, Text, YMessageType, YSyncMessageType, read_message
from pycrdt.websocket import WebsocketServer
from pycrdt.websocket.yroom import YRoom

from server.app import chat_service, docstore, node_service, pubsub
from server.app.auth import SESSION_COOKIE_NAME, get_guest_grant, get_user_for_session_token
from server.app.db import AsyncSessionLocal
from server.app.models import User

logger = logging.getLogger(__name__)

router = APIRouter()

TEXT_KEY = "content"
DEBOUNCE_SECONDS = 2.0
MAX_FLUSH_INTERVAL_SECONDS = 15.0

CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_NOT_FOUND = 4404
CLOSE_REPLACED_BY_NEWER_SESSION = 4409

MESSAGE_TYPE_CHAT = 0x02

# How often an open connection re-checks that its session (and user) is still valid.
# This is the backstop for revoke/deactivate/expiry when the cross-process kick is lost.
REVALIDATE_SECONDS = 30.0

websocket_server = WebsocketServer()

# In-memory no-op unless COLLAB_EDITOR_REDIS_URL is set; see pubsub.py. When
# Redis is enabled, each process keeps its own local websockets/rooms and this
# fans doc updates, awareness, chat and presence out to the other processes.
broadcaster = pubsub.create_broadcaster()

# How long a process joining a doc waits for a peer to hand over the live doc
# state before falling back to seeding from disk.
STATE_WAIT_SECONDS = 0.5

# Per-room bookkeeping that WebsocketServer itself doesn't track: whether we've
# already seeded this room's Y.Text from disk, and the debounce/flush timer
# state for persistence. Keyed by doc_id, same lifetime as websocket_server.rooms.
_seeded_doc_ids: set[str] = set()
# Weak values: a lock disappears once no joiner holds or awaits it.
_seed_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
_flush_tasks: dict[str, asyncio.Task] = {}
_last_flush_at: dict[str, float] = {}
# pycrdt's Subscription wraps a Rust object that isn't safe to drop from an
# arbitrary GC thread (surfaces as "Subscription is unsendable, but is being
# dropped on another thread"). Track it explicitly so room teardown can call
# .drop() itself instead of leaving it to whenever the garbage collector
# happens to finalize the Doc.
_observers: dict[str, object] = {}
# Redis mode only: ydoc-level observers that publish local updates, doc ids
# whose state is complete enough to hand to a joining peer, doc ids currently
# applying a remote update (so the observer doesn't re-publish it), and
# per-doc events set when a peer's state reply arrives.
_update_observers: dict[str, object] = {}
_ready_doc_ids: set[str] = set()
_applying_remote: set[str] = set()
_state_events: dict[str, asyncio.Event] = {}

# Enforces "one document open per user" server-side, and doubles as the
# backing store for the /api/presence endpoint (see presence.py). Maps user
# id -> (doc_id, display_name, live WebSocket) of their current connection.
# display_name is cached here (rather than looked up per presence request)
# since it's already in hand at connect time. A second connection from the
# same user to a *different* doc_id force-closes the entry found here; the
# entry is updated/cleared by whichever connection's lifecycle (new connect,
# or its own disconnect) touches it last.
_user_open_doc: dict[int, tuple[str, str, WebSocket]] = {}


def get_open_docs_by_user() -> dict[int, tuple[str, str]]:
    """Snapshot of user id -> (doc_id, display_name) for currently-connected
    users, for the presence endpoint. Excludes the live WebSocket, which
    presence has no business touching. Local process only."""
    return {user_id: (doc_id, display_name) for user_id, (doc_id, display_name, _ws) in _user_open_doc.items()}


async def get_all_open_docs_by_user() -> dict[int, tuple[str, str]]:
    """Like get_open_docs_by_user, but includes users connected to other
    processes when Redis is enabled."""
    merged = await broadcaster.list_presence()
    merged.update(get_open_docs_by_user())
    return merged


@asynccontextmanager
async def sync_lifespan():
    broadcaster.set_resync_handler(_resync_doc)
    broadcaster.set_control_handler(_handle_user_opened_elsewhere)
    broadcaster.set_kick_handler(close_user_connections)
    await broadcaster.start()
    try:
        async with websocket_server:
            yield
    finally:
        await broadcaster.stop()


class FastAPIChannel:
    """Bridges a Starlette WebSocket to pycrdt_websocket's Channel protocol.

    Also intercepts chat (0x02) frames before they ever reach YRoom.serve()'s
    receive loop. This can't be done via YRoom.on_message (pycrdt's own
    "extra message type" hook): that callback only receives raw bytes, with
    no reference to which connection/channel sent it, so it has no way to
    know the authenticated sender - handling chat here instead, where the
    per-connection `user` is already in scope, means the server always
    stamps user_id/display_name itself rather than trusting anything the
    client claims about its own identity.
    """

    def __init__(self, websocket: WebSocket, doc_id: str, user: User, room: YRoom, read_only: bool = False, guest: bool = False):
        self._websocket = websocket
        self._doc_id = doc_id
        self._user = user
        self._read_only = read_only
        self._guest = guest
        self.room = room

    @property
    def path(self) -> str:
        return self._doc_id

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        while True:
            try:
                message = await self.recv()
            except WebSocketDisconnect:
                raise StopAsyncIteration()
            if message and message[0] == MESSAGE_TYPE_CHAT:
                if not self._read_only:
                    await self._handle_chat_frame(message)
                continue
            if self._guest and message and message[0] == YMessageType.AWARENESS:
                # Awareness is client-controlled: pin a guest's identity to what the server knows.
                message = _guest_awareness(message, self._user.id, self._user.display_name)
                if message is None:
                    continue
            if self._read_only and _is_doc_write(message):
                continue
            if message and message[0] == YMessageType.AWARENESS:
                # YRoom.serve() fans this out to local clients itself; we only
                # need to forward it to other processes.
                await broadcaster.publish(self._doc_id, pubsub.KIND_AWARENESS, message)
            return message

    async def send(self, message: bytes) -> None:
        await self._websocket.send_bytes(message)

    async def recv(self) -> bytes:
        message = await self._websocket.receive_bytes()
        return bytes(message)

    async def _has_remote_participants(self) -> bool:
        if not broadcaster.enabled:
            return False
        presence = await broadcaster.list_presence()
        return any(uid != self._user.id and doc == self._doc_id for uid, (doc, _name) in presence.items())

    async def _handle_chat_frame(self, message: bytes) -> None:
        try:
            payload = json.loads(message[1:].decode("utf-8"))
            body = payload["body"]
        except (json.JSONDecodeError, UnicodeDecodeError, KeyError, TypeError):
            logger.warning("Dropping malformed chat frame on doc %s", self._doc_id)
            return

        if not isinstance(body, str) or not body.strip():
            return
        body = body.strip()[: chat_service.MAX_BODY_LENGTH]

        if len(self.room.clients) <= 1 and not await self._has_remote_participants():
            # Defense in depth behind the client-side "no one else is
            # editing this document" check - never persist/broadcast a
            # message with zero possible recipients.
            return

        async with AsyncSessionLocal() as db:
            saved = await chat_service.create_message(
                db, doc_id=self._doc_id, user_id=self._user.id, display_name=self._user.display_name, body=body
            )

        out_payload = json.dumps(
            {
                "id": saved.id,
                "docId": saved.doc_id,
                "userId": saved.user_id,
                "displayName": saved.display_name,
                "body": saved.body,
                "sentAt": saved.sent_at.isoformat(),
            }
        ).encode("utf-8")
        out_message = bytes([MESSAGE_TYPE_CHAT]) + out_payload

        # Concurrent, not sequential, so one slow connection can't delay
        # delivery to the others (mirrors YRoom.serve()'s own tg.start_soon
        # fan-out for awareness messages).
        await _send_to_local_clients(self.room, out_message)
        await broadcaster.publish(self._doc_id, pubsub.KIND_CHAT, out_message)


def _is_doc_write(message: bytes) -> bool:
    """Sync step2 / update frames carry document changes; step1 is only a read request."""
    return (
        len(message) > 1
        and message[0] == YMessageType.SYNC
        and message[1] in (YSyncMessageType.SYNC_STEP2, YSyncMessageType.SYNC_UPDATE)
    )


def _read_varuint(data: bytes, i: int) -> tuple[int, int]:
    n = shift = 0
    while True:
        byte = data[i]
        i += 1
        n |= (byte & 0x7F) << shift
        shift += 7
        if byte < 0x80:
            return n, i


def _write_varuint(n: int) -> bytes:
    out = bytearray()
    while n > 0x7F:
        out.append(0x80 | (n & 0x7F))
        n >>= 7
    out.append(n)
    return bytes(out)


MAX_GUEST_AWARENESS_BYTES = 4096


def _guest_awareness(message: bytes, user_id: int, name: str) -> bytes | None:
    """Rewrite an awareness frame so every state's `user` is the guest's server-known
    identity. Returns None (drop the frame) if it is oversized or malformed."""
    if len(message) > MAX_GUEST_AWARENESS_BYTES:
        return None
    try:
        _length, i = _read_varuint(message, 1)
        count, i = _read_varuint(message, i)
        body = bytearray(_write_varuint(count))
        for _ in range(count):
            client_id, i = _read_varuint(message, i)
            clock, i = _read_varuint(message, i)
            size, i = _read_varuint(message, i)
            raw = message[i : i + size]
            if len(raw) != size:
                return None
            i += size
            state = json.loads(raw.decode("utf-8"))
            if isinstance(state, dict):
                user = state.get("user")
                state["user"] = {**(user if isinstance(user, dict) else {}), "id": user_id, "name": name}
            elif state is not None:  # null = "client went away"
                return None
            out = json.dumps(state, separators=(",", ":")).encode("utf-8")
            body += _write_varuint(client_id) + _write_varuint(clock) + _write_varuint(len(out)) + out
        return bytes([YMessageType.AWARENESS]) + _write_varuint(len(body)) + bytes(body)
    except (IndexError, ValueError):
        return None


async def _revalidate_loop(websocket: WebSocket, session_token: str | None) -> None:
    while True:
        await asyncio.sleep(REVALIDATE_SECONDS)
        try:
            async with AsyncSessionLocal() as db:
                valid = await get_user_for_session_token(db, session_token) is not None
        except Exception:
            logger.exception("Session re-check failed; will retry")
            continue
        if not valid:
            try:
                await websocket.close(code=CLOSE_FORBIDDEN)
            except Exception:
                logger.warning("Failed to close revoked connection")
            return


async def kick_users(user_ids: list[int]) -> None:
    """Close these users' live connections on every process."""
    if not user_ids:
        return
    await close_user_connections(user_ids)
    await broadcaster.publish_users_revoked(user_ids)


async def close_user_connections(user_ids: list[int]) -> None:
    """Close live local sockets for these users (e.g. guests of a revoked link)."""
    for user_id in user_ids:
        entry = _user_open_doc.get(user_id)
        if entry is not None:
            try:
                await entry[2].close(code=CLOSE_FORBIDDEN)
            except Exception:
                logger.warning("Failed to close connection for user %s", user_id)


async def _send_to_local_clients(room: YRoom, message: bytes) -> None:
    await asyncio.gather(*(client.send(message) for client in room.clients), return_exceptions=True)


async def _resync_doc(doc_id: str) -> None:
    """After a Redis outage: hand peers our full state and ask for theirs, so
    edits made while pub/sub was down reconcile in both directions."""
    room = websocket_server.rooms.get(doc_id)
    if room is None or doc_id not in _ready_doc_ids:
        return
    await broadcaster.publish(doc_id, pubsub.KIND_STATE, room.ydoc.get_update())
    await broadcaster.publish(doc_id, pubsub.KIND_STATE_REQUEST, b"")


async def _handle_user_opened_elsewhere(user_id: int, doc_id: str) -> None:
    """Cross-process half of one-doc-per-user: another process says this user
    just opened doc_id, so close our older connection if it's a different doc."""
    prior = _user_open_doc.get(user_id)
    if prior is not None and prior[0] != doc_id:
        try:
            await prior[2].close(code=CLOSE_REPLACED_BY_NEWER_SESSION)
        except Exception:
            logger.warning("Failed to force-close connection for user %s replaced on another process", user_id)


def _seed_client_id(doc_id: str, content: str) -> int:
    digest = hashlib.sha256(f"{doc_id}:{content}".encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big")


async def _handle_remote(doc_id: str, kind: bytes, payload: bytes) -> None:
    """Apply a message another process published for this doc."""
    room = websocket_server.rooms.get(doc_id)
    if room is None:
        return
    if kind in (pubsub.KIND_UPDATE, pubsub.KIND_STATE):
        # YRoom's own ydoc observer relays this to our local clients.
        _applying_remote.add(doc_id)
        try:
            room.ydoc.apply_update(payload)
        finally:
            _applying_remote.discard(doc_id)
        if kind == pubsub.KIND_STATE:
            event = _state_events.get(doc_id)
            if event is not None:
                event.set()
    elif kind == pubsub.KIND_AWARENESS:
        await _send_to_local_clients(room, payload)
        room.awareness.apply_awareness_update(read_message(payload[1:]), room)
    elif kind == pubsub.KIND_CHAT:
        await _send_to_local_clients(room, payload)
    elif kind == pubsub.KIND_STATE_REQUEST:
        if doc_id in _ready_doc_ids:
            await broadcaster.publish(doc_id, pubsub.KIND_STATE, room.ydoc.get_update())


def _persist_doc_id(doc_id: str, blob_path: str) -> None:
    room = websocket_server.rooms.get(doc_id)
    if room is None:
        return
    ytext = room.ydoc.get(TEXT_KEY, type=Text)
    content = str(ytext)
    try:
        docstore.write_document(blob_path, content)
    except Exception:
        logger.exception("Failed to persist document %s", doc_id)
    else:
        _last_flush_at[doc_id] = asyncio.get_event_loop().time()


async def _debounced_flush(doc_id: str, blob_path: str) -> None:
    try:
        await asyncio.sleep(DEBOUNCE_SECONDS)
    except asyncio.CancelledError:
        return
    _persist_doc_id(doc_id, blob_path)


def _schedule_flush(doc_id: str, blob_path: str) -> None:
    existing = _flush_tasks.get(doc_id)
    if existing is not None and not existing.done():
        existing.cancel()

    now = asyncio.get_event_loop().time()
    last = _last_flush_at.get(doc_id)
    if last is not None and (now - last) >= MAX_FLUSH_INTERVAL_SECONDS:
        # Continuous typing hard cap: don't let the debounce keep pushing the
        # write out indefinitely — flush immediately and restart the window.
        _persist_doc_id(doc_id, blob_path)

    _flush_tasks[doc_id] = asyncio.ensure_future(_debounced_flush(doc_id, blob_path))


async def _seed_room_from_disk(doc_id: str, blob_path: str):
    # Serialize per doc so a second joiner can't get a half-seeded room while
    # the first is still waiting on peers. The room is fetched inside the lock
    # so a failed seed that deletes its empty room can't leave a waiter with a
    # stale one.
    async with _seed_locks.setdefault(doc_id, asyncio.Lock()):
        room = await websocket_server.get_room(doc_id)
        if doc_id in _seeded_doc_ids:
            return room
        try:
            await _seed_room(room, doc_id, blob_path)
        except BaseException:
            _state_events.pop(doc_id, None)
            if broadcaster.enabled:
                await broadcaster.unsubscribe(doc_id)
            if not room.clients:
                await websocket_server.delete_room(room=room)
            raise
        _seeded_doc_ids.add(doc_id)
    return room


async def _seed_room(room, doc_id: str, blob_path: str) -> None:
    ytext = room.ydoc.get(TEXT_KEY, type=Text)
    got_peer_state = False
    if broadcaster.enabled:
        # Another process may already hold live edits that aren't on disk yet.
        # Seeding from disk on top of that would insert the same text under a
        # different Yjs client id and duplicate it on merge, so ask peers for
        # their state first and only seed from disk if nobody answers.
        event = _state_events[doc_id] = asyncio.Event()
        await broadcaster.subscribe(doc_id, lambda kind, payload: _handle_remote(doc_id, kind, payload))
        await broadcaster.publish(doc_id, pubsub.KIND_STATE_REQUEST, b"")
        try:
            await asyncio.wait_for(event.wait(), STATE_WAIT_SECONDS)
            got_peer_state = True
        except asyncio.TimeoutError:
            pass
        _state_events.pop(doc_id, None)

    if len(ytext) == 0 and not got_peer_state:
        try:
            content = docstore.read_document(blob_path)
        except FileNotFoundError:
            content = ""
        if content and broadcaster.enabled:
            # Seed through a throwaway Doc whose client id derives from the
            # doc id + content: processes seeding the same disk content at the
            # same moment produce identical updates that dedupe on merge,
            # instead of two copies under different random client ids.
            seed_doc = Doc(client_id=_seed_client_id(doc_id, content))
            seed_doc.get(TEXT_KEY, type=Text).insert(0, content)
            room.ydoc.apply_update(seed_doc.get_update())
        elif content:
            ytext.insert(0, content)

    def _on_text_change(_event) -> None:
        _schedule_flush(doc_id, blob_path)

    _observers[doc_id] = ytext.observe(_on_text_change)

    if broadcaster.enabled:

        def _on_ydoc_update(event) -> None:
            if doc_id in _applying_remote:
                return
            broadcaster.publish_nowait(doc_id, pubsub.KIND_UPDATE, event.update)

        _update_observers[doc_id] = room.ydoc.observe(_on_ydoc_update)
        _ready_doc_ids.add(doc_id)


@router.websocket("/ws/doc/{doc_id}")
async def doc_sync(websocket: WebSocket, doc_id: str):
    session_token = websocket.cookies.get(SESSION_COOKIE_NAME)

    # Accept before any possible close: rejecting pre-handshake makes some
    # ASGI servers (uvicorn included) fail the WS opening handshake itself
    # rather than deliver our custom close code to the client, so a real
    # browser/`ws` client would see a bare 1006 and never learn *why*.
    # Accepting first guarantees a rejection's close code is actually
    # delivered.
    await websocket.accept()

    async with AsyncSessionLocal() as db:
        user = await get_user_for_session_token(db, session_token)
        if user is None:
            await websocket.close(code=CLOSE_UNAUTHORIZED)
            return

        grant = await get_guest_grant(db, user)
        if grant is not None and grant.doc_id != doc_id:
            await websocket.close(code=CLOSE_FORBIDDEN)
            return
        read_only = grant is not None and grant.read_only

        try:
            node = await node_service.get_document_node(db, doc_id)
        except node_service.NodeNotFoundError:
            await websocket.close(code=CLOSE_NOT_FOUND)
            return
        if not node.blob_path:
            await websocket.close(code=CLOSE_NOT_FOUND)
            return
        blob_path = node.blob_path

    prior = _user_open_doc.get(user.id)
    if prior is not None and prior[0] != doc_id:
        prior_doc_id, _prior_display_name, prior_ws = prior
        # Closing the *other* connection's WebSocket from here causes its own
        # coroutine (blocked awaiting receive_bytes() inside room.serve()) to
        # observe a "websocket.disconnect" ASGI message and raise
        # WebSocketDisconnect. That exception propagates out of
        # FastAPIChannel.recv()/__anext__ as StopAsyncIteration, which ends
        # that room's serve() loop and runs that connection's own `finally`
        # block below - so the old connection persists and tears itself down
        # through its normal path. We only need to ask it to close; we never
        # touch its room/task state directly from this coroutine.
        try:
            await prior_ws.close(code=CLOSE_REPLACED_BY_NEWER_SESSION)
        except Exception:
            logger.warning(
                "Failed to force-close prior connection for user %s (doc %s)",
                user.id,
                prior_doc_id,
            )

    _user_open_doc[user.id] = (doc_id, user.display_name, websocket)
    await broadcaster.set_presence(user.id, doc_id, user.display_name)
    await broadcaster.publish_user_opened(user.id, doc_id)

    room = None

    # A revoke can land between the auth check above and registering in
    # _user_open_doc (close_user_connections would then miss us): re-check.
    async with AsyncSessionLocal() as db:
        still_valid = await get_user_for_session_token(db, session_token) is not None

    # WebsocketServer.serve() auto-deletes the room from its registry the
    # instant the last client disconnects (before returning control to us),
    # which would race our own disconnect-triggered flush below. Drive the
    # room directly instead so we control exactly when it's read and torn
    # down: seed -> serve -> persist -> delete, in that order.
    revalidator = asyncio.ensure_future(_revalidate_loop(websocket, session_token))
    try:
        if not still_valid:
            await websocket.close(code=CLOSE_FORBIDDEN)
            return
        room = await _seed_room_from_disk(doc_id, blob_path)
        channel = FastAPIChannel(websocket, doc_id, user, room, read_only, grant is not None)
        await room.serve(channel)
    finally:
        # Only clear/own this user's entry if it still points at *this*
        # connection. If a newer connection already force-closed us and
        # overwrote the entry (or, in principle, raced ahead of us), we must
        # not clobber it here - whichever connection is current owns cleanup
        # of its own entry.
        revalidator.cancel()
        current = _user_open_doc.get(user.id)
        if current is not None and current[2] is websocket:
            del _user_open_doc[user.id]
            await broadcaster.clear_presence(user.id)

        # Make sure this client's last edits aren't left sitting only in the
        # debounce window if they just close the tab, and that multi-client
        # rooms are only torn down once truly empty.
        if room is not None:
            _persist_doc_id(doc_id, blob_path)
            pending = _flush_tasks.pop(doc_id, None)
            if pending is not None and not pending.done():
                pending.cancel()
            if not room.clients:
                await websocket_server.delete_room(room=room)
                _seeded_doc_ids.discard(doc_id)
                _last_flush_at.pop(doc_id, None)
                observer = _observers.pop(doc_id, None)
                if observer is not None:
                    observer.drop()
                if broadcaster.enabled:
                    _ready_doc_ids.discard(doc_id)
                    await broadcaster.unsubscribe(doc_id)
                    update_observer = _update_observers.pop(doc_id, None)
                    if update_observer is not None:
                        update_observer.drop()
