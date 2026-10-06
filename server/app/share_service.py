import asyncio
import os
import datetime
import logging

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from server.app import node_service
from server.app.db import AsyncSessionLocal
from server.app.models import ChatMessage, GuestGrant, Node, Session, ShareLink, User
from server.app.routers.sync import kick_users

logger = logging.getLogger(__name__)

REAP_INTERVAL_SECONDS = 3600
# Don't touch guests this young: join commits the user before its session exists.
REAP_MIN_GUEST_AGE = datetime.timedelta(hours=1)



def trash_folder_name() -> str:
    """Name of the root folder that counts as Trash (the client reads it from /api/config)."""
    return os.environ.get("COLLAB_EDITOR_TRASH_FOLDER_NAME", "").strip() or "Trash"


async def delete_share_link(db: AsyncSession, token: str) -> list[int]:
    """Remove a link and cut off the guests it created: their sessions are deleted and
    the users deactivated. User and grant rows are kept so ids are never reused (chat
    messages reference them) and the guests stay hidden from the admin list.
    Returns the guest user ids so callers can close live connections."""
    guest_ids = list((await db.execute(select(GuestGrant.user_id).where(GuestGrant.link_id == token))).scalars())
    if guest_ids:
        await db.execute(delete(Session).where(Session.user_id.in_(guest_ids)))
        await db.execute(update(User).where(User.id.in_(guest_ids)).values(is_active=False))
    await db.execute(delete(ShareLink).where(ShareLink.token == token))
    await db.commit()
    return guest_ids


async def end_sharing_if_trashed(db: AsyncSession, node: Node) -> None:
    """Revoke every link on the documents under `node` once it sits inside the Trash folder."""
    path = await node_service.get_ancestor_path(db, node)
    name = trash_folder_name()
    # Either `node` sits under the Trash folder, or `node` is itself a root folder just named Trash.
    if not (path[:1] == [name] or (not path and node.kind == "folder" and node.name == name)):
        return
    doc_ids: list[str] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if current.kind == "document":
            doc_ids.append(current.id)
        else:
            children = await db.execute(select(Node).where(Node.parent_id == current.id))
            stack.extend(children.scalars())
    if not doc_ids:
        return
    tokens = (await db.execute(select(ShareLink.token).where(ShareLink.doc_id.in_(doc_ids)))).scalars().all()
    guest_ids: list[int] = []
    for token in tokens:
        guest_ids += await delete_share_link(db, token)
    await kick_users(guest_ids)


async def reap_expired(db: AsyncSession) -> None:
    """Delete expired sessions, then guests that have no session left and never chatted
    (chat messages reference their user id, so those rows are kept)."""
    now = datetime.datetime.now(datetime.timezone.utc)
    await db.execute(delete(Session).where(Session.expires_at < now))
    stale = (
        select(GuestGrant.user_id)
        .join(User, User.id == GuestGrant.user_id)
        .where(
            User.created_at < now - REAP_MIN_GUEST_AGE,
            GuestGrant.user_id.not_in(select(Session.user_id)),
            GuestGrant.user_id.not_in(select(ChatMessage.user_id)),
        )
    )
    stale_ids = list((await db.execute(stale)).scalars())
    if stale_ids:
        await db.execute(delete(GuestGrant).where(GuestGrant.user_id.in_(stale_ids)))
        await db.execute(delete(User).where(User.id.in_(stale_ids)))
    await db.commit()


async def reap_loop() -> None:
    while True:
        try:
            async with AsyncSessionLocal() as db:
                await reap_expired(db)
        except Exception:
            logger.exception("Guest cleanup failed")
        await asyncio.sleep(REAP_INTERVAL_SECONDS)
