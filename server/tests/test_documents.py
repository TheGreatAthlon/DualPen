import pytest


async def test_tree_requires_auth(client):
    resp = await client.get("/api/tree")
    assert resp.status_code == 401


async def test_create_folder_requires_auth(client):
    resp = await client.post("/api/folders", json={"name": "Notes", "parent_id": None})
    assert resp.status_code == 401


async def test_create_document_requires_auth(client):
    resp = await client.post("/api/documents", json={"name": "doc.txt", "parent_id": None})
    assert resp.status_code == 401


async def test_get_content_requires_auth(client):
    resp = await client.get("/api/documents/some-id/content")
    assert resp.status_code == 401


async def test_put_content_requires_auth(client):
    resp = await client.put("/api/documents/some-id/content", json={"content": "x"})
    assert resp.status_code == 401


async def test_patch_node_requires_auth(client):
    resp = await client.patch("/api/nodes/some-id", json={"name": "x"})
    assert resp.status_code == 401


async def test_delete_node_requires_auth(client):
    resp = await client.delete("/api/nodes/some-id")
    assert resp.status_code == 401


async def test_empty_tree(user_client):
    resp = await user_client.get("/api/tree")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_create_folder(user_client):
    resp = await user_client.post("/api/folders", json={"name": "Notes", "parent_id": None})
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "Notes"
    assert body["kind"] == "folder"
    assert body["parent_id"] is None
    assert "blob_path" not in body


async def test_create_document_creates_blob(user_client):
    resp = await user_client.post("/api/documents", json={"name": "readme.txt", "parent_id": None})
    assert resp.status_code == 201
    body = resp.json()
    assert body["kind"] == "document"
    node_id = body["id"]

    resp = await user_client.get(f"/api/documents/{node_id}/content")
    assert resp.status_code == 200
    assert resp.json() == {"content": ""}


async def test_create_document_in_folder(user_client):
    folder_resp = await user_client.post("/api/folders", json={"name": "Folder", "parent_id": None})
    folder_id = folder_resp.json()["id"]

    resp = await user_client.post("/api/documents", json={"name": "doc.txt", "parent_id": folder_id})
    assert resp.status_code == 201
    assert resp.json()["parent_id"] == folder_id


async def test_create_with_nonexistent_parent_rejected(user_client):
    resp = await user_client.post("/api/documents", json={"name": "doc.txt", "parent_id": "nonexistent"})
    assert resp.status_code == 400


async def test_create_with_document_as_parent_rejected(user_client):
    doc_resp = await user_client.post("/api/documents", json={"name": "doc.txt", "parent_id": None})
    doc_id = doc_resp.json()["id"]

    resp = await user_client.post("/api/folders", json={"name": "sub", "parent_id": doc_id})
    assert resp.status_code == 400


async def test_list_tree_returns_created_nodes(user_client):
    await user_client.post("/api/folders", json={"name": "Folder", "parent_id": None})
    await user_client.post("/api/documents", json={"name": "doc.txt", "parent_id": None})

    resp = await user_client.get("/api/tree")
    assert resp.status_code == 200
    names = {n["name"] for n in resp.json()}
    assert names == {"Folder", "doc.txt"}


async def test_write_and_read_content(user_client):
    resp = await user_client.post("/api/documents", json={"name": "doc.txt", "parent_id": None})
    node_id = resp.json()["id"]

    resp = await user_client.put(f"/api/documents/{node_id}/content", json={"content": "hello world"})
    assert resp.status_code == 200

    resp = await user_client.get(f"/api/documents/{node_id}/content")
    assert resp.status_code == 200
    assert resp.json() == {"content": "hello world"}


async def test_get_content_404_for_missing_node(user_client):
    resp = await user_client.get("/api/documents/nonexistent/content")
    assert resp.status_code == 404


async def test_get_content_404_for_folder(user_client):
    resp = await user_client.post("/api/folders", json={"name": "Folder", "parent_id": None})
    folder_id = resp.json()["id"]

    resp = await user_client.get(f"/api/documents/{folder_id}/content")
    assert resp.status_code == 404


