from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.app.auth import (
    clear_session_cookie,
    create_session,
    get_current_user,
    set_session_cookie,
    SESSION_COOKIE_NAME,
)
from server.app.db import get_db
from server.app.models import Session, User
from server.app.schemas import LoginRequest, UserOut
from server.app.security import verify_password

router = APIRouter(tags=["auth"])


@router.post("/login", response_model=UserOut)
async def login(payload: LoginRequest, response: Response, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.username == payload.username))
    user = result.scalar_one_or_none()

    invalid = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")

    if user is None or not user.is_active:
        raise invalid
    if not verify_password(user.password_hash, payload.password):
        raise invalid

    session = await create_session(db, user)
    set_session_cookie(response, session)
    return user


@router.post("/logout")
async def logout(
    response: Response,
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    db: AsyncSession = Depends(get_db),
):
    if session_token is not None:
        result = await db.execute(select(Session).where(Session.token == session_token))
        session = result.scalar_one_or_none()
        if session is not None:
            await db.delete(session)
            await db.commit()
    clear_session_cookie(response)
    return {"ok": True}


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)):
    return user
