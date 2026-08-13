import os
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio

_tmp_dir = tempfile.mkdtemp(prefix="collab_editor_test_")
_db_path = Path(_tmp_dir) / "test.db"
os.environ["COLLAB_EDITOR_DATABASE_URL"] = f"sqlite+aiosqlite:///{_db_path}"
os.environ["COLLAB_EDITOR_MASTER_KEY_PATH"] = str(Path(_tmp_dir) / "master.key")
os.environ["COLLAB_EDITOR_DOCSTORE_PATH"] = str(Path(_tmp_dir) / "docstore")

from httpx import ASGITransport, AsyncClient

from server.app.db import AsyncSessionLocal, engine, init_db
from server.app.main import app
from server.app.user_service import create_user


@pytest_asyncio.fixture(autouse=True)
async def _reset_db():
    await init_db()
    yield
    async with engine.begin() as conn:
        from server.app.db import Base

        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def admin_user():
    async with AsyncSessionLocal() as db:
        return await create_user(db, "admin", "Admin User", "adminpass123", is_admin=True)


@pytest_asyncio.fixture
async def normal_user():
    async with AsyncSessionLocal() as db:
        return await create_user(db, "alice", "Alice", "alicepass123", is_admin=False)


@pytest_asyncio.fixture
async def admin_client(client, admin_user):
    resp = await client.post("/api/login", json={"username": "admin", "password": "adminpass123"})
    assert resp.status_code == 200
    return client


@pytest_asyncio.fixture
async def user_client(client, normal_user):
    resp = await client.post("/api/login", json={"username": "alice", "password": "alicepass123"})
    assert resp.status_code == 200
    return client
