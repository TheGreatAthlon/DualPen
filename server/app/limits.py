import os
import time
from collections import defaultdict, deque

from fastapi import Cookie, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse

from server.app.auth import SESSION_COOKIE_NAME, get_user_for_session_token
from server.app.db import get_db

_FALSE = ("0", "false", "no")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def max_body_bytes() -> int:
    """Request body cap in bytes. A value <= 0 disables the cap."""
    return _env_int("COLLAB_EDITOR_MAX_BODY_BYTES", 10 * 1024 * 1024)


def _window_seconds() -> int:
    """Rate-limit window; <= 0 or unparseable falls back to 60 (never disables;
    use COLLAB_EDITOR_RATE_LIMIT_ENABLED=false or a limit <= 0 for that)."""
    window = _env_int("COLLAB_EDITOR_RATE_LIMIT_WINDOW_SECONDS", 60)
    return window if window > 0 else 60


class BodySizeLimitMiddleware:
    """Pure ASGI middleware: rejects HTTP requests whose body exceeds the cap
    with 413, checking Content-Length up front and counting streamed bytes
    (chunked / lying Content-Length). WebSockets pass through untouched."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limit = max_body_bytes()
        if limit <= 0:
            await self.app(scope, receive, send)
            return
        too_large = JSONResponse({"detail": "Request body too large"}, status_code=413)

        for name, value in scope["headers"]:
            if name == b"content-length":
                try:
                    if int(value) > limit:
                        await too_large(scope, receive, send)
                        return
                except ValueError:
                    pass

        received = 0
        response_started = False
        exceeded = False

        async def limited_receive():
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    # Stop feeding the app; it sees a disconnect.
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message):
            nonlocal response_started
            if exceeded:
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            # Handlers reading the truncated body raise (e.g. ClientDisconnect).
            if not exceeded:
                raise
        if exceeded and not response_started:
            await too_large(scope, receive, send)


# --- rate limiting (in-process sliding window) ---

_hits: dict[tuple[str, str], deque] = defaultdict(deque)
_last_sweep = 0.0


def reset_rate_limits() -> None:
    global _last_sweep
    _hits.clear()
    _last_sweep = 0.0


def _enabled() -> bool:
    return os.environ.get("COLLAB_EDITOR_RATE_LIMIT_ENABLED", "true").strip().lower() not in _FALSE


def _client_ip(request: Request) -> str:
    # request.client is the direct peer; behind a reverse proxy this is
    # the proxy's address unless uvicorn --proxy-headers rewrites it.
    # COLLAB_EDITOR_TRUST_FORWARDED_FOR=true instead takes the LAST
    # X-Forwarded-For entry: the one our single trusted proxy appended
    # ($proxy_add_x_forwarded_for), which a client cannot forge.
    if os.environ.get("COLLAB_EDITOR_TRUST_FORWARDED_FOR", "").strip().lower() not in ("", *_FALSE):
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[-1].strip()
        if forwarded:
            return forwarded
    return request.client.host if request.client else "unknown"


def _sweep(now: float, window: int) -> None:
    """Drops keys with no hits inside the window, at most once per window,
    so memory stays bounded by recently-active keys."""
    global _last_sweep
    if now - _last_sweep < window:
        return
    _last_sweep = now
    for key in [k for k, d in _hits.items() if not d or now - d[-1] >= window]:
        del _hits[key]


def _check(key: tuple[str, str], limit: int) -> None:
    """Raises 429 if `key` is at its limit; otherwise records a hit."""
    window = _window_seconds()
    now = time.monotonic()
    _sweep(now, window)
    hits = _hits.get(key)
    if hits is not None:
        while hits and now - hits[0] >= window:
            hits.popleft()
        if not hits:
            del _hits[key]
            hits = None
    if hits is not None and len(hits) >= limit:
        retry_after = max(1, int(window - (now - hits[0])) + 1)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests",
            headers={"Retry-After": str(retry_after)},
        )
    _hits[key].append(now)


def _login_limit() -> int:
    return _env_int("COLLAB_EDITOR_LOGIN_RATE_LIMIT", 10)


async def login_rate_limit(request: Request) -> None:
    """Per-IP check that reserves a slot up front (so a parallel burst can't
    all pass before any failure is recorded); the login handler calls
    refund_login_hit() on success so successful logins never consume the
    budget. Caveat: while an IP is limited by failures, even a correct
    password gets 429 until the window expires."""
    limit = _login_limit()
    if not _enabled() or limit <= 0:
        return
    _check(("login", _client_ip(request)), limit)


def refund_login_hit(request: Request) -> None:
    hits = _hits.get(("login", _client_ip(request)))
    if hits:
        hits.pop()


async def join_rate_limit(request: Request) -> None:
    """Per-IP cap on guest joins (each join creates a user row)."""
    limit = _env_int("COLLAB_EDITOR_JOIN_RATE_LIMIT", 20)
    if not _enabled() or limit <= 0:
        return
    _check(("join", _client_ip(request)), limit)


async def admin_rate_limit(
    request: Request,
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Authenticated requests are bucketed per user id; unauthenticated or
    failed-auth requests go to a separate per-IP bucket (same limit), so they
    can never consume an authenticated admin's budget."""
    limit = _env_int("COLLAB_EDITOR_ADMIN_RATE_LIMIT", 120)
    if not _enabled() or limit <= 0:
        return
    user = await get_user_for_session_token(db, session_token)
    if user is not None:
        key = ("admin-user", str(user.id))
    else:
        key = ("admin-anon", _client_ip(request))
    _check(key, limit)
