// In-memory implementation of the cfds HTTP API v1, for driving the extension
// inside a real VS Code. Layout as the real server shows it: / (virtual),
// /kaggle-datasets (read-only), /home (writable).
import http from "node:http";

export function startFakeServer(token) {
  const now = () => Date.now() / 1000;
  const nodes = new Map(); // path -> {type, data?, mtime, v}
  const dir = (p, ro) => nodes.set(p, { type: "directory", mtime: now(), ro });
  const file = (p, s, ro) => nodes.set(p, { type: "file", data: Buffer.from(s), mtime: now(), v: 1, ro });
  dir("/", true); dir("/kaggle-datasets", true); dir("/kaggle-datasets/titanic", true); dir("/home", false);
  file("/kaggle-datasets/titanic/train.csv", "id,survived\n1,0\n2,1\n", true);
  const parent = (p) => p.slice(0, p.lastIndexOf("/")) || "/";
  const etag = (n) => `"v${n.v}-${n.data.length}"`;
  const entry = (p) => {
    const n = nodes.get(p);
    const e = { name: p, size: n.type === "file" ? n.data.length : 0, type: n.type, mtime: n.mtime, islink: false, writable: !n.ro };
    if (n.type === "file") e.etag = etag(n);
    return e;
  };
  const children = (p) => [...nodes.keys()].filter((k) => k !== "/" && parent(k) === p).sort();
  const readOnly = (p) => p === "/" || p === "/home" || p === "/kaggle-datasets" || p.startsWith("/kaggle-datasets/");
  const used = () => [...nodes.entries()].filter(([k, n]) => k.startsWith("/home/") && n.type === "file").reduce((a, [, n]) => a + n.data.length, 0);

  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      const send = (status, obj, headers = {}) => {
        res.writeHead(status, { "Content-Type": "application/json", ...headers });
        res.end(obj === undefined ? undefined : JSON.stringify(obj));
      };
      const err = (status, code) => send(status, { error: { code, message: code } });
      if (req.headers.authorization !== `Bearer ${token}`) return err(401, "unauthorized");
      const u = new URL(req.url, "http://x");
      const q = Object.fromEntries(u.searchParams);
      const p = q.path;
      const route = `${req.method} ${u.pathname}`;
      server.log.push(`${route} ${p ?? `${q.src}->${q.dst}`}`);

      if (route === "GET /v1/me") {
        return send(200, { user: { sub: "alice", email: "alice@example.com", name: "Alice", roles: ["free"] },
          quota: { limit: 107374182400, used: used(), available: 107374182400 - used() },
          home: "/home", datasets: "/kaggle-datasets", default_path: "/kaggle-datasets",
          mounts: [{ path: "/kaggle-datasets", writable: false, description: "Kaggle datasets (shared, read-only)" }, { path: "/home", writable: true, description: "Your files" }] });
      }
      if (route === "GET /v1/ls") {
        if (!nodes.has(p)) return err(404, "not_found");
        const n = nodes.get(p);
        return send(200, { info: entry(p), entries: n.type === "directory" && q.list !== "0" ? children(p).map(entry) : null });
      }
      if (route === "GET /v1/file") {
        const n = nodes.get(p);
        if (!n) return err(404, "not_found");
        if (n.type !== "file") return err(400, "is_a_directory");
        if (req.headers["if-match"] && req.headers["if-match"] !== etag(n)) return err(412, "precondition_failed");
        res.writeHead(200, { "Content-Type": "application/octet-stream", "Content-Length": n.data.length, ETag: etag(n) });
        return res.end(n.data);
      }
      if (route === "PUT /v1/file") {
        const n = nodes.get(p);
        if (readOnly(p)) return err(403, "read_only");
        if (!nodes.has(parent(p))) return err(404, "parent_not_found");
        if (n && q.overwrite === "0") return err(409, "exists");
        if (!n && q.create === "0") return err(404, "not_found");
        if (n && n.type !== "file") return err(400, "is_a_directory");
        if (n && req.headers["if-match"] && req.headers["if-match"] !== etag(n)) return err(412, "precondition_failed");
        nodes.set(p, { type: "file", data: Buffer.concat(chunks), mtime: now(), v: (n?.v ?? 0) + 1 });
        return send(n ? 200 : 201, entry(p), { ETag: etag(nodes.get(p)) });
      }
      if (route === "POST /v1/mkdir") {
        if (readOnly(p)) return err(403, "read_only");
        if (nodes.has(p)) return q.parents === "1" && nodes.get(p).type === "directory" ? send(200, entry(p)) : err(409, "exists");
        if (!nodes.has(parent(p))) return err(404, "parent_not_found");
        dir(p, false);
        return send(201, entry(p));
      }
      if (route === "DELETE /v1/file") {
        if (readOnly(p)) return err(403, "read_only");
        if (!nodes.has(p)) return err(404, "not_found");
        const kids = [...nodes.keys()].filter((k) => k.startsWith(p + "/"));
        if (kids.length && q.recursive !== "1") return err(409, "not_empty");
        for (const k of [p, ...kids]) nodes.delete(k);
        res.writeHead(204); return res.end();
      }
      if (route === "POST /v1/mv" || route === "POST /v1/cp") {
        const { src, dst } = q;
        if (!nodes.has(src)) return err(404, "not_found");
        if (readOnly(dst) || (route === "POST /v1/mv" && readOnly(src))) return err(403, "read_only");
        if (!nodes.has(parent(dst))) return err(404, "parent_not_found");
        if (nodes.has(dst) && q.overwrite !== "1") return err(409, "exists");
        const s = nodes.get(src);
        if (route === "POST /v1/cp") {
          if (s.type !== "file") return err(400, "is_a_directory");
          const existed = nodes.has(dst);
          nodes.set(dst, { type: "file", data: Buffer.from(s.data), mtime: now(), v: (nodes.get(dst)?.v ?? 0) + 1 });
          return send(existed ? 200 : 201, entry(dst));
        }
        for (const k of [...nodes.keys()].filter((k) => k === src || k.startsWith(src + "/"))) {
          nodes.set(dst + k.slice(src.length), nodes.get(k)); nodes.delete(k);
        }
        return send(200, entry(dst));
      }
      err(404, "not_found");
    });
  });
  server.log = [];
  server.nodes = nodes;
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve(server)));
}
