import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import http from "node:http";

const require = createRequire(import.meta.url);
const oidc = require("../dist/test/oidc.js");

test("S256 code challenge matches RFC 7636 appendix B", async () => {
  const challenge = await oidc.codeChallenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk");
  assert.equal(challenge, "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM");
});

test("authorization URL carries PKCE, state, nonce and prompt=create for sign-up", () => {
  const d = { issuer: "https://oidc.example", authorization_endpoint: "https://oidc.example/authorize", token_endpoint: "https://oidc.example/token" };
  const u = new URL(oidc.authorizationUrl(d, {
    clientId: "cfds-vscode", redirectUri: "vscode://cloudfronts.cfds-storage/did-authenticate",
    scopes: ["openid", "email"], state: "s1", nonce: "n1", challenge: "c1", signUp: true,
  }));
  assert.equal(u.searchParams.get("response_type"), "code");
  assert.equal(u.searchParams.get("code_challenge_method"), "S256");
  assert.equal(u.searchParams.get("code_challenge"), "c1");
  assert.equal(u.searchParams.get("scope"), "openid email");
  assert.equal(u.searchParams.get("prompt"), "create");
  assert.equal(u.searchParams.get("redirect_uri"), "vscode://cloudfronts.cfds-storage/did-authenticate");
});

test("decodeJwtPayload reads unpadded base64url claims", () => {
  const payload = Buffer.from(JSON.stringify({ sub: "u-1", email: "ä@example.com" })).toString("base64url");
  assert.deepEqual(oidc.decodeJwtPayload(`h.${payload}.s`), { sub: "u-1", email: "ä@example.com" });
});

test("discovery, code exchange and refresh against a fake issuer", async (t) => {
  const seen = [];
  const server = http.createServer((req, res) => {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      const base = `http://127.0.0.1:${server.address().port}`;
      if (req.url === "/.well-known/openid-configuration") {
        res.setHeader("Content-Type", "application/json");
        return res.end(JSON.stringify({ issuer: base, authorization_endpoint: base + "/authorize", token_endpoint: base + "/token" }));
      }
      if (req.url === "/token") {
        const p = new URLSearchParams(body);
        seen.push(Object.fromEntries(p));
        res.setHeader("Content-Type", "application/json");
        if (p.get("grant_type") === "authorization_code") {
          return res.end(JSON.stringify({ access_token: "at1", refresh_token: "rt1", expires_in: 300, id_token: "x.e30.y" }));
        }
        if (p.get("refresh_token") === "rt1") return res.end(JSON.stringify({ access_token: "at2", expires_in: 300 }));
        res.statusCode = 400;
        return res.end(JSON.stringify({ error: "invalid_grant", error_description: "expired" }));
      }
      res.statusCode = 404; res.end();
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  t.after(() => server.close());
  const issuer = `http://127.0.0.1:${server.address().port}`;

  const d = await oidc.discover(issuer + "/");
  const t1 = await oidc.exchangeCode(d, { clientId: "cfds-vscode", code: "C", redirectUri: "vscode://x/cb", verifier: "V" });
  assert.equal(t1.accessToken, "at1");
  assert.equal(t1.refreshToken, "rt1");
  assert.ok(t1.expiresAt > Date.now());
  assert.deepEqual(seen[0], { grant_type: "authorization_code", client_id: "cfds-vscode", code: "C", redirect_uri: "vscode://x/cb", code_verifier: "V" });

  const t2 = await oidc.refresh(d, { clientId: "cfds-vscode", refreshToken: "rt1" });
  assert.equal(t2.accessToken, "at2");
  assert.equal(t2.refreshToken, "rt1", "keeps the old refresh token when not rotated");

  await assert.rejects(oidc.refresh(d, { clientId: "cfds-vscode", refreshToken: "bad" }), /invalid_grant \(expired\)/);
});
