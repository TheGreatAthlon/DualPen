import os
from pathlib import Path

from server.app import crypto

SERVER_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "server_data"
DEFAULT_DOCSTORE_DIR = SERVER_DATA_DIR / "docstore"


def _docstore_dir() -> Path:
    override = os.environ.get("COLLAB_EDITOR_DOCSTORE_PATH")
    return Path(override) if override else DEFAULT_DOCSTORE_DIR


def _blob_file(blob_path: str) -> Path:
    return _docstore_dir() / blob_path


def write_document(blob_path: str, content: str) -> None:
    directory = _docstore_dir()
    directory.mkdir(parents=True, exist_ok=True)
    _blob_file(blob_path).write_bytes(crypto.encrypt(content))


def read_document(blob_path: str) -> str:
    return crypto.decrypt(_blob_file(blob_path).read_bytes())


def delete_document(blob_path: str) -> None:
    _blob_file(blob_path).unlink(missing_ok=True)
