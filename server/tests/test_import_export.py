import io
import zipfile

import pytest


def _make_zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for path, content in entries.items():
            zf.writestr(path, content)
    return buffer.getvalue()


async def test_import_zip_requires_auth(client):
    zip_bytes = _make_zip({"a.txt": b"hello"})
    resp = await client.post(
        "/api/import-zip", files={"file": ("notes.zip", zip_bytes, "application/zip")}
    )
    assert resp.status_code == 401


async def test_export_zip_requires_auth(client):
    resp = await client.get("/api/export-zip")
    assert resp.status_code == 401


async def test_import_creates_subfolder_named_after_zip(user_client):
    zip_bytes = _make_zip({"a.txt": "hello world".encode("utf-8")})
    resp = await user_client.post(
        "/api/import-zip", files={"file": ("My Notes.zip", zip_bytes, "application/zip")}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["root"]["name"] == "My Notes"
    assert body["root"]["kind"] == "folder"
    assert body["skipped"] == []

    tree = (await user_client.get("/api/tree")).json()
    root = next(n for n in tree if n["id"] == body["root"]["id"])
    assert root["parent_id"] is None
    doc = next(n for n in tree if n["name"] == "a.txt")
    assert doc["parent_id"] == root["id"]
    content = await user_client.get(f"/api/documents/{doc['id']}/content")
    assert content.json()["content"] == "hello world"


async def test_import_recreates_nested_folder_structure(user_client):
    zip_bytes = _make_zip(
        {
            "top.txt": b"top level",
            "sub/nested.txt": b"one level deep",
            "sub/deeper/leaf.txt": b"two levels deep",
        }
    )
    resp = await user_client.post(
        "/api/import-zip", files={"file": ("proj.zip", zip_bytes, "application/zip")}
    )
    assert resp.status_code == 201
    root_id = resp.json()["root"]["id"]

    tree = (await user_client.get("/api/tree")).json()
    by_id = {n["id"]: n for n in tree}

    top = next(n for n in tree if n["name"] == "top.txt")
    assert top["parent_id"] == root_id

    sub = next(n for n in tree if n["name"] == "sub" and n["kind"] == "folder")
    assert sub["parent_id"] == root_id

    nested = next(n for n in tree if n["name"] == "nested.txt")
    assert nested["parent_id"] == sub["id"]

    deeper = next(n for n in tree if n["name"] == "deeper" and n["kind"] == "folder")
    assert deeper["parent_id"] == sub["id"]

    leaf = next(n for n in tree if n["name"] == "leaf.txt")
    assert leaf["parent_id"] == deeper["id"]
    assert by_id[leaf["parent_id"]]["id"] == deeper["id"]


async def test_import_skips_non_utf8_entries_and_reports_them(user_client):
    zip_bytes = _make_zip(
        {
            "good.txt": "readable text".encode("utf-8"),
            "binary.dat": b"\xff\xfe\x00\x01\x80\x81",
        }
    )
    resp = await user_client.post(
        "/api/import-zip", files={"file": ("mixed.zip", zip_bytes, "application/zip")}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["skipped"] == ["binary.dat"]

    tree = (await user_client.get("/api/tree")).json()
    names = {n["name"] for n in tree}
    assert "good.txt" in names
    assert "binary.dat" not in names


async def test_import_into_existing_folder(user_client):
    folder_resp = await user_client.post("/api/folders", json={"name": "Projects", "parent_id": None})
    folder_id = folder_resp.json()["id"]

    zip_bytes = _make_zip({"a.txt": b"content"})
    resp = await user_client.post(
        "/api/import-zip",
        params={"parent_id": folder_id},
        files={"file": ("imported.zip", zip_bytes, "application/zip")},
    )
    assert resp.status_code == 201
    assert resp.json()["root"]["parent_id"] == folder_id


async def test_import_rejects_bad_zip(user_client):
    resp = await user_client.post(
        "/api/import-zip", files={"file": ("broken.zip", b"not a real zip", "application/zip")}
    )
    assert resp.status_code == 400


async def test_export_and_reimport_round_trip(user_client):
    zip_bytes = _make_zip(
        {
            "top.txt": "top content".encode("utf-8"),
            "sub/nested.txt": "nested content".encode("utf-8"),
        }
    )
    import_resp = await user_client.post(
        "/api/import-zip", files={"file": ("roundtrip.zip", zip_bytes, "application/zip")}
    )
    root_id = import_resp.json()["root"]["id"]

    export_resp = await user_client.get("/api/export-zip", params={"node_id": root_id})
    assert export_resp.status_code == 200
    assert export_resp.headers["content-type"] == "application/zip"
    assert "roundtrip.zip" in export_resp.headers["content-disposition"]

    exported = zipfile.ZipFile(io.BytesIO(export_resp.content))
    names = set(exported.namelist())
    assert "roundtrip/top.txt" in names
    assert "roundtrip/sub/" in names
    assert "roundtrip/sub/nested.txt" in names
    assert exported.read("roundtrip/top.txt").decode("utf-8") == "top content"
    assert exported.read("roundtrip/sub/nested.txt").decode("utf-8") == "nested content"


async def test_export_entire_tree_when_no_node_id(user_client):
    await user_client.post("/api/folders", json={"name": "Alpha", "parent_id": None})
    await user_client.post("/api/folders", json={"name": "Beta", "parent_id": None})

    resp = await user_client.get("/api/export-zip")
    assert resp.status_code == 200
    exported = zipfile.ZipFile(io.BytesIO(resp.content))
    names = exported.namelist()
    assert "Alpha/" in names
    assert "Beta/" in names


async def test_export_nonexistent_node_404s(user_client):
    resp = await user_client.get("/api/export-zip", params={"node_id": "nonexistent-id"})
    assert resp.status_code == 404
