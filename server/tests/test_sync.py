import asyncio
import gc

import pytest
import pytest_asyncio
import uvicorn
import websockets
from pycrdt import Doc, Text, YMessageType, create_sync_message, create_update_message, handle_sync_message
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from server.app import docstore
from server.app.main import app
from server.app.routers.sync import CLOSE_NOT_FOUND, CLOSE_REPLACED_BY_NEWER_SESSION, CLOSE_UNAUTHORIZED, TEXT_KEY

SYNC_PORT = 8765


@pytest.fixture
def sync_client():
    with TestClient(app) as client:
        yield client
        # TestClient drives the app through a background-thread anyio portal.
        # pycrdt's Rust-backed Doc/Text/Subscription objects created on that
        # thread aren't safe to finalize from a different thread, which is
        # what happens if Python's GC gets around to them later while some
        # other test's event loop is running. Collecting here, while the
        # portal thread is still alive, finalizes them on the correct thread
        # instead of leaving it to chance.
        #
        # This can still emit a PytestUnraisableExceptionWarning for the one
        # Subscription our own code doesn't control: pycrdt_websocket.YRoom
        # subscribes its Awareness in __init__ (`awareness.observe(...)`) but
        # never unobserves/drops it anywhere in the library, including in
        # YRoom.stop(). Our own subscription (ytext.observe in sync.py) *is*
        # explicitly dropped on room teardown - this is an upstream gap, not
        # one reachable from application code.
        gc.collect()


def _login(sync_client: TestClient, username: str, password: str) -> None:
    resp = sync_client.post("/api/login", json={"username": username, "password": password})
    assert resp.status_code == 200


def test_websocket_rejects_no_cookie(sync_client, normal_user):
    # The route always accepts the WS handshake before closing (see sync.py:
    # some ASGI servers won't deliver a custom close code otherwise), so the
    # rejection surfaces as a close frame received just after connecting,
    # not as a failure to connect at all.
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with sync_client.websocket_connect("/ws/doc/some-doc-id") as ws:
            ws.receive_bytes()
    assert exc_info.value.code == CLOSE_UNAUTHORIZED


def test_websocket_rejects_nonexistent_doc(sync_client, normal_user):
    _login(sync_client, "alice", "alicepass123")
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with sync_client.websocket_connect("/ws/doc/nonexistent-id") as ws:
            ws.receive_bytes()
    assert exc_info.value.code == CLOSE_NOT_FOUND


def test_websocket_rejects_folder_doc_id(sync_client, normal_user):
    _login(sync_client, "alice", "alicepass123")
    resp = sync_client.post("/api/folders", json={"name": "Folder", "parent_id": None})
    folder_id = resp.json()["id"]

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with sync_client.websocket_connect(f"/ws/doc/{folder_id}") as ws:
            ws.receive_bytes()
    assert exc_info.value.code == CLOSE_NOT_FOUND


def test_websocket_seeds_existing_content(sync_client, normal_user):
    _login(sync_client, "alice", "alicepass123")

    resp = sync_client.post("/api/documents", json={"name": "doc.txt", "parent_id": None})
    doc_id = resp.json()["id"]
    resp = sync_client.put(f"/api/documents/{doc_id}/content", json={"content": "hello from disk"})
    assert resp.status_code == 200

    client_doc = Doc()
    client_text = client_doc.get(TEXT_KEY, type=Text)

    with sync_client.websocket_connect(f"/ws/doc/{doc_id}") as ws:
        server_step1 = ws.receive_bytes()
        assert server_step1[0] == YMessageType.SYNC

        # Standard Yjs handshake: reply to the server's SYNC_STEP1 with our own
        # SYNC_STEP2 (telling it what we have, i.e. nothing), and separately
        # send our own SYNC_STEP1 so the server replies with a SYNC_STEP2
        # carrying its actual (seeded) content back to us. create_sync_message
        # and handle_sync_message's return value are both already fully
        # framed (leading YMessageType.SYNC byte included) - no manual
        # envelope wrapping needed.
        our_step2_reply = handle_sync_message(server_step1[1:], client_doc)
        assert our_step2_reply is not None
        ws.send_bytes(our_step2_reply)
        ws.send_bytes(create_sync_message(client_doc))

        # The room also broadcasts its own awareness state on connect; skip
        # any non-SYNC frames while waiting for the SYNC_STEP2 reply.
        for _ in range(5):
            frame = ws.receive_bytes()
            if frame[0] == YMessageType.SYNC:
                handle_sync_message(frame[1:], client_doc)
                if str(client_text) == "hello from disk":
                    break

    assert str(client_text) == "hello from disk"


