import asyncio
import json

import httpx
import pytest
import pytest_asyncio
import uvicorn
import websockets
from pycrdt import Doc, Text, YMessageType, create_sync_message, handle_sync_message

from server.app import chat_service
from server.app.db import AsyncSessionLocal
from server.app.main import app
from server.app.routers.sync import MESSAGE_TYPE_CHAT, TEXT_KEY
from server.app.user_service import create_user

CHAT_PORT = 8766


# --- chat_service unit tests (no websocket involved) ---


async def test_create_and_list_messages(normal_user):
    async with AsyncSessionLocal() as db:
        m1 = await chat_service.create_message(db, doc_id="doc-1", user_id=normal_user.id, display_name="Alice", body="hi")
        m2 = await chat_service.create_message(db, doc_id="doc-1", user_id=normal_user.id, display_name="Alice", body="there")
        # A message in a different doc should never show up in doc-1's history.
        await chat_service.create_message(db, doc_id="doc-2", user_id=normal_user.id, display_name="Alice", body="wrong doc")

        messages = await chat_service.list_messages(db, "doc-1")
        assert [m.id for m in messages] == [m1.id, m2.id]
        assert [m.body for m in messages] == ["hi", "there"]


async def test_list_messages_pagination_with_before_id(normal_user):
    async with AsyncSessionLocal() as db:
        ids = []
        for i in range(5):
            m = await chat_service.create_message(
                db, doc_id="doc-1", user_id=normal_user.id, display_name="Alice", body=f"msg {i}"
            )
            ids.append(m.id)

        page = await chat_service.list_messages(db, "doc-1", limit=2, before_id=ids[3])
        assert [m.id for m in page] == [ids[1], ids[2]]


async def test_list_messages_before_unknown_id_raises(normal_user):
    async with AsyncSessionLocal() as db:
        with pytest.raises(chat_service.MessageNotFoundError):
            await chat_service.list_messages(db, "doc-1", before_id="does-not-exist")


# --- REST history endpoint ---


async def test_get_chat_history_empty(user_client):
    resp = await user_client.post("/api/documents", json={"name": "doc.txt", "parent_id": None})
    doc_id = resp.json()["id"]

    resp = await user_client.get(f"/api/documents/{doc_id}/chat")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_get_chat_history_nonexistent_doc_404s(user_client):
    resp = await user_client.get("/api/documents/nonexistent-id/chat")
    assert resp.status_code == 404


# --- Real end-to-end test over a live uvicorn server + real `websockets` client ---
# Mirrors test_sync.py's live_server pattern: pycrdt's Rust-backed objects
# aren't safe across TestClient's background-thread anyio portal, so chat
# frame delivery (which piggybacks on the same room/connections as doc sync)
# needs the same real-event-loop live server setup.


