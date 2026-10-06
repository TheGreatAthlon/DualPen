import datetime
import os
import secrets

from fastapi import Cookie, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.app.db import get_db
from server.app.models import GuestGrant, Node, Session, User

SESSION_COOKIE_NAME = "session_token"
SESSION_LIFETIME = datetime.timedelta(days=14)
GUEST_SESSION_LIFETIME = datetime.timedelta(hours=24)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


async def create_session(
    db: AsyncSession, user: User, lifetime: datetime.timedelta = SESSION_LIFETIME
) -> Session:
    session = Session(
        token=secrets.token_urlsafe(32),
        user_id=user.id,
        expires_at=_now() + lifetime,
    )
    db.add(session)
    await db.commit()
    return session


def _cookie_secure() -> bool:
    # Secure by default; set COLLAB_EDITOR_COOKIE_SECURE=0 for local http dev.
    return os.environ.get("COLLAB_EDITOR_COOKIE_SECURE", "true").strip().lower() not in ("0", "false", "no")


def set_session_cookie(
    response: Response, session: Session, lifetime: datetime.timedelta = SESSION_LIFETIME
) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session.token,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        max_age=int(lifetime.total_seconds()),
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
    )


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


async def get_guest_grant(db: AsyncSession, user: User) -> GuestGrant | None:
    result = await db.execute(select(GuestGrant).where(GuestGrant.user_id == user.id))
    return result.scalar_one_or_none()


async def describe_user(db: AsyncSession, user: User):
    """UserOut for `user`, with guest fields filled in when they are a guest."""
    from server.app.schemas import UserOut

    out = UserOut.model_validate(user)
    grant = await get_guest_grant(db, user)
    if grant is not None:
        out.is_guest = True
        out.guest_doc_id = grant.doc_id
        out.guest_read_only = grant.read_only
        out.guest_doc_name = (await db.execute(select(Node.name).where(Node.id == grant.doc_id))).scalar_one_or_none()
    return out


async def require_member(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> User:
    if await get_guest_grant(db, user) is not None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Guests cannot access this resource")
    return user
