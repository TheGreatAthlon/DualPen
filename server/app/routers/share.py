import datetime
import secrets

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.app import node_service, share_service
from server.app.auth import (
    GUEST_SESSION_LIFETIME,
    SESSION_COOKIE_NAME,
    create_session,
    describe_user,
    get_guest_grant,
    get_user_for_session_token,
    require_member,
    set_session_cookie,
)
from server.app.db import get_db
from server.app.limits import join_rate_limit
from server.app.models import GuestGrant, ShareLink, User
from server.app.routers.sync import kick_users
from server.app.schemas import CreateShareLinkRequest, JoinRequest, ShareLinkOut, UserOut

router = APIRouter(tags=["share"])


async def _get_doc_or_404(db: AsyncSession, doc_id: str):
    try:
        return await node_service.get_document_node(db, doc_id)
    except node_service.NodeNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


async def _get_live_link(db: AsyncSession, token: str) -> ShareLink:
    result = await db.execute(select(ShareLink).where(ShareLink.token == token))
    link = result.scalar_one_or_none()
    if link is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Share link not found")
    if link.expires_at is not None:
        # SQLite stores naive datetimes; treat them as UTC.
        expires_at = link.expires_at if link.expires_at.tzinfo else link.expires_at.replace(tzinfo=datetime.timezone.utc)
        if expires_at < datetime.datetime.now(datetime.timezone.utc):
            raise HTTPException(status_code=status.HTTP_410_GONE, detail="Share link has expired")
    return link


@router.post("/documents/{doc_id}/share-links", response_model=ShareLinkOut, status_code=status.HTTP_201_CREATED)
async def create_share_link(
    doc_id: str,
    payload: CreateShareLinkRequest,
    user: User = Depends(require_member),
    db: AsyncSession = Depends(get_db),
):
    await _get_doc_or_404(db, doc_id)
    expires_at = None
    if payload.expires_in_hours is not None:
        expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=payload.expires_in_hours)
    link = ShareLink(
        token=secrets.token_urlsafe(24),
        doc_id=doc_id,
        read_only=payload.read_only,
        expires_at=expires_at,
        created_by=user.id,
    )
    db.add(link)
    await db.commit()
    await db.refresh(link)
    return link


@router.get("/documents/{doc_id}/share-links", response_model=list[ShareLinkOut])
async def list_share_links(doc_id: str, _: User = Depends(require_member), db: AsyncSession = Depends(get_db)):
    await _get_doc_or_404(db, doc_id)
    result = await db.execute(select(ShareLink).where(ShareLink.doc_id == doc_id).order_by(ShareLink.created_at))
    return result.scalars().all()


@router.delete("/documents/{doc_id}/share-links/{token}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_share_link(
    doc_id: str, token: str, _: User = Depends(require_member), db: AsyncSession = Depends(get_db)
):
    result = await db.execute(select(ShareLink).where(ShareLink.token == token, ShareLink.doc_id == doc_id))
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Share link not found")
    guest_ids = await share_service.delete_share_link(db, token)
    await kick_users(guest_ids)


@router.get("/share/{token}", response_model=ShareLinkOut)
async def get_share_link(token: str, _: User = Depends(require_member), db: AsyncSession = Depends(get_db)):
    """Lets a signed-in member resolve a link to its document instead of joining as a guest."""
    return await _get_live_link(db, token)


@router.post("/share/{token}/join", response_model=UserOut, dependencies=[Depends(join_rate_limit)])
async def join_share_link(
    token: str,
    payload: JoinRequest,
    response: Response,
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    db: AsyncSession = Depends(get_db),
):
    # Never replace a signed-in member's session with a guest one.
    current = await get_user_for_session_token(db, session_token)
    if current is not None and await get_guest_grant(db, current) is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Already signed in")
    link = await _get_live_link(db, token)

    # "!" is not a valid argon2 hash, so verify_password always fails: guests cannot password-login.
    guest = User(
        username=f"guest-{secrets.token_hex(8)}",
        display_name=payload.display_name,
        password_hash="!",
    )
    db.add(guest)
    await db.flush()
    db.add(GuestGrant(user_id=guest.id, doc_id=link.doc_id, read_only=link.read_only, link_id=link.token))
    await db.commit()

    session = await create_session(db, guest, GUEST_SESSION_LIFETIME)
    set_session_cookie(response, session, GUEST_SESSION_LIFETIME)
    return await describe_user(db, guest)
