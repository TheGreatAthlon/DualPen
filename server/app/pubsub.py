"""Cross-process fan-out for realtime rooms.

Two implementations behind one small interface:

* InMemoryBroadcaster: every method is a no-op. Used when
  COLLAB_EDITOR_REDIS_URL is unset, i.e. the original single-process
  behavior; rooms and presence live only in this process's memory.
* RedisBroadcaster: per-room Redis pub/sub channels plus TTL'd presence keys.
  Each process still holds only its own local websockets; it publishes what
  its local clients produce and applies what *other* processes publish.

Envelope on the wire: <32 hex chars process id><1 byte kind><payload>. The
process id lets a process drop its own messages (Redis delivers a publisher's
messages back to its own subscription), which prevents echo duplicates.
"""

import asyncio
import json
import logging
import os
import uuid
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

REDIS_URL_ENV = "COLLAB_EDITOR_REDIS_URL"

KIND_UPDATE = b"u"  # Yjs document update
KIND_AWARENESS = b"a"  # full awareness frame (type byte included)
KIND_CHAT = b"c"  # full chat frame (type byte included)
KIND_STATE_REQUEST = b"r"  # "send me your full doc state"
KIND_STATE = b"s"  # reply to KIND_STATE_REQUEST: full doc state as an update

PRESENCE_TTL_SECONDS = 15
PRESENCE_REFRESH_SECONDS = 5.0
PRESENCE_LIST_TIMEOUT_SECONDS = 1.0

CONTROL_CHANNEL = "collab:control"
PUBLISH_ATTEMPTS = 3
RESYNC_DELAY_SECONDS = 0.5

PID_LENGTH = 32

Handler = Callable[[bytes, bytes], Awaitable[None]]
ResyncHandler = Callable[[str], Awaitable[None]]
# (user_id, doc_id) of a user who just opened doc_id on another process.
ControlHandler = Callable[[int, str], Awaitable[None]]
KickHandler = Callable[[list[int]], Awaitable[None]]


class Broadcaster:
    enabled = False

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def subscribe(self, doc_id: str, handler: Handler) -> None:
        pass

    async def unsubscribe(self, doc_id: str) -> None:
        pass

    async def publish(self, doc_id: str, kind: bytes, payload: bytes) -> None:
        pass

    def publish_nowait(self, doc_id: str, kind: bytes, payload: bytes) -> None:
        pass

    def set_resync_handler(self, handler: ResyncHandler) -> None:
        pass

    def set_control_handler(self, handler: ControlHandler) -> None:
        pass

    async def publish_user_opened(self, user_id: int, doc_id: str) -> None:
        pass

    def set_kick_handler(self, handler: KickHandler) -> None:
        pass

    async def publish_users_revoked(self, user_ids: list[int]) -> None:
        pass

    async def set_presence(self, user_id: int, doc_id: str, display_name: str) -> None:
        pass

    async def clear_presence(self, user_id: int) -> None:
        pass

    async def list_presence(self) -> dict[int, tuple[str, str]]:
        """user id -> (doc_id, display_name) across *all* processes."""
        return {}


InMemoryBroadcaster = Broadcaster


