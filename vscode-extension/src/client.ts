// HTTP client for the cfds storage server. No VS Code imports, so it runs in
// Node, in a browser web worker, and in plain `node --test`.
//
// Contract: cfds HTTP API v1 (docs/API.md in the server work; shared by the
// Python client, this extension and the iOS app):
//   GET    /v1/ls?path=&list=0|1               -> {info, entries}
//   GET    /v1/file?path=                      -> bytes (Range, If-Match, ETag)
//   PUT    /v1/file?path=&create=&overwrite=   body = whole file -> entry
//   POST   /v1/mkdir?path=[&parents=1]         -> entry
//   DELETE /v1/file?path=&recursive=0|1        -> 204
//   POST   /v1/mv?src=&dst=&overwrite=0|1      -> entry (same mount only)
//   POST   /v1/cp?src=&dst=&overwrite=0|1      -> entry (files only)
//   GET    /v1/me                              -> {user, quota, home, datasets, default_path, mounts}
// Errors: {"error": {"code", "message"}}; branch on status + code.

export type EntryType = "file" | "directory" | "other";

export interface Entry {
  name: string;
  size: number;
  type: EntryType;
  mtime: number; // seconds since epoch
  ctime?: number;
  islink?: boolean;
  etag?: string;
  /** false when the signed-in user may not change this path. */
  writable?: boolean;
}

export interface Listing {
  info: Entry;
  entries: Entry[] | null;
}

export interface Mount {
  path: string;
  writable: boolean;
  description?: string;
}

export interface Me {
  user: { sub: string; email?: string | null; name?: string | null; roles?: string[] | null };
  quota?: { limit: number; used: number; available?: number };
  home?: string;
  datasets?: string;
  default_path?: string;
  mounts?: Mount[];
}

export const DEFAULT_DATASETS_PATH = "/kaggle-datasets";

export class CfdsError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "CfdsError";
  }
}

export type TokenSource = (opts: { forceRefresh: boolean }) => Promise<string | undefined>;

export interface ClientOptions {
  baseUrl: string;
  getToken?: TokenSource;
  fetchImpl?: typeof fetch;
  userAgent?: string;
}

/** Normalise a path to the server's form: leading slash, no trailing slash, no "..". */
export function normPath(p: string): string {
  const parts: string[] = [];
  for (const seg of p.split("/")) {
    if (seg === "" || seg === ".") continue;
    if (seg === "..") throw new CfdsError(400, "bad_path", `'..' is not allowed: ${p}`);
    parts.push(seg);
  }
  return "/" + parts.join("/");
}

export class CfdsClient {
  private readonly base: string;
  private readonly fetchImpl: typeof fetch;

  constructor(private readonly opts: ClientOptions) {
    this.base = opts.baseUrl.replace(/\/+$/, "");
    this.fetchImpl = opts.fetchImpl ?? ((...a) => fetch(...a));
  }

  private url(endpoint: string, params: Record<string, string>): string {
    const qs = new URLSearchParams(params).toString();
    return `${this.base}${endpoint}${qs ? "?" + qs : ""}`;
  }

  /** Sends a request with the bearer token; on 401 refreshes the token once and retries. */
  private async request(
    method: string,
    endpoint: string,
    params: Record<string, string>,
    init: { body?: Uint8Array; headers?: Record<string, string> } = {},
  ): Promise<Response> {
    for (let attempt = 0; attempt < 2; attempt++) {
      const headers: Record<string, string> = { ...(init.headers ?? {}) };
      const token = await this.opts.getToken?.({ forceRefresh: attempt > 0 });
      if (token) headers["Authorization"] = `Bearer ${token}`;
      if (this.opts.userAgent) headers["User-Agent"] = this.opts.userAgent;
      const res = await this.fetchImpl(this.url(endpoint, params), {
        method,
        headers,
        body: init.body,
      });
      if (res.status === 401 && attempt === 0 && this.opts.getToken) {
        await res.body?.cancel().catch(() => undefined);
        continue;
      }
      if (!res.ok) throw await toError(res);
      return res;
    }
    throw new CfdsError(401, "unauthorized", "Not signed in");
  }

