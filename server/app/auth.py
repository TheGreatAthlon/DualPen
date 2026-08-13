import datetime
import secrets

from fastapi import Cookie, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.app.db import get_db
from server.app.models import Session, User

SESSION_COOKIE_NAME = "session_token"
SESSION_LIFETIME = datetime.timedelta(days=14)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


async def create_session(db: AsyncSession, user: User) -> Session:
    session = Session(
        token=secrets.token_urlsafe(32),
        user_id=user.id,
        expires_at=_now() + SESSION_LIFETIME,
    )
    db.add(session)
    await db.commit()
    return session


def set_session_cookie(response: Response, session: Session) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session.token,
        httponly=True,
        samesite="lax",
        max_age=int(SESSION_LIFETIME.total_seconds()),
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")


async def get_user_for_session_token(db: AsyncSession, session_token: str | None) -> User | None:
    if session_token is None:
        return None

    result = await db.execute(select(Session).where(Session.token == session_token))
    session = result.scalar_one_or_none()
    if session is None:
        return None

    # SQLite stores naive datetimes; treat them as UTC to match _now().
    expires_at = session.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=datetime.timezone.utc)
    if expires_at < _now():
        await db.delete(session)
        await db.commit()
        return None

    result = await db.execute(select(User).where(User.id == session.user_id))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        return None

    return user


async def get_current_user(
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    db: AsyncSession = Depends(get_db),
) -> User:
    unauthorized = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    user = await get_user_for_session_token(db, session_token)
    if user is None:
        raise unauthorized
    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required")
    return user
