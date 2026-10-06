import datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from server.app import auth, share_service
from server.app.db import AsyncSessionLocal
from server.app.models import GuestGrant, Node, Session, ShareLink, User
from server.app.user_service import create_user


async def _make_guest(db, owner):
    doc = Node(name="d", kind="document")
    db.add(doc)
    await db.commit()
    db.add(ShareLink(token="tok", doc_id=doc.id, read_only=True, created_by=owner.id))
    guest = await create_user(db, "guest-1", "Guest", "x" * 20)
    db.add(GuestGrant(user_id=guest.id, doc_id=doc.id, read_only=True, link_id="tok"))
    await db.commit()
    return doc, guest


async def test_grant_lookup_and_require_member(normal_user):
    async with AsyncSessionLocal() as db:
        _, guest = await _make_guest(db, normal_user)
        grant = await auth.get_guest_grant(db, guest)
        assert grant.read_only and grant.link_id == "tok"
        assert await auth.get_guest_grant(db, normal_user) is None
        with pytest.raises(HTTPException) as exc:
            await auth.require_member(guest, db)
        assert exc.value.status_code == 403
        assert await auth.require_member(normal_user, db) is normal_user


async def test_session_lifetime(normal_user):
    async with AsyncSessionLocal() as db:
        default = await auth.create_session(db, normal_user)
        guest = await auth.create_session(db, normal_user, auth.GUEST_SESSION_LIFETIME)
        assert guest.expires_at < default.expires_at
        delta = guest.expires_at - datetime.datetime.now(datetime.timezone.utc)
        assert datetime.timedelta(hours=23) < delta <= datetime.timedelta(hours=24)


async def test_delete_link_cleans_guests(normal_user):
    async with AsyncSessionLocal() as db:
        _, guest = await _make_guest(db, normal_user)
        await auth.create_session(db, guest, auth.GUEST_SESSION_LIFETIME)
        await share_service.delete_share_link(db, "tok")
        assert (await db.execute(select(ShareLink))).first() is None
        assert (await db.execute(select(Session))).first() is None
        # Guest row is kept (inactive) so its id is never reused for another user.
        await db.refresh(guest)
        assert guest.is_active is False
        assert (await db.execute(select(User).where(User.id == normal_user.id))).first() is not None


async def test_reap_expired_removes_idle_guests_but_keeps_chatters(normal_user):
    from server.app import chat_service
    from server.app.models import ChatMessage

    old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=2)
    async with AsyncSessionLocal() as db:
        doc, quiet = await _make_guest(db, normal_user)
        chatty = await create_user(db, "guest-2", "Chatty", "x" * 20)
        db.add(GuestGrant(user_id=chatty.id, doc_id=doc.id, read_only=False, link_id="tok"))
        fresh = await create_user(db, "guest-3", "Fresh", "x" * 20)
        db.add(GuestGrant(user_id=fresh.id, doc_id=doc.id, read_only=False, link_id="tok"))
        for u in (quiet, chatty):
            u.created_at = old
            await auth.create_session(db, u)
        expired = (await db.execute(select(Session))).scalars().all()
        for s in expired:
            s.expires_at = old
        await db.commit()
        await chat_service.create_message(db, doc_id=doc.id, user_id=chatty.id, display_name="Chatty", body="hi")

        await share_service.reap_expired(db)

        users = {u.username for u in (await db.execute(select(User))).scalars()}
        assert "guest-1" not in users  # idle, old, no session: deleted
        assert {"guest-2", "guest-3", "alice"} <= users  # chatted / too new / member: kept
        assert (await db.execute(select(Session))).first() is None
        assert (await db.execute(select(GuestGrant).where(GuestGrant.user_id == quiet.id))).first() is None
        assert (await db.execute(select(ChatMessage))).first() is not None