  async list(path: string): Promise<Listing> {
    const res = await this.request("GET", "/v1/ls", { path: normPath(path) });
    return (await res.json()) as Listing;
  }

  async info(path: string): Promise<Entry> {
    const res = await this.request("GET", "/v1/ls", { path: normPath(path), list: "0" });
    return ((await res.json()) as Listing).info;
  }

  /**
   * Reads a file, or `[start, end)` of it. With `ifMatch`, fails with 412 if the
   * file is no longer that version. A body cut short (the server closes the
   * connection when the file changes mid-send) also becomes 412 file_changed.
   */
  async read(
    path: string,
    opts: { start?: number; end?: number; ifMatch?: string } = {},
  ): Promise<Uint8Array> {
    const headers: Record<string, string> = {};
    if (opts.start !== undefined || opts.end !== undefined) {
      headers["Range"] = `bytes=${opts.start ?? 0}-${opts.end !== undefined ? opts.end - 1 : ""}`;
    }
    if (opts.ifMatch) headers["If-Match"] = opts.ifMatch;
    const res = await this.request("GET", "/v1/file", { path: normPath(path) }, { headers });
    const expected = Number(res.headers.get("Content-Length") ?? NaN);
    let body: Uint8Array;
    try {
      body = new Uint8Array(await res.arrayBuffer());
    } catch {
      throw new CfdsError(412, "file_changed", `${path} changed while it was being read`);
    }
    if (Number.isFinite(expected) && body.byteLength !== expected) {
      throw new CfdsError(412, "file_changed", `${path} changed while it was being read`);
    }
    return body;
  }

  async write(
    path: string,
    data: Uint8Array,
    opts: { create: boolean; overwrite: boolean; ifMatch?: string },
  ): Promise<Entry | undefined> {
    const headers: Record<string, string> = { "Content-Type": "application/octet-stream" };
    if (opts.ifMatch) headers["If-Match"] = opts.ifMatch;
    const res = await this.request(
      "PUT",
      "/v1/file",
      { path: normPath(path), create: flag(opts.create), overwrite: flag(opts.overwrite) },
      { body: data, headers },
    );
    return jsonOrUndefined<Entry>(res);
  }

  async mkdir(path: string, parents = false): Promise<void> {
    const params: Record<string, string> = { path: normPath(path) };
    if (parents) params.parents = "1";
    await this.request("POST", "/v1/mkdir", params);
  }

  async delete(path: string, recursive: boolean): Promise<void> {
    await this.request("DELETE", "/v1/file", { path: normPath(path), recursive: flag(recursive) });
  }

  async move(src: string, dst: string, overwrite: boolean): Promise<void> {
    await this.request("POST", "/v1/mv", {
      src: normPath(src),
      dst: normPath(dst),
      overwrite: flag(overwrite),
    });
  }

  /** Server-side copy of one file. */
  async copy(src: string, dst: string, overwrite: boolean): Promise<void> {
    await this.request("POST", "/v1/cp", {
      src: normPath(src),
      dst: normPath(dst),
      overwrite: flag(overwrite),
    });
  }

  async me(): Promise<Me> {
    const res = await this.request("GET", "/v1/me", {});
    return (await res.json()) as Me;
  }
}

function flag(b: boolean): string {
  return b ? "1" : "0";
}

async function jsonOrUndefined<T>(res: Response): Promise<T | undefined> {
  const text = await res.text();
  if (!text) return undefined;
  try {
    return JSON.parse(text) as T;
  } catch {
    return undefined;
  }
}

async function toError(res: Response): Promise<CfdsError> {
  let code = `http_${res.status}`;
  let message = res.statusText || `HTTP ${res.status}`;
  try {
    const body = (await res.json()) as { error?: { code?: string; message?: string } };
    if (body?.error?.code) code = body.error.code;
    if (body?.error?.message) message = body.error.message;
  } catch {
    // Non-JSON error body; keep the status text.
  }
  return new CfdsError(res.status, code, message);
}

export function formatBytes(n: number): string {
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i++;
  }
  return `${n.toFixed(n >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}
