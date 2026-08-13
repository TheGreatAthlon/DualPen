from server.app import docstore


def test_write_read_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLAB_EDITOR_DOCSTORE_PATH", str(tmp_path / "docstore"))
    docstore.write_document("abc.blob", "hello there")
    assert docstore.read_document("abc.blob") == "hello there"


def test_write_creates_docstore_dir(tmp_path, monkeypatch):
    target = tmp_path / "nested" / "docstore"
    monkeypatch.setenv("COLLAB_EDITOR_DOCSTORE_PATH", str(target))
    assert not target.exists()
    docstore.write_document("doc.blob", "content")
    assert target.exists()


def test_overwrite_replaces_content(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLAB_EDITOR_DOCSTORE_PATH", str(tmp_path / "docstore"))
    docstore.write_document("doc.blob", "first")
    docstore.write_document("doc.blob", "second")
    assert docstore.read_document("doc.blob") == "second"


def test_delete_removes_file(tmp_path, monkeypatch):
    docstore_dir = tmp_path / "docstore"
    monkeypatch.setenv("COLLAB_EDITOR_DOCSTORE_PATH", str(docstore_dir))
    docstore.write_document("doc.blob", "content")
    assert (docstore_dir / "doc.blob").exists()
    docstore.delete_document("doc.blob")
    assert not (docstore_dir / "doc.blob").exists()


def test_delete_nonexistent_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLAB_EDITOR_DOCSTORE_PATH", str(tmp_path / "docstore"))
    docstore.delete_document("does-not-exist.blob")


def test_stored_file_is_not_plaintext(tmp_path, monkeypatch):
    docstore_dir = tmp_path / "docstore"
    monkeypatch.setenv("COLLAB_EDITOR_DOCSTORE_PATH", str(docstore_dir))
    secret = "this is sensitive plaintext content"
    docstore.write_document("doc.blob", secret)
    raw_bytes = (docstore_dir / "doc.blob").read_bytes()
    assert secret.encode("utf-8") not in raw_bytes
