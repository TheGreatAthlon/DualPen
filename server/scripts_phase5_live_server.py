import asyncio
import os
import sys
import tempfile
from pathlib import Path

_tmp_dir = tempfile.mkdtemp(prefix="collab_editor_phase5_verify_")
_db_path = Path(_tmp_dir) / "verify.db"
os.environ["COLLAB_EDITOR_DATABASE_URL"] = f"sqlite+aiosqlite:///{_db_path}"
os.environ["COLLAB_EDITOR_MASTER_KEY_PATH"] = str(Path(_tmp_dir) / "master.key")
os.environ["COLLAB_EDITOR_DOCSTORE_PATH"] = str(Path(_tmp_dir) / "docstore")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn  # noqa: E402

from server.app.db import AsyncSessionLocal, init_db  # noqa: E402
from server.app.main import app  # noqa: E402
from server.app.user_service import create_user  # noqa: E402

PORT = 8799


async def _bootstrap() -> None:
    await init_db()
    async with AsyncSessionLocal() as db:
        await create_user(db, "verify_alice", "Verify Alice", "alicepass123", is_admin=False)
        await create_user(db, "verify_bob", "Verify Bob", "bobpass123", is_admin=False)
    print(f"READY port={PORT}", flush=True)


async def main() -> None:
    await _bootstrap()
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