async def test_rename_node(user_client):
    resp = await user_client.post("/api/documents", json={"name": "old.txt", "parent_id": None})
    node_id = resp.json()["id"]

    resp = await user_client.patch(f"/api/nodes/{node_id}", json={"name": "new.txt"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "new.txt"


async def test_rename_strips_surrounding_whitespace_and_dots(user_client):
    resp = await user_client.post("/api/documents", json={"name": "old.txt", "parent_id": None})
    node_id = resp.json()["id"]

    resp = await user_client.patch(f"/api/nodes/{node_id}", json={"name": "  spaced.txt.  "})
    assert resp.status_code == 200
    assert resp.json()["name"] == "spaced.txt"


@pytest.mark.parametrize("bad_name", ["a/b", "a\\b", "a:b", "a*b", "a?b", 'a"b', "a<b", "a>b", "a|b", "\t\n"])
async def test_create_document_rejects_invalid_characters(user_client, bad_name):
    resp = await user_client.post("/api/documents", json={"name": bad_name, "parent_id": None})
    assert resp.status_code == 422


async def test_create_folder_rejects_empty_name(user_client):
    resp = await user_client.post("/api/folders", json={"name": "   ", "parent_id": None})
    assert resp.status_code == 422


async def test_rename_rejects_invalid_characters(user_client):
    resp = await user_client.post("/api/documents", json={"name": "old.txt", "parent_id": None})
    node_id = resp.json()["id"]

    resp = await user_client.patch(f"/api/nodes/{node_id}", json={"name": "bad/name.txt"})
    assert resp.status_code == 422
    # original name must be unchanged
    resp = await user_client.get("/api/tree")
    names = [n["name"] for n in resp.json()]
    assert "old.txt" in names


async def test_move_node(user_client):
    folder_resp = await user_client.post("/api/folders", json={"name": "Folder", "parent_id": None})
    folder_id = folder_resp.json()["id"]
    doc_resp = await user_client.post("/api/documents", json={"name": "doc.txt", "parent_id": None})
    doc_id = doc_resp.json()["id"]

    resp = await user_client.patch(f"/api/nodes/{doc_id}", json={"parent_id": folder_id})
    assert resp.status_code == 200
    assert resp.json()["parent_id"] == folder_id


async def test_move_to_root_via_clear_parent(user_client):
    folder_resp = await user_client.post("/api/folders", json={"name": "Folder", "parent_id": None})
    folder_id = folder_resp.json()["id"]
    doc_resp = await user_client.post("/api/documents", json={"name": "doc.txt", "parent_id": folder_id})
    doc_id = doc_resp.json()["id"]

    resp = await user_client.patch(f"/api/nodes/{doc_id}", json={"clear_parent": True})
    assert resp.status_code == 200
    assert resp.json()["parent_id"] is None


async def test_move_rejects_self_parent(user_client):
    resp = await user_client.post("/api/folders", json={"name": "Folder", "parent_id": None})
    folder_id = resp.json()["id"]

    resp = await user_client.patch(f"/api/nodes/{folder_id}", json={"parent_id": folder_id})
    assert resp.status_code == 400


async def test_move_rejects_cycle(user_client):
    parent_resp = await user_client.post("/api/folders", json={"name": "Parent", "parent_id": None})
    parent_id = parent_resp.json()["id"]
    child_resp = await user_client.post("/api/folders", json={"name": "Child", "parent_id": parent_id})
    child_id = child_resp.json()["id"]

    resp = await user_client.patch(f"/api/nodes/{parent_id}", json={"parent_id": child_id})
    assert resp.status_code == 400


async def test_move_rejects_nonexistent_parent(user_client):
    doc_resp = await user_client.post("/api/documents", json={"name": "doc.txt", "parent_id": None})
    doc_id = doc_resp.json()["id"]

    resp = await user_client.patch(f"/api/nodes/{doc_id}", json={"parent_id": "nonexistent"})
    assert resp.status_code == 400


async def test_patch_404_for_missing_node(user_client):
    resp = await user_client.patch("/api/nodes/nonexistent", json={"name": "x"})
    assert resp.status_code == 404


async def test_delete_empty_folder_succeeds(user_client):
    folder_resp = await user_client.post("/api/folders", json={"name": "Folder", "parent_id": None})
    folder_id = folder_resp.json()["id"]

    resp = await user_client.delete(f"/api/nodes/{folder_id}")
    assert resp.status_code == 204

    resp = await user_client.get("/api/tree")
    remaining_ids = {n["id"] for n in resp.json()}
    assert folder_id not in remaining_ids


async def test_delete_nonempty_folder_rejected(user_client):
    folder_resp = await user_client.post("/api/folders", json={"name": "Folder", "parent_id": None})
    folder_id = folder_resp.json()["id"]
    doc_resp = await user_client.post("/api/documents", json={"name": "doc.txt", "parent_id": folder_id})
    doc_id = doc_resp.json()["id"]

    resp = await user_client.delete(f"/api/nodes/{folder_id}")
    assert resp.status_code == 400

    resp = await user_client.get("/api/tree")
    remaining_ids = {n["id"] for n in resp.json()}
    assert folder_id in remaining_ids
    assert doc_id in remaining_ids


async def test_delete_document_rejected(user_client):
    doc_resp = await user_client.post("/api/documents", json={"name": "doc.txt", "parent_id": None})
    doc_id = doc_resp.json()["id"]

    resp = await user_client.delete(f"/api/nodes/{doc_id}")
    assert resp.status_code == 400

    resp = await user_client.get(f"/api/documents/{doc_id}/content")
    assert resp.status_code == 200


async def test_delete_404_for_missing_node(user_client):
    resp = await user_client.delete("/api/nodes/nonexistent")
    assert resp.status_code == 404
