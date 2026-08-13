import pytest
from sqlalchemy import select

from server.app.db import AsyncSessionLocal
from server.app.models import User
from server.cli import create_admin


async def test_create_admin_creates_admin_user():
    await create_admin("clibootstrap", "CLI Bootstrap", "clipass1234")

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.username == "clibootstrap"))
        user = result.scalar_one()
        assert user.is_admin is True
        assert user.display_name == "CLI Bootstrap"


async def test_create_admin_refuses_duplicate_username():
    await create_admin("dupeadmin", "Dupe Admin", "pass1234567")

    with pytest.raises(SystemExit):
        await create_admin("dupeadmin", "Dupe Admin 2", "pass7654321")
