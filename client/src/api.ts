// Same-origin by default (works out of the box when a reverse proxy serves
// the built frontend and proxies /api on one domain) - override at build
// time with VITE_API_BASE for a split-origin deployment (frontend and
// backend on different hosts/ports).
const API_BASE = import.meta.env.VITE_API_BASE ?? `${window.location.origin}/api`;

export interface NodeOut {
  id: string;
  parent_id: string | null;
  name: string;
  kind: "folder" | "document";
  created_at: string;
  updated_at: string;
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  let resp: Response;
  try {
    resp = await fetch(`${API_BASE}${path}`, {
      ...options,
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        ...(options.headers ?? {}),
      },
    });
  } catch {
    throw new ApiError(0, "Could not reach the server. Check your connection and try again.");
  }
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const body = await resp.json();
      if (typeof body.detail === "string") {
        detail = body.detail;
      } else if (Array.isArray(body.detail)) {
        // FastAPI/Pydantic 422 validation errors: detail is a list of
        // {msg, loc, ...}, not a plain string.
        detail = body.detail.map((d: { msg?: string }) => d.msg ?? String(d)).join("; ");
      }
    } catch {
      // ignore non-JSON error bodies
    }
    throw new ApiError(resp.status, detail);
  }
  if (resp.status === 204) {
    return undefined as T;
  }
  return (await resp.json()) as T;
}

export interface CurrentUser {
  id: number;
  username: string;
  display_name: string;
  is_admin: boolean;
  is_active: boolean;
  created_at: string;
}

export function login(username: string, password: string): Promise<CurrentUser> {
  return request("/login", { method: "POST", body: JSON.stringify({ username, password }) });
}

export function logout(): Promise<{ ok: boolean }> {
  return request("/logout", { method: "POST" });
}

export function me(): Promise<CurrentUser> {
  return request("/me");
}

export interface PresenceEntry {
  user_id: number;
  display_name: string;
  doc_id: string;
  doc_name: string;
}

export function presence(): Promise<PresenceEntry[]> {
  return request("/presence");
}

export function getTree(): Promise<NodeOut[]> {
  return request("/tree");
}

export function createFolder(name: string, parent_id: string | null): Promise<NodeOut> {
  return request("/folders", { method: "POST", body: JSON.stringify({ name, parent_id }) });
}

export function createDocument(name: string, parent_id: string | null): Promise<NodeOut> {
  return request("/documents", { method: "POST", body: JSON.stringify({ name, parent_id }) });
}

export function getDocumentContent(id: string): Promise<{ content: string }> {
  return request(`/documents/${id}/content`);
}

export function putDocumentContent(id: string, content: string): Promise<NodeOut> {
  return request(`/documents/${id}/content`, { method: "PUT", body: JSON.stringify({ content }) });
}

export function updateNode(
  id: string,
  payload: { name?: string; parent_id?: string; clear_parent?: boolean },
): Promise<NodeOut> {
  return request(`/nodes/${id}`, { method: "PATCH", body: JSON.stringify(payload) });
}

export function deleteNode(id: string): Promise<void> {
  return request(`/nodes/${id}`, { method: "DELETE" });
}

export interface ChatMessageOut {
  id: string;
  doc_id: string;
  user_id: number;
  display_name: string;
  body: string;
  sent_at: string;
}

export function getDocumentChat(
  docId: string,
  options: { limit?: number; beforeId?: string } = {},
): Promise<ChatMessageOut[]> {
  const params = new URLSearchParams();
  if (options.limit !== undefined) params.set("limit", String(options.limit));
  if (options.beforeId !== undefined) params.set("before_id", options.beforeId);
  const query = params.toString();
  return request(`/documents/${docId}/chat${query ? `?${query}` : ""}`);
}

export interface ImportZipResult {
  root: NodeOut;
  skipped: string[];
}

// Can't go through request(): it unconditionally sets Content-Type:
// application/json, but a multipart body needs the browser to set its own
// Content-Type with a boundary parameter - setting it manually would break
// the upload.
export async function importZip(file: File, parentId: string | null): Promise<ImportZipResult> {
  const formData = new FormData();
  formData.append("file", file);
  const params = new URLSearchParams();
  if (parentId !== null) params.set("parent_id", parentId);
  const query = params.toString();

  let resp: Response;
  try {
    resp = await fetch(`${API_BASE}/import-zip${query ? `?${query}` : ""}`, {
      method: "POST",
      credentials: "include",
      body: formData,
    });
  } catch {
    throw new ApiError(0, "Could not reach the server. Check your connection and try again.");
  }
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const body = await resp.json();
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      // ignore non-JSON error bodies
    }
    throw new ApiError(resp.status, detail);
  }
  return (await resp.json()) as ImportZipResult;
}

// Can't go through request(): the response body is a zip file, not JSON.
export async function exportZip(nodeId: string | null): Promise<{ blob: Blob; filename: string }> {
  const params = new URLSearchParams();
  if (nodeId !== null) params.set("node_id", nodeId);
  const query = params.toString();

  let resp: Response;
  try {
    resp = await fetch(`${API_BASE}/export-zip${query ? `?${query}` : ""}`, {
      credentials: "include",
    });
  } catch {
    throw new ApiError(0, "Could not reach the server. Check your connection and try again.");
  }
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const body = await resp.json();
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      // ignore non-JSON error bodies
    }
    throw new ApiError(resp.status, detail);
  }

  const disposition = resp.headers.get("Content-Disposition") ?? "";
  const match = /filename="([^"]+)"/.exec(disposition);
  const filename = match ? match[1] : "export.zip";
  return { blob: await resp.blob(), filename };
}
