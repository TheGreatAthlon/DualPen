from server.app.security import hash_password, verify_password


def test_hash_and_verify_roundtrip():
    h = hash_password("correct horse battery staple")
    assert verify_password(h, "correct horse battery staple") is True


def test_verify_rejects_wrong_password():
    h = hash_password("correct horse battery staple")
    assert verify_password(h, "wrong password") is False


def test_hash_is_not_plaintext():
    h = hash_password("secret123")
    assert h != "secret123"
