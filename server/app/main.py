import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from server.app.db import init_db
from server.app.routers import admin, auth, documents, presence, sync


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    async with sync.sync_lifespan():
        yield


app = FastAPI(lifespan=lifespan)

# Only needed for a split-origin deployment (frontend served from a
# different host/port than the API) or local dev against the Vite dev
# server - a same-origin production deploy (reverse proxy serving both the
# built frontend and /api on one domain) doesn't hit CORS at all, so the
# dev-only defaults here are harmless in that case.
_default_origins = "http://localhost:5173,http://localhost:5174"
allow_origins = [o.strip() for o in os.environ.get("COLLAB_EDITOR_CORS_ORIGINS", _default_origins).split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/api")
app.include_router(admin.router, prefix="/api")
app.include_router(documents.router, prefix="/api")
app.include_router(presence.router, prefix="/api")
app.include_router(sync.router)
