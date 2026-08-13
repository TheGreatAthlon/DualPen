import base64
import os
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SERVER_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "server_data"
DEFAULT_MASTER_KEY_PATH = SERVER_DATA_DIR / "master.key"

_NONCE_LEN = 12


def _master_key_path() -> Path:
    override = os.environ.get("COLLAB_EDITOR_MASTER_KEY_PATH")
    return Path(override) if override else DEFAULT_MASTER_KEY_PATH


def _load_or_create_master_key() -> bytes:
    path = _master_key_path()
    if path.exists():
        return base64.b64decode(path.read_text().strip())

    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    path.write_text(base64.b64encode(key).decode("ascii"))
    try:
        # Best-effort on Windows; NTFS ACLs aren't POSIX chmod, and a dev-box
        # single-user setup doesn't warrant real ACL manipulation here.
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


def _get_aesgcm() -> AESGCM:
    return AESGCM(_load_or_create_master_key())


def encrypt(plaintext: str) -> bytes:
    aesgcm = _get_aesgcm()
    # A fresh random nonce every call is required for AES-GCM: reusing a
    # nonce with the same key lets an attacker recover the keystream and
    # forge ciphertexts, so we never persist or derive it deterministically.
    nonce = secrets.token_bytes(_NONCE_LEN)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return nonce + ciphertext


def decrypt(blob: bytes) -> str:
    aesgcm = _get_aesgcm()
    nonce, ciphertext = blob[:_NONCE_LEN], blob[_NONCE_LEN:]
    return aesgcm.decrypt(nonce, ciphertext, None).decode("utf-8")
