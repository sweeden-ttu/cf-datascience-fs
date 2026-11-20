// Client tests. Reads run against the real Python cfds-server from this repo
// (../src); writes run against a small in-memory server that implements the
// write half of the contract documented in src/client.ts.
import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import http from "node:http";
import net from "node:net";

const require = createRequire(import.meta.url);
const { CfdsClient, CfdsError, normPath } = require("../dist/test/client.js");
const repoRoot = path.resolve(import.meta.dirname, "..", "..");

test("normPath", () => {
  assert.equal(normPath("a//b/./c/"), "/a/b/c");
  assert.equal(normPath("/"), "/");
  assert.throws(() => normPath("/a/../b"), CfdsError);
});

// ---------- real Python server (read path) ----------
const python = process.env.PYTHON ?? "python3";
const havePython = spawnSync(python, ["-c", "import fsspec"], { env: { ...process.env, PYTHONPATH: path.join(repoRoot, "src") } }).status === 0;

function freePort() {
  return new Promise((r) => { const s = net.createServer(); s.listen(0, "127.0.0.1", () => { const p = s.address().port; s.close(() => r(p)); }); });
}

let py, pyUrl, rootDir;
const TOKEN = "test-token-123";
before(async () => {
  if (!havePython) return;
  rootDir = fs.mkdtempSync(path.join(os.tmpdir(), "cfds-root-"));
  fs.mkdirSync(path.join(rootDir, "ai-rig/kaggle-datasets/titanic"), { recursive: true });
  fs.writeFileSync(path.join(rootDir, "ai-rig/kaggle-datasets/titanic/train.csv"), "id,survived\n1,0\n2,1\n");
  const tokenFile = path.join(rootDir, "..", path.basename(rootDir) + ".token");
  fs.writeFileSync(tokenFile, TOKEN);
  const port = await freePort();
  py = spawn(python, ["-m", "cf_datascience_fs.server", "--root", rootDir, "--port", String(port), "--token-file", tokenFile, "--log-level", "WARNING"], {
    env: { ...process.env, PYTHONPATH: path.join(repoRoot, "src") }, stdio: "inherit",
  });
  pyUrl = `http://127.0.0.1:${port}`;
  for (let i = 0; i < 100; i++) {
    try { await fetch(pyUrl + "/v1/ls?path=/"); return; } catch { await new Promise((r) => setTimeout(r, 100)); }
  }
  throw new Error("python server did not start");
});
after(() => py?.kill());

test("reads from the real Python cfds-server with a bearer token", { skip: !havePython && "python3 with fsspec not available" }, async () => {
  const c = new CfdsClient({ baseUrl: pyUrl, getToken: async () => TOKEN });
  const listing = await c.list("/ai-rig/kaggle-datasets");
  assert.equal(listing.info.type, "directory");
  assert.deepEqual(listing.entries.map((e) => e.name), ["/ai-rig/kaggle-datasets/titanic"]);
  const info = await c.info("/ai-rig/kaggle-datasets/titanic/train.csv");
  assert.equal(info.type, "file");
  assert.equal(info.size, 20);
  assert.ok(info.etag);
  const all = new TextDecoder().decode(await c.read("/ai-rig/kaggle-datasets/titanic/train.csv"));
  assert.equal(all, "id,survived\n1,0\n2,1\n");
  const part = new TextDecoder().decode(await c.read("/ai-rig/kaggle-datasets/titanic/train.csv", { start: 3, end: 11 }));
  assert.equal(part, "survived");
  await assert.rejects(c.read("/ai-rig/kaggle-datasets/titanic/train.csv", { ifMatch: '"stale"' }), (e) => e.status === 412);
  const ok = await c.read("/ai-rig/kaggle-datasets/titanic/train.csv", { ifMatch: info.etag });
  assert.equal(ok.byteLength, 20);
});

test("maps server errors: 404 and 401", { skip: !havePython && "python3 with fsspec not available" }, async () => {
  const c = new CfdsClient({ baseUrl: pyUrl, getToken: async () => TOKEN });
  await assert.rejects(c.info("/nope"), (e) => e instanceof CfdsError && e.status === 404);
  const anon = new CfdsClient({ baseUrl: pyUrl });
  await assert.rejects(anon.list("/"), (e) => e instanceof CfdsError && e.status === 401);
});

test("on 401 asks for a fresh token once and retries", { skip: !havePython && "python3 with fsspec not available" }, async () => {
  const calls = [];
  const c = new CfdsClient({ baseUrl: pyUrl, getToken: async ({ forceRefresh }) => { calls.push(forceRefresh); return forceRefresh ? TOKEN : "stale"; } });
  const l = await c.list("/");
  assert.equal(l.info.type, "directory");
  assert.deepEqual(calls, [false, true]);
});

