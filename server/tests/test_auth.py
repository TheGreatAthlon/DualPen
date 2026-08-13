async def test_login_success(client, normal_user):
    resp = await client.post("/api/login", json={"username": "alice", "password": "alicepass123"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["username"] == "alice"
    assert "password_hash" not in body
    assert "session_token" in resp.cookies


async def test_login_wrong_password(client, normal_user):
    resp = await client.post("/api/login", json={"username": "alice", "password": "wrong"})
    assert resp.status_code == 401


async def test_login_unknown_user(client):
    resp = await client.post("/api/login", json={"username": "ghost", "password": "whatever"})
    assert resp.status_code == 401


async def test_me_requires_session(client):
    resp = await client.get("/api/me")
    assert resp.status_code == 401


async def test_me_with_valid_session(user_client):
    resp = await user_client.get("/api/me")
    assert resp.status_code == 200
    assert resp.json()["username"] == "alice"


async def test_logout_clears_session(user_client):
    resp = await user_client.post("/api/logout")
    assert resp.status_code == 200

    resp = await user_client.get("/api/me")
    assert resp.status_code == 401


async def test_inactive_user_cannot_login(client, normal_user):
    from server.app.db import AsyncSessionLocal
    from server.app.models import User
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.username == "alice"))
        user = result.scalar_one()
        user.is_active = False
        await db.commit()

    resp = await client.post("/api/login", json={"username": "alice", "password": "alicepass123"})
    assert resp.status_code == 401
