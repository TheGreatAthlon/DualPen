from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.app.models import User
from server.app.security import hash_password


class UsernameTakenError(Exception):
    pass


async def create_user(
    db: AsyncSession,
    username: str,
    display_name: str,
    password: str,
    is_admin: bool = False,
) -> User:
    existing = await db.execute(select(User).where(User.username == username))
    if existing.scalar_one_or_none() is not None:
        raise UsernameTakenError(f"username '{username}' already exists")

    user = User(
        username=username,
        display_name=display_name,
        password_hash=hash_password(password),
        is_admin=is_admin,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user