# --- Real end-to-end test over a live uvicorn server + real `websockets` client ---
#
# starlette.testclient.TestClient drives the ASGI app through a background-thread
# anyio portal; pycrdt's Rust-backed Doc/Subscription objects are not safe to hand
# across that thread boundary (observed: rooms silently vanishing from the
# registry, "Subscription is unsendable" warnings). Running a real uvicorn server
# in-process on the same asyncio event loop as the test avoids that entirely and
# is a closer match to how the frontend actually talks to this route.


@pytest_asyncio.fixture
async def live_server():
    config = uvicorn.Config(app, host="127.0.0.1", port=SYNC_PORT, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.ensure_future(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    yield server
    server.should_exit = True
    await task


async def _login_get_cookie(base_url: str, username: str, password: str) -> str:
    import httpx

    async with httpx.AsyncClient(base_url=base_url) as client:
        resp = await client.post("/api/login", json={"username": username, "password": password})
        assert resp.status_code == 200
        return resp.cookies["session_token"]


async def test_live_websocket_persists_edit_to_disk(live_server, normal_user):
    base_url = f"http://127.0.0.1:{SYNC_PORT}"
    cookie = await _login_get_cookie(base_url, "alice", "alicepass123")

    import httpx

    async with httpx.AsyncClient(base_url=base_url, cookies={"session_token": cookie}) as client:
        resp = await client.post("/api/documents", json={"name": "live.txt", "parent_id": None})
        doc_id = resp.json()["id"]
        resp = await client.put(f"/api/documents/{doc_id}/content", json={"content": "seeded content"})
        assert resp.status_code == 200

    client_doc = Doc()
    client_text = client_doc.get(TEXT_KEY, type=Text)

    ws_url = f"ws://127.0.0.1:{SYNC_PORT}/ws/doc/{doc_id}"
    async with websockets.connect(ws_url, additional_headers={"Cookie": f"session_token={cookie}"}) as ws:
        server_step1 = await ws.recv()
        our_step2_reply = handle_sync_message(server_step1[1:], client_doc)
        await ws.send(our_step2_reply)
        await ws.send(create_sync_message(client_doc))

        for _ in range(5):
            frame = await asyncio.wait_for(ws.recv(), timeout=5)
            if frame[0] == YMessageType.SYNC:
                handle_sync_message(frame[1:], client_doc)
                if str(client_text) == "seeded content":
                    break

        assert str(client_text) == "seeded content"

        captured_updates = []
        subscription = client_doc.observe(lambda event: captured_updates.append(event.update))
        with client_doc.transaction():
            client_text += " plus live edit"
        # Drop explicitly rather than letting GC finalize it: pycrdt's Rust
        # Subscription is not safe to drop from an arbitrary GC thread, which
        # otherwise surfaces as a PytestUnraisableExceptionWarning at
        # interpreter shutdown even though it doesn't affect this test's
        # assertions.
        subscription.drop()

        await ws.send(create_update_message(captured_updates[0]))

        # Barrier: round-trip once more so we know the server processed our
        # update before we close the connection below.
        await ws.send(create_sync_message(client_doc))
        for _ in range(5):
            frame = await asyncio.wait_for(ws.recv(), timeout=5)
            if frame[0] == YMessageType.SYNC and frame[1] == 1:
                break

    # Give the route's disconnect-triggered flush a moment to run.
    await asyncio.sleep(0.5)

    async with httpx.AsyncClient(base_url=base_url, cookies={"session_token": cookie}) as client:
        resp = await client.get(f"/api/documents/{doc_id}/content")
        assert resp.status_code == 200
        assert resp.json()["content"] == "seeded content plus live edit"


# --- Multi-client / one-doc-per-user enforcement tests ---
#
# These all drive the live uvicorn server with real `websockets` connections,
# for the same reason test_live_websocket_persists_edit_to_disk does: pycrdt's
# Rust objects aren't safe across TestClient's background-thread portal.


async def _create_doc(base_url: str, cookie: str, name: str, content: str = "") -> str:
    import httpx

    async with httpx.AsyncClient(base_url=base_url, cookies={"session_token": cookie}) as client:
        resp = await client.post("/api/documents", json={"name": name, "parent_id": None})
        assert resp.status_code == 201
        doc_id = resp.json()["id"]
        if content:
            resp = await client.put(f"/api/documents/{doc_id}/content", json={"content": content})
            assert resp.status_code == 200
        return doc_id


async def _connect_and_sync(base_url: str, cookie: str, doc_id: str):
    client_doc = Doc()
    client_text = client_doc.get(TEXT_KEY, type=Text)
    ws_url = f"ws://127.0.0.1:{SYNC_PORT}/ws/doc/{doc_id}"
    ws = await websockets.connect(ws_url, additional_headers={"Cookie": f"session_token={cookie}"})
    server_step1 = await ws.recv()
    our_step2_reply = handle_sync_message(server_step1[1:], client_doc)
    await ws.send(our_step2_reply)
    await ws.send(create_sync_message(client_doc))
    # Server also broadcasts its own awareness frame on connect; skip
    # non-SYNC frames while waiting for the SYNC_STEP2 reply carrying its
    # actual content back to us (mirrors test_websocket_seeds_existing_content).
    for _ in range(5):
        frame = await asyncio.wait_for(ws.recv(), timeout=5)
        if frame[0] == YMessageType.SYNC:
            handle_sync_message(frame[1:], client_doc)
            break
    return ws, client_doc, client_text


async def test_second_doc_force_closes_first_connection(live_server, normal_user):
    base_url = f"http://127.0.0.1:{SYNC_PORT}"
    cookie = await _login_get_cookie(base_url, "alice", "alicepass123")

    doc_a = await _create_doc(base_url, cookie, "a.txt", "content a")
    doc_b = await _create_doc(base_url, cookie, "b.txt", "content b")

    ws_a, doc_a_ydoc, text_a = await _connect_and_sync(base_url, cookie, doc_a)

    captured_updates = []
    subscription = doc_a_ydoc.observe(lambda event: captured_updates.append(event.update))
    with doc_a_ydoc.transaction():
        text_a += " plus edit before being replaced"
    subscription.drop()
    await ws_a.send(create_update_message(captured_updates[0]))
    # Barrier: round-trip so we know the server applied our update before we
    # open the second connection below.
    await ws_a.send(create_sync_message(doc_a_ydoc))
    for _ in range(5):
        frame = await asyncio.wait_for(ws_a.recv(), timeout=5)
        if frame[0] == YMessageType.SYNC and frame[1] == 1:
            break

    ws_b, _doc_b_ydoc, _text_b = await _connect_and_sync(base_url, cookie, doc_b)
    try:
        with pytest.raises(websockets.exceptions.ConnectionClosed) as exc_info:
            await asyncio.wait_for(ws_a.recv(), timeout=5)
        assert exc_info.value.rcvd.code == CLOSE_REPLACED_BY_NEWER_SESSION
    finally:
        await ws_b.close()

    await asyncio.sleep(0.5)

    import httpx

    async with httpx.AsyncClient(base_url=base_url, cookies={"session_token": cookie}) as client:
        resp = await client.get(f"/api/documents/{doc_a}/content")
        assert resp.status_code == 200
        assert resp.json()["content"] == "content a plus edit before being replaced"


async def test_same_doc_reconnect_does_not_force_close(live_server, normal_user):
    base_url = f"http://127.0.0.1:{SYNC_PORT}"
    cookie = await _login_get_cookie(base_url, "alice", "alicepass123")
    doc_id = await _create_doc(base_url, cookie, "solo.txt", "hello")

    ws_1, _doc1, _text1 = await _connect_and_sync(base_url, cookie, doc_id)
    ws_2, _doc2, _text2 = await _connect_and_sync(base_url, cookie, doc_id)

    # Neither connection should be force-closed: both are the same user on
    # the same doc_id, which is a legitimate multi-tab/reconnect scenario.
    for ws in (ws_1, ws_2):
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(ws.recv(), timeout=0.5)

    await ws_1.close()
    await ws_2.close()


async def test_different_users_same_doc_do_not_interfere(live_server, normal_user):
    base_url = f"http://127.0.0.1:{SYNC_PORT}"
    from server.app.db import AsyncSessionLocal
    from server.app.user_service import create_user

    async with AsyncSessionLocal() as db:
        await create_user(db, "bob", "Bob", "bobpass123", is_admin=False)

    cookie_alice = await _login_get_cookie(base_url, "alice", "alicepass123")
    cookie_bob = await _login_get_cookie(base_url, "bob", "bobpass123")

    doc_id = await _create_doc(base_url, cookie_alice, "shared.txt", "start")

    ws_alice, doc_alice, text_alice = await _connect_and_sync(base_url, cookie_alice, doc_id)
    ws_bob, doc_bob, text_bob = await _connect_and_sync(base_url, cookie_bob, doc_id)

    captured = []
    subscription = doc_alice.observe(lambda event: captured.append(event.update))
    with doc_alice.transaction():
        text_alice += " from alice"
    subscription.drop()
    await ws_alice.send(create_update_message(captured[0]))

    converged = False
    for _ in range(10):
        frame = await asyncio.wait_for(ws_bob.recv(), timeout=5)
        if frame[0] == YMessageType.SYNC:
            handle_sync_message(frame[1:], doc_bob)
            if str(text_bob) == "start from alice":
                converged = True
                break
    assert converged

    await ws_alice.close()
    await ws_bob.close()


async def test_different_users_different_docs_no_force_close(live_server, normal_user):
    base_url = f"http://127.0.0.1:{SYNC_PORT}"
    from server.app.db import AsyncSessionLocal
    from server.app.user_service import create_user

    async with AsyncSessionLocal() as db:
        await create_user(db, "carol", "Carol", "carolpass123", is_admin=False)

    cookie_alice = await _login_get_cookie(base_url, "alice", "alicepass123")
    cookie_carol = await _login_get_cookie(base_url, "carol", "carolpass123")

    doc_alice_id = await _create_doc(base_url, cookie_alice, "alice-doc.txt", "a")
    doc_carol_id = await _create_doc(base_url, cookie_carol, "carol-doc.txt", "c")

    ws_alice, _da, _ta = await _connect_and_sync(base_url, cookie_alice, doc_alice_id)
    ws_carol, _dc, _tc = await _connect_and_sync(base_url, cookie_carol, doc_carol_id)

    for ws in (ws_alice, ws_carol):
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(ws.recv(), timeout=0.5)

    await ws_alice.close()
    await ws_carol.close()


async def test_multi_client_room_teardown_only_when_empty(live_server, normal_user):
    base_url = f"http://127.0.0.1:{SYNC_PORT}"
    from server.app.db import AsyncSessionLocal
    from server.app.user_service import create_user
    from server.app.routers import sync as sync_module

    async with AsyncSessionLocal() as db:
        await create_user(db, "dave", "Dave", "davepass123", is_admin=False)

    cookie_alice = await _login_get_cookie(base_url, "alice", "alicepass123")
    cookie_dave = await _login_get_cookie(base_url, "dave", "davepass123")

    doc_id = await _create_doc(base_url, cookie_alice, "room.txt", "room content")

    ws_alice, _da, _ta = await _connect_and_sync(base_url, cookie_alice, doc_id)
    ws_dave, _dd, _td = await _connect_and_sync(base_url, cookie_dave, doc_id)

    assert doc_id in sync_module.websocket_server.rooms

    await ws_alice.close()
    await asyncio.sleep(0.5)

    # One of two clients disconnected; the room must still exist since Dave
    # is still connected - this is the multi-client correctness the Phase-4
    # single-client teardown logic was never actually exercised against.
    assert doc_id in sync_module.websocket_server.rooms

    await ws_dave.close()
    await asyncio.sleep(0.5)

    assert doc_id not in sync_module.websocket_server.rooms