class RedisBroadcaster(Broadcaster):
    enabled = True

    def __init__(self, client, process_id: str | None = None):
        self._redis = client
        self.process_id = process_id or uuid.uuid4().hex
        self._handlers: dict[str, Handler] = {}
        self._pubsub = None
        self._reader: asyncio.Task | None = None
        self._refresher: asyncio.Task | None = None
        self._local_presence: dict[int, tuple[str, str]] = {}
        # Every publish goes through one ordered queue + worker so a burst of
        # edits can't open unbounded concurrent Redis connections.
        self._queue: asyncio.Queue[tuple[str, bytes]] = asyncio.Queue()
        self._publisher: asyncio.Task | None = None
        self._resyncer: asyncio.Task | None = None
        self._needs_resync = False
        self._resync_handler: ResyncHandler | None = None
        self._control_handler: ControlHandler | None = None
        self._kick_handler: KickHandler | None = None

    def set_resync_handler(self, handler: ResyncHandler) -> None:
        self._resync_handler = handler

    def set_control_handler(self, handler: ControlHandler) -> None:
        self._control_handler = handler

    def set_kick_handler(self, handler: KickHandler) -> None:
        self._kick_handler = handler

    @staticmethod
    def _channel(doc_id: str) -> str:
        return f"collab:room:{doc_id}"

    def _presence_key(self, user_id: int) -> str:
        return f"collab:presence:{self.process_id}:{user_id}"

    async def start(self) -> None:
        self._pubsub = self._redis.pubsub()
        # Stay subscribed to something at all times: redis-py's reader loop
        # idles instead of blocking when a PubSub has no subscriptions.
        await self._pubsub.subscribe(CONTROL_CHANNEL)
        self._publisher = asyncio.ensure_future(self._publish_loop())
        self._reader = asyncio.ensure_future(self._read_loop())
        self._refresher = asyncio.ensure_future(self._refresh_presence_loop())

    async def stop(self) -> None:
        tasks = (self._reader, self._refresher, self._publisher, self._resyncer)
        for task in tasks:
            if task is not None:
                task.cancel()
        for task in tasks:
            if task is not None:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._reader = self._refresher = self._publisher = self._resyncer = None
        if self._pubsub is not None:
            try:
                await self._pubsub.aclose()
            except Exception:
                logger.exception("Failed closing Redis pubsub")
            self._pubsub = None
        for user_id in list(self._local_presence):
            await self.clear_presence(user_id)
        try:
            await self._redis.aclose()
        except Exception:
            logger.exception("Failed closing Redis client")

    async def subscribe(self, doc_id: str, handler: Handler) -> None:
        self._handlers[doc_id] = handler
        try:
            await self._pubsub.subscribe(self._channel(doc_id))
        except Exception:
            logger.exception("Redis subscribe failed for doc %s", doc_id)

    async def unsubscribe(self, doc_id: str) -> None:
        self._handlers.pop(doc_id, None)
        try:
            await self._pubsub.unsubscribe(self._channel(doc_id))
        except Exception:
            logger.exception("Redis unsubscribe failed for doc %s", doc_id)

    async def publish(self, doc_id: str, kind: bytes, payload: bytes) -> None:
        self.publish_nowait(doc_id, kind, payload)

    def publish_nowait(self, doc_id: str, kind: bytes, payload: bytes) -> None:
        self._queue.put_nowait((self._channel(doc_id), self.process_id.encode("ascii") + kind + payload))

    async def publish_user_opened(self, user_id: int, doc_id: str) -> None:
        body = json.dumps({"user_id": user_id, "doc_id": doc_id}).encode("utf-8")
        self._queue.put_nowait((CONTROL_CHANNEL, self.process_id.encode("ascii") + b"o" + body))

    async def publish_users_revoked(self, user_ids: list[int]) -> None:
        body = json.dumps({"user_ids": user_ids}).encode("utf-8")
        self._queue.put_nowait((CONTROL_CHANNEL, self.process_id.encode("ascii") + b"k" + body))

    async def _publish_loop(self) -> None:
        while True:
            batch = [await self._queue.get()]
            while not self._queue.empty():
                batch.append(self._queue.get_nowait())
            attempts = 1 if self._needs_resync else PUBLISH_ATTEMPTS
            for attempt in range(attempts):
                try:
                    pipe = self._redis.pipeline(transaction=False)
                    for channel, envelope in batch:
                        pipe.publish(channel, envelope)
                    await pipe.execute()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._needs_resync = True
                    if attempt + 1 < attempts:
                        await asyncio.sleep(0.2 * (attempt + 1))
                    else:
                        # Local editing must keep working if Redis hiccups;
                        # the resync after recovery reconciles what was lost.
                        logger.exception("Redis publish failed; dropping %d message(s)", len(batch))
                else:
                    if self._needs_resync:
                        self._schedule_resync()
                    break

    def _schedule_resync(self) -> None:
        if self._resync_handler is None or (self._resyncer is not None and not self._resyncer.done()):
            return
        self._resyncer = asyncio.ensure_future(self._resync())

    async def _resync(self) -> None:
        await asyncio.sleep(RESYNC_DELAY_SECONDS)
        self._needs_resync = False
        for doc_id in list(self._handlers):
            try:
                await self._resync_handler(doc_id)
            except Exception:
                logger.exception("Resync failed for doc %s", doc_id)

    async def _read_loop(self) -> None:
        failed = False
        while True:
            try:
                message = await self._pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Redis pubsub read failed; retrying")
                failed = True
                self._needs_resync = True
                await asyncio.sleep(1.0)
                continue
            if failed:
                failed = False
                self._schedule_resync()
            if message is None or message.get("type") != "message":
                # get_message returns promptly with None on fakeredis/idle
                # connections; yield so we never spin.
                await asyncio.sleep(0)
                continue
            await self._dispatch(message)

    async def _dispatch(self, message: dict) -> None:
        channel = message["channel"]
        if isinstance(channel, bytes):
            channel = channel.decode("utf-8")
        data = message["data"]
        if not isinstance(data, bytes) or len(data) <= PID_LENGTH:
            return
        if data[:PID_LENGTH].decode("ascii", "replace") == self.process_id:
            return
        if channel == CONTROL_CHANNEL:
            await self._dispatch_control(data[PID_LENGTH : PID_LENGTH + 1], data[PID_LENGTH + 1 :])
            return
        doc_id = channel[len("collab:room:") :]
        handler = self._handlers.get(doc_id)
        if handler is None:
            return
        try:
            await handler(data[PID_LENGTH : PID_LENGTH + 1], data[PID_LENGTH + 1 :])
        except Exception:
            logger.exception("Error handling remote message for doc %s", doc_id)

    async def _dispatch_control(self, kind: bytes, payload: bytes) -> None:
        try:
            body = json.loads(payload)
            if kind == b"k" and self._kick_handler is not None:
                await self._kick_handler([int(i) for i in body["user_ids"]])
            elif kind == b"o" and self._control_handler is not None:
                await self._control_handler(int(body["user_id"]), str(body["doc_id"]))
        except Exception:
            logger.exception("Error handling control message")

    async def set_presence(self, user_id: int, doc_id: str, display_name: str) -> None:
        self._local_presence[user_id] = (doc_id, display_name)
        await self._write_presence(user_id, doc_id, display_name)

    async def _write_presence(self, user_id: int, doc_id: str, display_name: str) -> None:
        value = json.dumps({"doc_id": doc_id, "display_name": display_name})
        try:
            await self._redis.set(self._presence_key(user_id), value, ex=PRESENCE_TTL_SECONDS)
        except Exception:
            logger.exception("Redis presence write failed for user %s", user_id)

    async def clear_presence(self, user_id: int) -> None:
        self._local_presence.pop(user_id, None)
        try:
            await self._redis.delete(self._presence_key(user_id))
        except Exception:
            logger.exception("Redis presence delete failed for user %s", user_id)

    async def _refresh_presence_loop(self) -> None:
        while True:
            await asyncio.sleep(PRESENCE_REFRESH_SECONDS)
            for user_id, (doc_id, display_name) in list(self._local_presence.items()):
                await self._write_presence(user_id, doc_id, display_name)

    async def list_presence(self) -> dict[int, tuple[str, str]]:
        result: dict[int, tuple[str, str]] = {}

        async def scan() -> None:
            async for key in self._redis.scan_iter(match="collab:presence:*"):
                raw = await self._redis.get(key)
                if raw is None:
                    continue
                key_str = key.decode("utf-8") if isinstance(key, bytes) else key
                user_id = int(key_str.rsplit(":", 1)[1])
                entry = json.loads(raw)
                result[user_id] = (entry["doc_id"], entry["display_name"])

        try:
            # Degrade fast to local-only presence when Redis is unreachable.
            await asyncio.wait_for(scan(), PRESENCE_LIST_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.warning("Redis presence listing timed out")
        except Exception:
            logger.exception("Redis presence listing failed")
        return result


def create_broadcaster() -> Broadcaster:
    url = os.environ.get(REDIS_URL_ENV)
    if not url:
        return InMemoryBroadcaster()
    import redis.asyncio as aioredis
    from redis.asyncio.retry import Retry
    from redis.backoff import ExponentialBackoff
    from redis.exceptions import ConnectionError as RedisConnectionError
    from redis.exceptions import TimeoutError as RedisTimeoutError

    # Retry + health checks so a stale pooled connection (e.g. after a Redis
    # restart) is replaced transparently instead of failing the first command.
    return RedisBroadcaster(
        aioredis.from_url(
            url,
            health_check_interval=10,
            socket_connect_timeout=2,
            retry=Retry(ExponentialBackoff(cap=0.5, base=0.05), 3),
            retry_on_error=[RedisConnectionError, RedisTimeoutError, ConnectionError, TimeoutError],
        )
    )
