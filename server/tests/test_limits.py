import io
import zipfile

import pytest

from server.app import import_export_service


def _zip(entries: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


# --- cookie Secure flag ---

async def test_cookie_not_secure_when_disabled(client, normal_user):
    resp = await client.post("/api/login", json={"username": "alice", "password": "alicepass123"})
    assert "secure" not in resp.headers["set-cookie"].lower()


@pytest.mark.parametrize("value", [None, "1", "true"])
async def test_cookie_secure_by_default(client, normal_user, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("COLLAB_EDITOR_COOKIE_SECURE")
    else:
        monkeypatch.setenv("COLLAB_EDITOR_COOKIE_SECURE", value)
    resp = await client.post("/api/login", json={"username": "alice", "password": "alicepass123"})
    assert "secure" in resp.headers["set-cookie"].lower()


@pytest.mark.parametrize("value", ["0", "false", "No"])
async def test_cookie_secure_disabled_values(client, normal_user, monkeypatch, value):
    monkeypatch.setenv("COLLAB_EDITOR_COOKIE_SECURE", value)
    resp = await client.post("/api/login", json={"username": "alice", "password": "alicepass123"})
    assert "secure" not in resp.headers["set-cookie"].lower()


# --- body size cap ---

async def test_body_over_content_length_cap_413(client, monkeypatch):
    monkeypatch.setenv("COLLAB_EDITOR_MAX_BODY_BYTES", "100")
    resp = await client.post("/api/login", content=b"x" * 500, headers={"content-type": "application/json"})
    assert resp.status_code == 413


async def test_streamed_body_over_cap_413(client, monkeypatch):
    monkeypatch.setenv("COLLAB_EDITOR_MAX_BODY_BYTES", "100")

    async def gen():
        for _ in range(10):
            yield b"x" * 50

    resp = await client.post("/api/login", content=gen(), headers={"content-type": "application/json"})
    assert resp.status_code == 413


async def test_streamed_body_read_by_handler_413(monkeypatch):
    import httpx
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    from server.app.limits import BodySizeLimitMiddleware

    monkeypatch.setenv("COLLAB_EDITOR_MAX_BODY_BYTES", "1000")

    async def handler(request):
        return PlainTextResponse(str(len(await request.body())))

    app = BodySizeLimitMiddleware(Starlette(routes=[Route("/", handler, methods=["POST"])]))

    async def gen():
        for _ in range(5):
            yield b"x" * 500

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        assert (await c.post("/", content=gen())).status_code == 413


async def test_login_password_too_long_422(client, monkeypatch):
    resp = await client.post("/api/login", json={"username": "alice", "password": "x" * 1000})
    assert resp.status_code == 422


async def test_body_under_cap_ok(client, normal_user, monkeypatch):
    monkeypatch.setenv("COLLAB_EDITOR_MAX_BODY_BYTES", "1000")
    resp = await client.post("/api/login", json={"username": "alice", "password": "alicepass123"})
    assert resp.status_code == 200


# --- import limits ---

async def test_import_too_many_entries_413(user_client, monkeypatch):
    monkeypatch.setenv("COLLAB_EDITOR_IMPORT_MAX_ENTRIES", "3")
    data = _zip({f"f{i}.txt": "x" for i in range(4)})
    resp = await user_client.post("/api/import-zip", files={"file": ("big.zip", data, "application/zip")})
    assert resp.status_code == 413


async def test_import_declared_uncompressed_too_large_413(user_client, monkeypatch):
    monkeypatch.setenv("COLLAB_EDITOR_IMPORT_MAX_UNCOMPRESSED_BYTES", "1000")
    data = _zip({"a.txt": "a" * 5000})
    resp = await user_client.post("/api/import-zip", files={"file": ("bomb.zip", data, "application/zip")})
    assert resp.status_code == 413


async def test_import_within_limits_ok(user_client, monkeypatch):
    monkeypatch.setenv("COLLAB_EDITOR_IMPORT_MAX_ENTRIES", "3")
    data = _zip({"a.txt": "a", "b.txt": "b"})
    resp = await user_client.post("/api/import-zip", files={"file": ("ok.zip", data, "application/zip")})
    assert resp.status_code == 201


def test_import_limit_error_is_exception_type():
    assert issubclass(import_export_service.ImportTooLargeError, Exception)


# --- rate limiting ---

async def test_login_rate_limited_429_with_retry_after(client, normal_user, monkeypatch):
    monkeypatch.setenv("COLLAB_EDITOR_LOGIN_RATE_LIMIT", "3")
    for _ in range(3):
        r = await client.post("/api/login", json={"username": "alice", "password": "bad"})
        assert r.status_code == 401
    r = await client.post("/api/login", json={"username": "alice", "password": "bad"})
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) >= 1


async def test_admin_rate_limited(admin_client, monkeypatch):
    monkeypatch.setenv("COLLAB_EDITOR_ADMIN_RATE_LIMIT", "2")
    assert (await admin_client.get("/api/admin/users")).status_code == 200
    assert (await admin_client.get("/api/admin/users")).status_code == 200
    r = await admin_client.get("/api/admin/users")
    assert r.status_code == 429
    assert "retry-after" in r.headers


async def test_rate_limit_disabled(client, normal_user, monkeypatch):
    monkeypatch.setenv("COLLAB_EDITOR_LOGIN_RATE_LIMIT", "1")
    monkeypatch.setenv("COLLAB_EDITOR_RATE_LIMIT_ENABLED", "0")
    for _ in range(3):
        r = await client.post("/api/login", json={"username": "alice", "password": "bad"})
        assert r.status_code == 401


# --- review fixes ---

async def test_unauthenticated_admin_hits_do_not_lock_out_admin(client, admin_user, monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from server.app.main import app

    monkeypatch.setenv("COLLAB_EDITOR_ADMIN_RATE_LIMIT", "3")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        for _ in range(3):
            assert (await anon.get("/api/admin/users")).status_code == 401
        assert (await anon.get("/api/admin/users")).status_code == 429
    r = await client.post("/api/login", json={"username": "admin", "password": "adminpass123"})
    assert r.status_code == 200
    assert (await client.get("/api/admin/users")).status_code == 200


async def test_successful_logins_do_not_consume_login_limit(client, normal_user, monkeypatch):
    monkeypatch.setenv("COLLAB_EDITOR_LOGIN_RATE_LIMIT", "2")
    for _ in range(5):
        r = await client.post("/api/login", json={"username": "alice", "password": "alicepass123"})
        assert r.status_code == 200


@pytest.mark.parametrize("value", ["0", "-5", "abc"])
async def test_bad_window_falls_back_to_default(client, normal_user, monkeypatch, value):
    monkeypatch.setenv("COLLAB_EDITOR_LOGIN_RATE_LIMIT", "2")
    monkeypatch.setenv("COLLAB_EDITOR_RATE_LIMIT_WINDOW_SECONDS", value)
    from server.app import limits

    assert limits._window_seconds() == 60
    for _ in range(2):
        await client.post("/api/login", json={"username": "alice", "password": "bad"})
    r = await client.post("/api/login", json={"username": "alice", "password": "bad"})
    assert r.status_code == 429


async def test_max_body_nonpositive_disables_cap(client, normal_user, monkeypatch):
    monkeypatch.setenv("COLLAB_EDITOR_MAX_BODY_BYTES", "0")
    resp = await client.post("/api/login", content=b"x" * 5000, headers={"content-type": "application/json"})
    assert resp.status_code != 413


def test_expired_keys_are_pruned(monkeypatch):
    from server.app import limits

    monkeypatch.setenv("COLLAB_EDITOR_RATE_LIMIT_WINDOW_SECONDS", "1")
    limits._hits[("login", "1.2.3.4")].append(limits.time.monotonic() - 100)
    limits._hits[("login", "5.6.7.8")].append(limits.time.monotonic() - 100)
    limits._check(("login", "9.9.9.9"), 5)
    assert ("login", "1.2.3.4") not in limits._hits
    assert ("login", "5.6.7.8") not in limits._hits


async def test_logout_cookie_has_same_attributes(client, normal_user, monkeypatch):
    monkeypatch.setenv("COLLAB_EDITOR_COOKIE_SECURE", "1")
    resp = await client.post("/api/logout")
    cookie = resp.headers["set-cookie"].lower()
    assert "secure" in cookie and "httponly" in cookie and "samesite=lax" in cookie


# --- import rollback ---

def _lying_zip() -> bytes:
    data = bytearray(_zip({"good.txt": "ok", "bad.txt": "y" * 2000}))
    # Corrupt the stored/deflated payload of the last entry so CRC/decompress fails.
    idx = data.rfind(b"bad.txt") + len(b"bad.txt")
    for i in range(idx, idx + 8):
        data[i] ^= 0xFF
    return bytes(data)


async def test_import_corrupt_entry_400_and_rolls_back(user_client):
    resp = await user_client.post("/api/import-zip", files={"file": ("c.zip", _lying_zip(), "application/zip")})
    assert resp.status_code == 400
    tree = await user_client.get("/api/tree")
    assert resp.status_code == 400
    assert "c" not in [n["name"] for n in tree.json()]


async def test_import_too_large_mid_loop_rolls_back(user_client, monkeypatch):
    monkeypatch.setenv("COLLAB_EDITOR_IMPORT_MAX_UNCOMPRESSED_BYTES", "1000")
    from server.app import import_export_service as svc

    # Declared sizes pass the up-front check; real read exceeds the cap.
    monkeypatch.setattr(svc, "_check_zip_limits", lambda zf: None)
    data = _zip({"a.txt": "a" * 600, "b.txt": "b" * 600})
    resp = await user_client.post("/api/import-zip", files={"file": ("m.zip", data, "application/zip")})
    assert resp.status_code == 413
    tree = await user_client.get("/api/tree")
    assert "m" not in [n["name"] for n in tree.json()]


async def test_login_rate_limit_holds_under_parallel_burst(client, normal_user, monkeypatch):
    import asyncio
    from collections import Counter

    monkeypatch.setenv("COLLAB_EDITOR_LOGIN_RATE_LIMIT", "5")
    rs = await asyncio.gather(
        *[client.post("/api/login", json={"username": "alice", "password": "bad"}) for _ in range(30)]
    )
    assert Counter(r.status_code for r in rs) == {401: 5, 429: 25}


async def test_successful_logins_do_not_consume_login_budget(client, normal_user, monkeypatch):
    monkeypatch.setenv("COLLAB_EDITOR_LOGIN_RATE_LIMIT", "2")
    for _ in range(6):
        r = await client.post("/api/login", json={"username": "alice", "password": "alicepass123"})
        assert r.status_code == 200


# --- import error mapping ---

@pytest.mark.parametrize(
    "message, expected",
    [("File 'a.txt' is encrypted, password required", import_export_service.InvalidZipError), ("db exploded", RuntimeError)],
)
async def test_import_runtime_error_mapping(normal_user, monkeypatch, message, expected):
    from server.app.db import AsyncSessionLocal

    async def boom(*args, **kwargs):
        raise RuntimeError(message)

    monkeypatch.setattr(import_export_service, "_import_entries", boom)
    async with AsyncSessionLocal() as db:
        with pytest.raises(expected):
            await import_export_service.import_zip(db, _zip({"a.txt": "x"}), "z.zip", None)


async def test_forwarded_for_keys_the_limit_when_trusted(client, monkeypatch):
    monkeypatch.setenv("COLLAB_EDITOR_JOIN_RATE_LIMIT", "1")
    monkeypatch.setenv("COLLAB_EDITOR_TRUST_FORWARDED_FOR", "true")
    codes = []
    # Forged leftmost entries are ignored; only the proxy-appended last entry counts.
    for ip, forged in (("9.9.9.9", "1.1.1.1"), ("9.9.9.9", "2.2.2.2"), ("8.8.8.8", "3.3.3.3")):
        resp = await client.post(
            "/api/share/nope/join",
            json={"display_name": "g"},
            headers={"X-Forwarded-For": f"{forged}, {ip}"},
        )
        codes.append(resp.status_code)
    assert codes == [404, 429, 404]
