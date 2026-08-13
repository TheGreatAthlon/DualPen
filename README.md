# Collaborative Accessible Text Editor

A self-hosted, real-time collaborative plaintext editor built to be fully keyboard- and
screen-reader-accessible, so blind and sighted collaborators can edit documents together.

## Features

- Real-time collaborative editing (Yjs CRDT sync over WebSocket), with remote cursor
  rendering and a "one document open per user" rule enforced server-side.
- Presence sounds and a jump-to-collaborator shortcut, designed for non-visual awareness
  of where collaborators are relative to your own cursor.
- Persistent per-document chat (F2 quick-send, Shift+F2 for the full panel).
- A file tree with full keyboard navigation (arrow keys, type-ahead, move/rename/delete),
  built to the W3C APG tree pattern.
- Per-user accessibility mode, font, and presence-sound settings.
- Zip import (creates a labeled subfolder and recreates the archive's structure) and
  zip export (whole tree or a selected folder).
- Server-wide AES-256-GCM encryption at rest for document content, argon2id-hashed
  passwords, and admin-managed accounts (no self-signup).

## Stack

- **Backend:** Python (FastAPI + Uvicorn), [pycrdt](https://github.com/y-crdt/pycrdt) /
  [pycrdt-websocket](https://github.com/jupyter-server/pycrdt-websocket) for CRDT sync,
  SQLite for metadata (users, tree, chat), filesystem for encrypted document blobs.
- **Frontend:** TypeScript + Vite, [Monaco Editor](https://microsoft.github.io/monaco-editor/),
  [Yjs](https://docs.yjs.dev/) + [y-monaco](https://github.com/yjs/y-monaco) for collaborative editing.

## Local development

Requirements: Python 3.11+, Node 20+.

**Backend:**

```bash
cd server
python -m venv ../.venv
../.venv/bin/activate      # or ..\.venv\Scripts\activate on Windows
pip install -r requirements.txt
python -m server.cli create-admin   # first-run only, interactive prompts
uvicorn server.app.main:app --reload --port 8000
```

Run from the **repository root**, not `server/` — the app is imported as `server.app.main`
and reads/writes `server_data/` relative to the repo root.

**Frontend:**

```bash
cd client
npm install
npm run dev
```

Vite serves on `http://localhost:5173` (or `5174` if `5173` is taken — both are allowed
by the backend's default CORS config) and talks to the backend at `http://localhost:8000`
automatically; no configuration needed for local dev.

Run the test suite with `python -m pytest` from `server/` (or `server/tests/` — see
`server/pytest.ini`).

## Deploying on your own server

This has not yet been run through a real production deployment, but the pieces needed for
one are in place. The architecture below is the recommended shape; adjust as needed.

### Architecture

One VPS, one process each:

- **uvicorn** runs the FastAPI backend, bound to `127.0.0.1` on some port (`8000` in the
  examples below) — never exposed directly to the internet.
- **nginx** (or another reverse proxy) terminates TLS, serves the built frontend's static
  files directly, and proxies `/api/*` and `/ws/*` to uvicorn. Keeping frontend and backend
  on the same public origin (e.g. `https://editor.example.com/`) is the simplest setup:
  the frontend's API/WebSocket URLs default to same-origin, so no frontend build
  configuration is required in this case.

If you'd rather run the frontend and backend on separate origins (e.g. a static host for
the frontend, a different host/port for the API), see **Split-origin deployment** below —
it needs two extra environment variables and a CORS setting.

### 1. Get the code onto the server

```bash
git clone <your-fork-or-repo-url> collab-editor
cd collab-editor
```

### 2. Backend setup

```bash
sudo apt update
sudo apt install -y python3 python3-venv

python3 -m venv .venv
.venv/bin/pip install -r server/requirements.txt
```

`server/requirements.txt` includes the test dependencies (`pytest`, `pytest-asyncio`,
`httpx`) alongside the runtime ones — harmless to install, just some extra disk space if
you'd rather trim it down yourself.

**Runtime data** lives in `server_data/` at the repo root (SQLite database, encrypted
document blobs, and the master encryption key), created automatically on first run. To
point these somewhere else instead (e.g. a separate data volume), set:

| Variable | Default |
|---|---|
| `COLLAB_EDITOR_DATABASE_URL` | `sqlite+aiosqlite:///<repo>/server_data/db/app.db` |
| `COLLAB_EDITOR_DOCSTORE_PATH` | `<repo>/server_data/docstore` |
| `COLLAB_EDITOR_MASTER_KEY_PATH` | `<repo>/server_data/master.key` |

**The master encryption key is generated automatically** the first time anything is
encrypted or decrypted — there's no manual key-generation step. **Back this file up.**
Every document is encrypted at rest with it; if it's lost, encrypted documents on disk
are unrecoverable. It's written with `0600` permissions on Linux.

Create the first admin account (interactive — do this over your SSH session, not
scripted, since it prompts for username/display name/password):

```bash
.venv/bin/python -m server.cli create-admin
```

Additional users are created afterward from the admin account, via the app's admin API
(no UI for this yet — it's reachable at `/api/admin/users`, gated by `is_admin`).

### 3. Frontend build

Same-origin deployment (recommended — frontend and API on one domain):

```bash
cd client
npm install
npm run build
```

This produces `client/dist/`, a static site with no build-time configuration needed —
it talks to whatever origin it's served from.

### 4. systemd service for the backend

`/etc/systemd/system/collab-editor.service`:

```ini
[Unit]
Description=Collab Editor backend
After=network.target

[Service]
Type=simple
User=collab-editor
WorkingDirectory=/opt/collab-editor
ExecStart=/opt/collab-editor/.venv/bin/uvicorn server.app.main:app --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Create a dedicated non-root user to own the deployment and its `server_data/` directory
(`sudo useradd -r -s /bin/false collab-editor`, then `chown -R collab-editor:collab-editor
/opt/collab-editor`), then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now collab-editor
sudo systemctl status collab-editor
```

### 5. nginx

The WebSocket route (`/ws/doc/{doc_id}`) authenticates using the session cookie set by
the regular login flow, so nginx must forward cookies (default behavior) and correctly
proxy the WebSocket upgrade — plain `proxy_pass` does **not** do this on its own.

```nginx
server {
    listen 443 ssl;
    server_name editor.example.com;

    ssl_certificate     /etc/letsencrypt/live/editor.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/editor.example.com/privkey.pem;

    root /opt/collab-editor/client/dist;
    index index.html;

    location / {
        try_files $uri /index.html;
    }

    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    location /ws/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 3600s;  # long-lived collaboration sessions
    }
}

server {
    listen 80;
    server_name editor.example.com;
    return 301 https://$host$request_uri;
}
```

[certbot](https://certbot.eff.org/) (`sudo apt install certbot python3-certbot-nginx`) is
the easiest way to get the TLS certificate this config references.

### 6. CORS

Only relevant if you're doing a split-origin deployment (see below) or want to allow a
non-default local dev origin. For the same-origin setup above, the default CORS config
(`localhost:5173`/`5174`, for local dev) is simply unused — the browser never sends a
cross-origin request in the first place, so nothing needs changing.

### Split-origin deployment

If the frontend and backend are on different origins:

- **Backend:** set `COLLAB_EDITOR_CORS_ORIGINS` to a comma-separated list of the frontend's
  origin(s), e.g. `COLLAB_EDITOR_CORS_ORIGINS=https://editor.example.com` in the systemd
  unit's `Environment=` line (or an `EnvironmentFile=`).
- **Frontend:** set `VITE_API_BASE` and `VITE_WS_BASE` before running `npm run build`, e.g.:
  ```bash
  VITE_API_BASE=https://api.example.com/api VITE_WS_BASE=wss://api.example.com npm run build
  ```

### Known limitations to be aware of before going live

- The session cookie is `HttpOnly`/`SameSite=Lax` but not marked `Secure` — harmless as
  long as TLS is terminated at the reverse proxy (the browser-facing connection is what
  matters), but don't serve this directly over plain HTTP in production.
- There's no admin UI yet — account management is via the CLI (first admin only) and the
  `/api/admin/*` REST endpoints directly.
- `create-admin` has no non-interactive/scripted mode (no flags, no env vars) — it's
  meant for one manual run over SSH.
- No rate limiting, no request body size cap, and no import size/entry-count limits exist
  anywhere in the stack. Fine for a small trusted group; don't expose this to the open
  internet as-is if that's a concern for you.
- Single-process, in-memory room registry for realtime sync — sized for a small trusted
  group, not for horizontal scaling.
