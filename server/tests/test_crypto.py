from server.app import crypto


def test_encrypt_decrypt_roundtrip():
    plaintext = "hello, collaborative world"
    blob = crypto.encrypt(plaintext)
    assert crypto.decrypt(blob) == plaintext


def test_roundtrip_empty_string():
    blob = crypto.encrypt("")
    assert crypto.decrypt(blob) == ""


def test_roundtrip_unicode():
    plaintext = "héllo wörld 你好 🎉"
    blob = crypto.encrypt(plaintext)
    assert crypto.decrypt(blob) == plaintext


def test_nonce_is_not_reused():
    plaintext = "same plaintext every time"
    blob1 = crypto.encrypt(plaintext)
    blob2 = crypto.encrypt(plaintext)
    assert blob1 != blob2
    nonce1, nonce2 = blob1[:12], blob2[:12]
    assert nonce1 != nonce2


def test_master_key_file_created_and_reused(tmp_path, monkeypatch):
    key_path = tmp_path / "sub" / "master.key"
    monkeypatch.setenv("COLLAB_EDITOR_MASTER_KEY_PATH", str(key_path))

    blob = crypto.encrypt("some content")
    assert key_path.exists()

    key_bytes_first = key_path.read_bytes()
    crypto.encrypt("more content")
    assert key_path.read_bytes() == key_bytes_first

    assert crypto.decrypt(blob) == "some content"
