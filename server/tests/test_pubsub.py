import asyncio
import json

import fakeredis
import httpx
import pytest
import pytest_asyncio
import uvicorn
import websockets
from pycrdt import Doc, Text, YMessageType, create_sync_message, create_update_message, handle_sync_message

from server.app import pubsub
from server.app.main import app
from server.app.routers import sync as sync_module
from server.app.routers.sync import MESSAGE_TYPE_CHAT, TEXT_KEY

PUBSUB_PORT = 8767


async def _eventually(predicate, timeout=5.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()


def _fake_broadcaster(server, process_id=None):
    return pubsub.RedisBroadcaster(fakeredis.FakeAsyncRedis(server=server), process_id=process_id)


# --- Broadcaster unit tests ---


def test_create_broadcaster_defaults_to_in_memory(monkeypatch):
    monkeypatch.delenv(pubsub.REDIS_URL_ENV, raising=False)
    b = pubsub.create_broadcaster()
    assert isinstance(b, pubsub.InMemoryBroadcaster)
    assert not b.enabled


async def test_in_memory_broadcaster_is_noop():
    b = pubsub.InMemoryBroadcaster()
    await b.start()
    await b.publish("doc", pubsub.KIND_UPDATE, b"x")
    await b.set_presence(1, "doc", "Alice")
    assert await b.list_presence() == {}
    await b.stop()


async def test_redis_broadcaster_delivers_to_peers_but_not_self():
    server = fakeredis.FakeServer()
    a, b = _fake_broadcaster(server), _fake_broadcaster(server)
    got_a, got_b = [], []

    async def handler_a(kind, payload):
        got_a.append((kind, payload))

    async def handler_b(kind, payload):
        got_b.append((kind, payload))

    await a.start()
    await b.start()
    await a.subscribe("d1", handler_a)
    await b.subscribe("d1", handler_b)
    await a.publish("d1", pubsub.KIND_CHAT, b"hello")
    await a.publish("other-room", pubsub.KIND_CHAT, b"ignored")

    assert await _eventually(lambda: got_b == [(pubsub.KIND_CHAT, b"hello")])
    await asyncio.sleep(0.2)
    assert got_a == []  # no echo to the publishing process
    assert got_b == [(pubsub.KIND_CHAT, b"hello")]  # per-room channels

    await a.stop()
    await b.stop()


async def test_redis_broadcaster_presence_shared_and_cleared():
    server = fakeredis.FakeServer()
    a, b = _fake_broadcaster(server), _fake_broadcaster(server)
    await a.start()
    await b.start()
    await a.set_presence(7, "doc-x", "Alice")
    assert await b.list_presence() == {7: ("doc-x", "Alice")}
    await a.clear_presence(7)
    assert await b.list_presence() == {}
    await a.stop()
    await b.stop()


async def test_burst_publishes_arrive_in_order_via_single_worker():
    server = fakeredis.FakeServer()
    a, b = _fake_broadcaster(server), _fake_broadcaster(server)
    got = []

    async def handler(kind, payload):
        got.append(payload)

    await a.start()
    await b.start()
    await b.subscribe("d1", handler)
    for i in range(500):
        a.publish_nowait("d1", pubsub.KIND_UPDATE, str(i).encode())
    assert await _eventually(lambda: len(got) == 500, timeout=10)
    assert got == [str(i).encode() for i in range(500)]
    await a.stop()
    await b.stop()


async def test_publish_retries_then_resyncs_after_failure(monkeypatch):
    monkeypatch.setattr(pubsub, "RESYNC_DELAY_SECONDS", 0.05)
    server = fakeredis.FakeServer()
    a = _fake_broadcaster(server)
    resynced = []

    async def on_resync(doc_id):
        resynced.append(doc_id)

    async def noop(kind, payload):
        pass

    a.set_resync_handler(on_resync)
    await a.start()
    await a.subscribe("d1", noop)

    real_pipeline = a._redis.pipeline
    calls = {"n": 0}

    def flaky_pipeline(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("stale connection")
        return real_pipeline(*args, **kwargs)

    monkeypatch.setattr(a._redis, "pipeline", flaky_pipeline)
    a.publish_nowait("d1", pubsub.KIND_UPDATE, b"x")
    assert await _eventually(lambda: resynced == ["d1"])
    assert calls["n"] >= 2  # first attempt failed, retry succeeded
    await a.stop()


async def test_control_message_reaches_other_process_only():
    server = fakeredis.FakeServer()
    a, b = _fake_broadcaster(server), _fake_broadcaster(server)
    got_a, got_b = [], []

    async def ca(user_id, doc_id):
        got_a.append((user_id, doc_id))

    async def cb(user_id, doc_id):
        got_b.append((user_id, doc_id))

    a.set_control_handler(ca)
    b.set_control_handler(cb)
    await a.start()
    await b.start()
    await a.publish_user_opened(5, "doc-1")
    assert await _eventually(lambda: got_b == [(5, "doc-1")])
    assert got_a == []
    await a.stop()
    await b.stop()


async def test_list_presence_times_out_fast(monkeypatch):
    monkeypatch.setattr(pubsub, "PRESENCE_LIST_TIMEOUT_SECONDS", 0.1)
    b = _fake_broadcaster(fakeredis.FakeServer())

    async def hang(*args, **kwargs):
        await asyncio.sleep(10)

    monkeypatch.setattr(b._redis, "get", hang)
    await b._redis.set("collab:presence:x:1", json.dumps({"doc_id": "d", "display_name": "n"}))
    start = asyncio.get_event_loop().time()
    assert await b.list_presence() == {}
    assert asyncio.get_event_loop().time() - start < 2


def test_seed_client_id_is_deterministic_and_content_sensitive():
    assert sync_module._seed_client_id("d", "abc") == sync_module._seed_client_id("d", "abc")
    assert sync_module._seed_client_id("d", "abc") != sync_module._seed_client_id("d", "abd")
    assert sync_module._seed_client_id("d", "abc") != sync_module._seed_client_id("e", "abc")


# --- Two-process simulation over the real sync route ---
#
# The app under test is "process 1" (module-global sync.broadcaster swapped for
# a Redis one backed by fakeredis); the test's own RedisBroadcaster plays
# "process 2" on the same fake Redis server.


@pytest_asyncio.fixture
async def redis_live_server(monkeypatch):
    fake_server = fakeredis.FakeServer()
    monkeypatch.setattr(sync_module, "broadcaster", _fake_broadcaster(fake_server))
    config = uvicorn.Config(app, host="127.0.0.1", port=PUBSUB_PORT, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.ensure_future(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    peer = _fake_broadcaster(fake_server)
    await peer.start()
    yield peer
    await peer.stop()
    server.should_exit = True
    await task


async def _login_and_create_doc(content: str):
    base_url = f"http://127.0.0.1:{PUBSUB_PORT}"
    async with httpx.AsyncClient(base_url=base_url) as client:
        resp = await client.post("/api/login", json={"username": "alice", "password": "alicepass123"})
        assert resp.status_code == 200
        cookie = resp.cookies["session_token"]
        resp = await client.post("/api/documents", json={"name": "r.txt", "parent_id": None})
        doc_id = resp.json()["id"]
        resp = await client.put(f"/api/documents/{doc_id}/content", json={"content": content})
        assert resp.status_code == 200
    return cookie, doc_id


async def _connect(cookie, doc_id):
    client_doc = Doc()
    client_text = client_doc.get(TEXT_KEY, type=Text)
    ws = await websockets.connect(
        f"ws://127.0.0.1:{PUBSUB_PORT}/ws/doc/{doc_id}", additional_headers={"Cookie": f"session_token={cookie}"}
    )
    step1 = await ws.recv()
    await ws.send(handle_sync_message(step1[1:], client_doc))
    await ws.send(create_sync_message(client_doc))
    return ws, client_doc, client_text


async def _pump(ws, client_doc, frames, until, timeout=5.0):
    """Feed incoming frames into client_doc (SYNC) / frames (others) until `until()`."""
    deadline = asyncio.get_event_loop().time() + timeout
    while not until() and asyncio.get_event_loop().time() < deadline:
        try:
            frame = await asyncio.wait_for(ws.recv(), timeout=0.2)
        except asyncio.TimeoutError:
            continue
        if frame[0] == YMessageType.SYNC:
            handle_sync_message(frame[1:], client_doc)
        else:
            frames.append(frame)


async def test_joining_process_takes_peer_state_instead_of_disk(redis_live_server, normal_user):
    peer = redis_live_server
    cookie, doc_id = await _login_and_create_doc("stale disk")

    # "Process 2" already holds a live doc with newer, un-flushed content.
    peer_doc = Doc()
    peer_text = peer_doc.get(TEXT_KEY, type=Text)
    peer_text += "live peer text"
    requests = []

    async def peer_handler(kind, payload):
        if kind == pubsub.KIND_STATE_REQUEST:
            requests.append(1)
            await peer.publish(doc_id, pubsub.KIND_STATE, peer_doc.get_update())

    await peer.subscribe(doc_id, peer_handler)

    ws, client_doc, client_text = await _connect(cookie, doc_id)
    await _pump(ws, client_doc, [], lambda: str(client_text) == "live peer text")
    await asyncio.sleep(0.3)
    await _pump(ws, client_doc, [], lambda: False, timeout=0.5)
    # Exactly the peer's text: no duplicated/merged disk seed.
    assert str(client_text) == "live peer text"
    assert requests

    peer_subscription_updates = []

    async def collect(kind, payload):
        if kind == pubsub.KIND_UPDATE:
            peer_subscription_updates.append(payload)

    await peer.subscribe(doc_id, collect)

    # Local edit fans out to the peer process.
    captured = []
    sub = client_doc.observe(lambda event: captured.append(event.update))
    with client_doc.transaction():
        client_text += "!"
    sub.drop()
    await ws.send(create_update_message(captured[0]))
    assert await _eventually(lambda: len(peer_subscription_updates) >= 1)
    for update in peer_subscription_updates:
        peer_doc.apply_update(update)
    assert str(peer_text) == "live peer text!"

    await ws.close()
    await asyncio.sleep(0.5)


async def test_remote_update_awareness_and_chat_reach_local_client(redis_live_server, normal_user):
    peer = redis_live_server
    cookie, doc_id = await _login_and_create_doc("base")
    await peer.subscribe(doc_id, lambda kind, payload: asyncio.sleep(0))

    ws, client_doc, client_text = await _connect(cookie, doc_id)
    await _pump(ws, client_doc, [], lambda: str(client_text) == "base")
    assert str(client_text) == "base"

    # Presence is visible to other processes while the socket is open.
    assert await peer.list_presence() == {normal_user.id: (doc_id, "Alice")}

    # Remote doc update -> local client.
    remote_doc = Doc()
    remote_doc.apply_update(client_doc.get_update())
    remote_text = remote_doc.get(TEXT_KEY, type=Text)
    captured = []
    sub = remote_doc.observe(lambda event: captured.append(event.update))
    with remote_doc.transaction():
        remote_text += " +remote"
    sub.drop()
    await peer.publish(doc_id, pubsub.KIND_UPDATE, captured[0])

    # Remote chat -> local client, passed through verbatim.
    chat_frame = bytes([MESSAGE_TYPE_CHAT]) + json.dumps({"body": "hi from p2"}).encode()
    await peer.publish(doc_id, pubsub.KIND_CHAT, chat_frame)

    frames = []
    await _pump(ws, client_doc, frames, lambda: str(client_text) == "base +remote" and chat_frame in frames)
    assert str(client_text) == "base +remote"
    assert chat_frame in frames

    # A chat from the local client is delivered to the peer process.
    received = []

    async def collect(kind, payload):
        received.append((kind, payload))

    await peer.subscribe(doc_id, collect)
    await ws.send(bytes([MESSAGE_TYPE_CHAT]) + json.dumps({"body": "hello"}).encode())
    # Local room has only one client, but the peer-side presence list has no
    # *other* user on this doc, so this one is correctly dropped.
    await asyncio.sleep(0.3)
    assert not [r for r in received if r[0] == pubsub.KIND_CHAT]

    await ws.close()
    await asyncio.sleep(0.5)
    assert await peer.list_presence() == {}


async def test_revoked_users_kick_reaches_other_processes():
    server = fakeredis.FakeServer()
    a, b = _fake_broadcaster(server), _fake_broadcaster(server)
    kicked = []

    async def on_kick(user_ids):
        kicked.append(user_ids)

    b.set_kick_handler(on_kick)
    await a.start()
    await b.start()
    await a.publish_users_revoked([3, 4])
    assert await _eventually(lambda: kicked == [[3, 4]])
    await a.stop()
    await b.stop()