// ---------- in-memory server (write path of the contract) ----------
function memServer() {
  const files = new Map([["/home", null], ["/kaggle-datasets", null], ["/kaggle-datasets/a.csv", Buffer.from("a,b\n")]]); // null = directory
  const json = (res, status, obj) => { res.statusCode = status; res.setHeader("Content-Type", "application/json"); res.end(obj === undefined ? "" : JSON.stringify(obj)); };
  const err = (res, status, code) => json(res, status, { error: { code, message: code } });
  const requests = [];
  const s = http.createServer((req, res) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      const u = new URL(req.url, "http://x");
      const q = Object.fromEntries(u.searchParams);
      requests.push({ method: req.method, path: u.pathname, q, auth: req.headers.authorization, ifMatch: req.headers["if-match"] });
      if (req.headers.authorization !== "Bearer t") return err(res, 401, "unauthorized");
      const p = q.path;
      if (req.method === "PUT" && u.pathname === "/v1/file") {
        const exists = files.has(p);
        if (exists && q.overwrite === "0") return err(res, 409, "exists");
        if (!exists && q.create === "0") return err(res, 404, "not_found");
        if (p.startsWith("/kaggle-datasets/")) return err(res, 403, "read_only");
        files.set(p, Buffer.concat(chunks));
        return json(res, exists ? 200 : 201, { name: p, size: Buffer.concat(chunks).length, type: "file", mtime: 1, etag: '"e1"' });
      }
      if (req.method === "POST" && u.pathname === "/v1/mkdir") {
        if (files.has(p)) return err(res, 409, "exists");
        files.set(p, null); return json(res, 201, undefined);
      }
      if (req.method === "DELETE" && u.pathname === "/v1/file") {
        if (!files.has(p)) return err(res, 404, "not_found");
        files.delete(p); return json(res, 204, undefined);
      }
      if (req.method === "POST" && u.pathname === "/v1/mv") {
        if (!files.has(q.src)) return err(res, 404, "not_found");
        if (files.has(q.dst) && q.overwrite !== "1") return err(res, 409, "exists");
        files.set(q.dst, files.get(q.src)); files.delete(q.src); return json(res, 200, undefined);
      }
      if (req.method === "POST" && u.pathname === "/v1/cp") {
        if (!files.has(q.src)) return err(res, 404, "not_found");
        if (files.has(q.dst) && q.overwrite !== "1") return err(res, 409, "exists");
        files.set(q.dst, files.get(q.src)); return json(res, 201, { name: q.dst, size: files.get(q.dst).length, type: "file", mtime: 1, writable: true });
      }
      if (req.method === "GET" && u.pathname === "/v1/file" && p === "/short") {
        // Promise 100 bytes, send 4, then drop the connection (file changed mid-send).
        res.writeHead(200, { "Content-Length": "100" }); res.write("abcd");
        return setTimeout(() => res.socket.destroy(), 20);
      }
      if (req.method === "GET" && u.pathname === "/v1/me") {
        return json(res, 200, { user: { sub: "u1", email: "u1@example.com", name: null, roles: ["free"] }, quota: { limit: 107374182400, used: 5, available: 107374182395 }, home: "/home", datasets: "/kaggle-datasets", default_path: "/kaggle-datasets", mounts: [{ path: "/kaggle-datasets", writable: false }, { path: "/home", writable: true }] });
      }
      err(res, 404, "not_found");
    });
  });
  return { s, files, requests };
}

test("write contract: put, mkdir, move, delete, me, quota/readonly errors", async (t) => {
  const { s, files, requests } = memServer();
  await new Promise((r) => s.listen(0, "127.0.0.1", r));
  t.after(() => s.close());
  const c = new CfdsClient({ baseUrl: `http://127.0.0.1:${s.address().port}/`, getToken: async () => "t" });

  await c.mkdir("/home/notebooks");
  await assert.rejects(c.mkdir("/home/notebooks"), (e) => e.status === 409);

  const e = await c.write("/home/notebooks/a.py", new TextEncoder().encode("print(1)\n"), { create: true, overwrite: false });
  assert.equal(e.size, 9);
  assert.equal(files.get("/home/notebooks/a.py").toString(), "print(1)\n");
  await assert.rejects(c.write("/home/notebooks/a.py", new Uint8Array(), { create: true, overwrite: false }), (e) => e.status === 409);
  await c.write("/home/notebooks/a.py", new TextEncoder().encode("x"), { create: false, overwrite: true, ifMatch: '"e1"' });
  assert.equal(requests.at(-1).ifMatch, '"e1"');
  await assert.rejects(c.write("/kaggle-datasets/x.csv", new Uint8Array(), { create: true, overwrite: true }), (e) => e.status === 403 && e.code === "read_only");

  await c.move("/home/notebooks/a.py", "/home/notebooks/b.py", false);
  assert.ok(files.has("/home/notebooks/b.py") && !files.has("/home/notebooks/a.py"));
  await c.delete("/home/notebooks/b.py", false);
  assert.ok(!files.has("/home/notebooks/b.py"));
  assert.deepEqual(requests.filter((r) => r.method === "DELETE")[0].q, { path: "/home/notebooks/b.py", recursive: "0" });

  await c.copy("/kaggle-datasets/a.csv", "/home/a.csv", false);
  assert.equal(files.get("/home/a.csv").toString(), "a,b\n");
  await assert.rejects(c.copy("/kaggle-datasets/a.csv", "/home/a.csv", false), (e) => e.status === 409 && e.code === "exists");
  await c.mkdir("/home/x/y", true);
  assert.equal(requests.at(-1).q.parents, "1");

  await assert.rejects(c.read("/short"), (e) => e.status === 412 && e.code === "file_changed");

  const me = await c.me();
  assert.equal(me.quota.limit, 100 * 1024 ** 3);
  assert.equal(me.default_path, "/kaggle-datasets");
  assert.deepEqual(me.mounts.map((m) => m.path), ["/kaggle-datasets", "/home"]);
  assert.ok(requests.every((r) => r.auth === "Bearer t"));
});