@pytest_asyncio.fixture
async def live_server():
    config = uvicorn.Config(app, host="127.0.0.1", port=CHAT_PORT, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.ensure_future(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    yield server
    server.should_exit = True
    await task


async def _login_get_cookie(base_url: str, username: str, password: str) -> str:
    async with httpx.AsyncClient(base_url=base_url) as client:
        resp = await client.post("/api/login", json={"username": username, "password": password})
        assert resp.status_code == 200
        return resp.cookies["session_token"]


async def _create_doc(base_url: str, cookie: str, name: str) -> str:
    async with httpx.AsyncClient(base_url=base_url, cookies={"session_token": cookie}) as client:
        resp = await client.post("/api/documents", json={"name": name, "parent_id": None})
        assert resp.status_code == 201
        return resp.json()["id"]


async def _connect_and_sync(base_url: str, cookie: str, doc_id: str):
    client_doc = Doc()
    client_text = client_doc.get(TEXT_KEY, type=Text)
    ws_url = f"ws://127.0.0.1:{CHAT_PORT}/ws/doc/{doc_id}"
    ws = await websockets.connect(ws_url, additional_headers={"Cookie": f"session_token={cookie}"})
    server_step1 = await ws.recv()
    our_step2_reply = handle_sync_message(server_step1[1:], client_doc)
    await ws.send(our_step2_reply)
    await ws.send(create_sync_message(client_doc))
    for _ in range(5):
        frame = await asyncio.wait_for(ws.recv(), timeout=5)
        if frame[0] == YMessageType.SYNC:
            handle_sync_message(frame[1:], client_doc)
            break
    return ws, client_doc, client_text


def _chat_frame(body: str) -> bytes:
    return bytes([MESSAGE_TYPE_CHAT]) + json.dumps({"body": body}).encode("utf-8")


async def _recv_chat_frame(ws, timeout=5) -> dict:
    for _ in range(10):
        frame = await asyncio.wait_for(ws.recv(), timeout=timeout)
        if frame[0] == MESSAGE_TYPE_CHAT:
            return json.loads(frame[1:].decode("utf-8"))
    raise AssertionError("no chat frame received")


async def _bootstrap_two_users_one_doc(base_url: str):
    async with AsyncSessionLocal() as db:
        await create_user(db, "alice", "Alice", "alicepass123", is_admin=False)
        await create_user(db, "bob", "Bob", "bobpass123", is_admin=False)

    cookie_a = await _login_get_cookie(base_url, "alice", "alicepass123")
    doc_id = await _create_doc(base_url, cookie_a, "chat.txt")
    cookie_b = await _login_get_cookie(base_url, "bob", "bobpass123")
    return doc_id, cookie_a, cookie_b


async def test_chat_message_broadcasts_to_other_client_with_server_stamped_identity(live_server):
    base_url = f"http://127.0.0.1:{CHAT_PORT}"
    doc_id, cookie_a, cookie_b = await _bootstrap_two_users_one_doc(base_url)

    ws_a, _, _ = await _connect_and_sync(base_url, cookie_a, doc_id)
    ws_b, _, _ = await _connect_and_sync(base_url, cookie_b, doc_id)
    try:
        # Alice sends only {body} - no user identity in the payload at all -
        # proving the server stamps sender identity itself rather than
        # trusting anything the client could claim.
        await ws_a.send(_chat_frame("hello from alice"))

        received = await _recv_chat_frame(ws_b)
        assert received["body"] == "hello from alice"
        assert received["displayName"] == "Alice"
        assert received["docId"] == doc_id

        # The sender also gets their own message echoed back (consistent
        # with how awareness broadcasts to all clients including itself),
        # so their own chat panel renders it without a local-echo hack.
        echoed = await _recv_chat_frame(ws_a)
        assert echoed["body"] == "hello from alice"
        assert echoed["id"] == received["id"]
    finally:
        await ws_a.close()
        await ws_b.close()


async def test_chat_message_persists_and_is_retrievable_via_rest(live_server):
    base_url = f"http://127.0.0.1:{CHAT_PORT}"
    doc_id, cookie_a, cookie_b = await _bootstrap_two_users_one_doc(base_url)

    ws_a, _, _ = await _connect_and_sync(base_url, cookie_a, doc_id)
    ws_b, _, _ = await _connect_and_sync(base_url, cookie_b, doc_id)
    try:
        await ws_a.send(_chat_frame("persisted message"))
        await _recv_chat_frame(ws_b)
        await _recv_chat_frame(ws_a)
    finally:
        await ws_a.close()
        await ws_b.close()

    await asyncio.sleep(0.2)

    async with httpx.AsyncClient(base_url=base_url, cookies={"session_token": cookie_a}) as client:
        resp = await client.get(f"/api/documents/{doc_id}/chat")
        assert resp.status_code == 200
        bodies = [m["body"] for m in resp.json()]
        assert bodies == ["persisted message"]


async def test_chat_message_with_no_other_recipients_is_dropped(live_server):
    base_url = f"http://127.0.0.1:{CHAT_PORT}"
    async with AsyncSessionLocal() as db:
        await create_user(db, "alice", "Alice", "alicepass123", is_admin=False)
    cookie_a = await _login_get_cookie(base_url, "alice", "alicepass123")
    doc_id = await _create_doc(base_url, cookie_a, "solo.txt")

    ws_a, _, _ = await _connect_and_sync(base_url, cookie_a, doc_id)
    try:
        # Alone in the doc - server-side defense in depth should refuse to
        # persist/broadcast even though the client should already prevent
        # sending in this situation.
        await ws_a.send(_chat_frame("shouting into the void"))
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(ws_a.recv(), timeout=1)
    finally:
        await ws_a.close()

    await asyncio.sleep(0.2)

    async with httpx.AsyncClient(base_url=base_url, cookies={"session_token": cookie_a}) as client:
        resp = await client.get(f"/api/documents/{doc_id}/chat")
        assert resp.json() == []


async def test_malformed_chat_frame_is_dropped_without_crashing_connection(live_server):
    base_url = f"http://127.0.0.1:{CHAT_PORT}"
    doc_id, cookie_a, cookie_b = await _bootstrap_two_users_one_doc(base_url)

    ws_a, doc_a, text_a = await _connect_and_sync(base_url, cookie_a, doc_id)
    ws_b, _, _ = await _connect_and_sync(base_url, cookie_b, doc_id)
    try:
        await ws_a.send(bytes([MESSAGE_TYPE_CHAT]) + b"not valid json")

        # Connection must still be alive and process ordinary sync traffic
        # afterwards - a malformed frame shouldn't take down the whole room.
        await ws_a.send(_chat_frame("still works"))
        received = await _recv_chat_frame(ws_b)
        assert received["body"] == "still works"
    finally:
        await ws_a.close()
        await ws_b.close()
