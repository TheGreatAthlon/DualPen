import sqlite3
import tarfile
import time
from pathlib import Path

from server import backup_service
from server.app import crypto, docstore


async def test_backup_contains_db_docstore_and_key(user_client, tmp_path):
    doc_resp = await user_client.post("/api/documents", json={"name": "doc.txt", "parent_id": None})
    node_id = doc_resp.json()["id"]
    await user_client.put(f"/api/documents/{node_id}/content", json={"content": "hello world"})

    dest_dir = tmp_path / "backups"
    archive_path = backup_service.run_backup(dest_dir)

    assert archive_path.parent == dest_dir
    assert archive_path.exists()

    extract_dir = tmp_path / "extracted"
    extract_dir.mkdir()
    with tarfile.open(archive_path) as tar:
        tar.extractall(extract_dir, filter="data")

    db_copy = extract_dir / "db" / "app.db"
    assert db_copy.exists()
    conn = sqlite3.connect(str(db_copy))
    try:
        rows = conn.execute("SELECT name FROM nodes WHERE id = ?", (node_id,)).fetchall()
    finally:
        conn.close()
    assert rows == [("doc.txt",)]

    docstore_copy_dir = extract_dir / "docstore"
    blobs = list(docstore_copy_dir.glob("*.blob"))
    assert len(blobs) == 1
    original_bytes = (docstore._docstore_dir() / blobs[0].name).read_bytes()
    assert blobs[0].read_bytes() == original_bytes

    key_copy = extract_dir / "master.key"
    assert key_copy.exists()
    assert key_copy.read_bytes() == crypto._master_key_path().read_bytes()


async def test_backup_keep_prunes_older_archives(user_client, tmp_path):
    dest_dir = tmp_path / "backups"

    first = backup_service.run_backup(dest_dir)
    time.sleep(1.1)  # archive names are second-resolution timestamps
    second = backup_service.run_backup(dest_dir)
    time.sleep(1.1)
    third = backup_service.run_backup(dest_dir, keep=2)

    remaining = set(dest_dir.glob("dualpen-backup-*.tar.gz"))
    assert remaining == {second, third}
    assert first not in remaining
