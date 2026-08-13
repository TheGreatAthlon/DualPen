import os
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

SERVER_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "server_data"
DB_DIR = SERVER_DATA_DIR / "db"
DB_PATH = DB_DIR / "app.db"

# Overridable so tests (and future config.toml wiring) don't have to touch the real db/app.db.
DATABASE_URL = os.environ.get("COLLAB_EDITOR_DATABASE_URL", f"sqlite+aiosqlite:///{DB_PATH}")

engine = create_async_engine(DATABASE_URL)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def init_db() -> None:
    if DATABASE_URL.startswith(f"sqlite+aiosqlite:///{DB_DIR}"):
        DB_DIR.mkdir(parents=True, exist_ok=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session
