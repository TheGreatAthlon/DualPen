import asyncio
import json
import logging
from contextlib import asynccontextmanager

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pycrdt import Text
from pycrdt.websocket import WebsocketServer
from pycrdt.websocket.yroom import YRoom

from server.app import chat_service, docstore, node_service
from server.app.auth import SESSION_COOKIE_NAME, get_user_for_session_token
from server.app.db import AsyncSessionLocal
from server.app.models import User

logger = logging.getLogger(__name__)

router = APIRouter()

TEXT_KEY = "content"
DEBOUNCE_SECONDS = 2.0
MAX_FLUSH_INTERVAL_SECONDS = 15.0

CLOSE_UNAUTHORIZED = 4401
CLOSE_NOT_FOUND = 4404
CLOSE_REPLACED_BY_NEWER_SESSION = 4409

MESSAGE_TYPE_CHAT = 0x02

websocket_server = WebsocketServer()

# Per-room bookkeeping that WebsocketServer itself doesn't track: whether we've
# already seeded this room's Y.Text from disk, and the debounce/flush timer
# state for persistence. Keyed by doc_id, same lifetime as websocket_server.rooms.
_seeded_doc_ids: set[str] = set()
_flush_tasks: dict[str, asyncio.Task] = {}
_last_flush_at: dict[str, float] = {}
# pycrdt's Subscription wraps a Rust object that isn't safe to drop from an
# arbitrary GC thread (surfaces as "Subscription is unsendable, but is being
# dropped on another thread"). Track it explicitly so room teardown can call
# .drop() itself instead of leaving it to whenever the garbage collector
# happens to finalize the Doc.
_observers: dict[str, object] = {}

# Enforces "one document open per user" server-side. Maps user id -> the
# doc_id and live WebSocket of their current connection. A second connection
# from the same user to a *different* doc_id force-closes the entry found
# here; the entry is updated/cleared by whichever connection's lifecycle
# (new connect, or its own disconnect) touches it last.
_user_open_doc: dict[int, tuple[str, WebSocket]] = {}


@asynccontextmanager
async def sync_lifespan():
    async with websocket_server:
        yield


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

    def __init__(self, websocket: WebSocket, doc_id: str, user: User, room: YRoom):
        self._websocket = websocket
        self._doc_id = doc_id
        self._user = user
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
                await self._handle_chat_frame(message)
                continue
            return message

    async def send(self, message: bytes) -> None:
        await self._websocket.send_bytes(message)

    async def recv(self) -> bytes:
        message = await self._websocket.receive_bytes()
        return bytes(message)

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

        if len(self.room.clients) <= 1:
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
        await asyncio.gather(
            *(client.send(out_message) for client in self.room.clients), return_exceptions=True
        )


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
    room = await websocket_server.get_room(doc_id)

    if doc_id in _seeded_doc_ids:
        return room
    _seeded_doc_ids.add(doc_id)

    ytext = room.ydoc.get(TEXT_KEY, type=Text)
    if len(ytext) == 0:
        try:
            content = docstore.read_document(blob_path)
        except FileNotFoundError:
            content = ""
        if content:
            ytext.insert(0, content)

    def _on_text_change(_event) -> None:
        _schedule_flush(doc_id, blob_path)

    _observers[doc_id] = ytext.observe(_on_text_change)
    return room


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
        prior_doc_id, prior_ws = prior
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

    _user_open_doc[user.id] = (doc_id, websocket)

    room = await _seed_room_from_disk(doc_id, blob_path)

    # WebsocketServer.serve() auto-deletes the room from its registry the
    # instant the last client disconnects (before returning control to us),
    # which would race our own disconnect-triggered flush below. Drive the
    # room directly instead so we control exactly when it's read and torn
    # down: seed -> serve -> persist -> delete, in that order.
    channel = FastAPIChannel(websocket, doc_id, user, room)
    try:
        await room.serve(channel)
    finally:
        # Only clear/own this user's entry if it still points at *this*
        # connection. If a newer connection already force-closed us and
        # overwrote the entry (or, in principle, raced ahead of us), we must
        # not clobber it here - whichever connection is current owns cleanup
        # of its own entry.
        current = _user_open_doc.get(user.id)
        if current is not None and current[1] is websocket:
            del _user_open_doc[user.id]

        # Make sure this client's last edits aren't left sitting only in the
        # debounce window if they just close the tab, and that multi-client
        # rooms are only torn down once truly empty.
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
