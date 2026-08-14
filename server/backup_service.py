import shutil
import sqlite3
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.engine import make_url

from server.app import crypto, docstore
from server.app.db import DATABASE_URL, SERVER_DATA_DIR

DEFAULT_BACKUP_DIR = SERVER_DATA_DIR.parent / "backups"

_ARCHIVE_PREFIX = "dualpen-backup-"


class NotSqliteError(Exception):
    pass


def _db_path() -> Path:
    url = make_url(DATABASE_URL)
    if url.get_backend_name() != "sqlite" or not url.database:
        raise NotSqliteError(f"backup only supports a file-based sqlite database, got: {DATABASE_URL}")
    return Path(url.database)


def _backup_sqlite_db(source: Path, dest: Path) -> None:
    # sqlite3's online backup API is the only safe way to copy a live
    # database: it's WAL-aware and produces a consistent snapshot even while
    # the app is mid-write, unlike a raw file copy which could grab a
    # half-written page or miss data still sitting in the -wal file.
    src_conn = sqlite3.connect(str(source))
    try:
        dest_conn = sqlite3.connect(str(dest))
        try:
            src_conn.backup(dest_conn)
        finally:
            dest_conn.close()
    finally:
        src_conn.close()


def run_backup(dest_dir: Path | None = None, keep: int | None = None) -> Path:
    dest_dir = dest_dir or DEFAULT_BACKUP_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)

    db_source = _db_path()
    docstore_source = docstore._docstore_dir()
    key_source = crypto._master_key_path()

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_path = dest_dir / f"{_ARCHIVE_PREFIX}{timestamp}.tar.gz"

    with tempfile.TemporaryDirectory(prefix="dualpen_backup_staging_") as staging:
        staging_dir = Path(staging)

        db_staging_dir = staging_dir / "db"
        db_staging_dir.mkdir()
        _backup_sqlite_db(db_source, db_staging_dir / "app.db")

        if docstore_source.exists():
            shutil.copytree(docstore_source, staging_dir / "docstore")

        if key_source.exists():
            shutil.copy2(key_source, staging_dir / "master.key")

        with tarfile.open(archive_path, "w:gz") as tar:
            for item in staging_dir.iterdir():
                tar.add(item, arcname=item.name)

    if keep is not None:
        _prune_old_backups(dest_dir, keep)

    return archive_path


def _prune_old_backups(dest_dir: Path, keep: int) -> None:
    archives = sorted(dest_dir.glob(f"{_ARCHIVE_PREFIX}*.tar.gz"))
    excess = len(archives) - keep
    for old in archives[:excess]:
        old.unlink()
