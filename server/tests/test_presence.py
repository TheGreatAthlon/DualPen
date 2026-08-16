import gc
import time

import pytest
from starlette.testclient import TestClient

from server.app.main import app


@pytest.fixture
def sync_client():
    with TestClient(app) as client:
        yield client
        # See test_sync.py's sync_client fixture for why this gc.collect() is
        # needed: pycrdt's Rust-backed Subscription objects must be finalized
        # on the portal thread while it's still alive.
        gc.collect()


def _login(sync_client: TestClient, username: str, password: str) -> None:
    resp = sync_client.post("/api/login", json={"username": username, "password": password})
    assert resp.status_code == 200


def _presence_entries(sync_client: TestClient) -> list[dict]:
    # websocket_connect() returning only guarantees the ASGI accept
    # handshake completed; the route's own coroutine still has to run its DB
    # lookups before it registers the connection in _user_open_doc, on the
    # same portal-thread event loop a synchronous request right after can
    # race ahead of. A real client never queries presence in the same tick
    # it opens its own connection, so this tiny poll only compensates for
    # that test-harness ordering, not for anything in application code.
    for _ in range(20):
        resp = sync_client.get("/api/presence")
        assert resp.status_code == 200
        entries = resp.json()
        if entries:
            return entries
        time.sleep(0.05)
    return []


def test_presence_includes_ancestor_folder_path(sync_client, normal_user):
    # Both the nested-folder and root-level cases share one websocket
    # connection/test rather than two: _user_open_doc is keyed by user_id in
    # module-level state that outlives a single TestClient's portal thread,
    # so a second sync_client/websocket_connect for the same user in a
    # separate test can race the first connection's disconnect-triggered
    # cleanup and observe a stale/empty roster.
    _login(sync_client, "alice", "alicepass123")
    resp = sync_client.post("/api/folders", json={"name": "Work", "parent_id": None})
    work_id = resp.json()["id"]
    resp = sync_client.post("/api/folders", json={"name": "Projects", "parent_id": work_id})
    projects_id = resp.json()["id"]
    resp = sync_client.post("/api/documents", json={"name": "Notes.txt", "parent_id": projects_id})
    nested_doc_id = resp.json()["id"]
    resp = sync_client.post("/api/documents", json={"name": "root.txt", "parent_id": None})
    root_doc_id = resp.json()["id"]

    with sync_client.websocket_connect(f"/ws/doc/{nested_doc_id}"):
        entries = _presence_entries(sync_client)
        assert len(entries) == 1
        assert entries[0]["doc_id"] == nested_doc_id
        assert entries[0]["doc_name"] == "Notes.txt"
        assert entries[0]["doc_path"] == ["Work", "Projects"]

    with sync_client.websocket_connect(f"/ws/doc/{root_doc_id}"):
        entries = _presence_entries(sync_client)
        assert len(entries) == 1
        assert entries[0]["doc_id"] == root_doc_id
        assert entries[0]["doc_path"] == []

