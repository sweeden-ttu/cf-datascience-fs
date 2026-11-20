// OpenID Connect helpers: discovery, PKCE, token exchange and refresh.
// No VS Code imports, so these can be unit tested with `node --test`.

export interface Discovery {
  issuer: string;
  authorization_endpoint: string;
  token_endpoint: string;
  end_session_endpoint?: string;
  revocation_endpoint?: string;
}

export interface TokenSet {
  accessToken: string;
  refreshToken?: string;
  idToken?: string;
  /** ms since epoch */
  expiresAt: number;
  scope?: string;
}

interface TokenResponse {
  access_token: string;
  token_type?: string;
  expires_in?: number;
  refresh_token?: string;
  id_token?: string;
  scope?: string;
  error?: string;
  error_description?: string;
}

async function subtle(): Promise<SubtleCrypto> {
  const g = globalThis as { crypto?: Crypto };
  if (g.crypto?.subtle) return g.crypto.subtle;
  // Node 18 extension hosts do not always expose the global; fall back to node:crypto.
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const nodeCrypto = require("crypto") as { webcrypto: Crypto };
  return nodeCrypto.webcrypto.subtle;
}

function randomBytes(n: number): Uint8Array {
  const g = globalThis as { crypto?: Crypto };
  if (g.crypto?.getRandomValues) return g.crypto.getRandomValues(new Uint8Array(n));
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const nodeCrypto = require("crypto") as { randomBytes(n: number): Uint8Array };
  return new Uint8Array(nodeCrypto.randomBytes(n));
}

export function base64url(bytes: Uint8Array): string {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function randomString(byteLength = 32): string {
  return base64url(randomBytes(byteLength));
}

/** RFC 7636 S256 code challenge for a verifier. */
export async function codeChallenge(verifier: string): Promise<string> {
  const digest = await (await subtle()).digest("SHA-256", new TextEncoder().encode(verifier));
  return base64url(new Uint8Array(digest));
}

/** Decodes a JWT payload without verifying it (the server verifies; we only read display claims). */
export function decodeJwtPayload(jwt: string): Record<string, unknown> {
  const part = jwt.split(".")[1];
  if (!part) return {};
  const b64 = part.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((part.length + 3) % 4);
  try {
    const bin = atob(b64);
    const bytes = Uint8Array.from(bin, (c) => c.charCodeAt(0));
    return JSON.parse(new TextDecoder().decode(bytes)) as Record<string, unknown>;
  } catch {
    return {};
  }
}

export async function discover(issuer: string, fetchImpl: typeof fetch = fetch): Promise<Discovery> {
  const url = issuer.replace(/\/+$/, "") + "/.well-known/openid-configuration";
  const res = await fetchImpl(url);
  if (!res.ok) throw new Error(`OIDC discovery failed (${res.status}) at ${url}`);
  const doc = (await res.json()) as Discovery;
  if (!doc.authorization_endpoint || !doc.token_endpoint) {
    throw new Error(`OIDC discovery document at ${url} is missing endpoints`);
  }
  return doc;
}

export function authorizationUrl(
  d: Discovery,
  p: {
    clientId: string;
    redirectUri: string;
    scopes: string[];
    state: string;
    nonce: string;
    challenge: string;
    signUp?: boolean;
  },
): string {
  const q = new URLSearchParams({
    response_type: "code",
    client_id: p.clientId,
    redirect_uri: p.redirectUri,
    scope: p.scopes.join(" "),
    state: p.state,
    nonce: p.nonce,
    code_challenge: p.challenge,
    code_challenge_method: "S256",
  });
  // OIDC "prompt=create" (Initiating User Registration) sends new users to sign-up.
  if (p.signUp) q.set("prompt", "create");
  const sep = d.authorization_endpoint.includes("?") ? "&" : "?";
  return d.authorization_endpoint + sep + q.toString();
}

async function tokenRequest(
  d: Discovery,
  body: Record<string, string>,
  fetchImpl: typeof fetch,
): Promise<TokenSet> {
  const res = await fetchImpl(d.token_endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded", Accept: "application/json" },
    body: new URLSearchParams(body).toString(),
  });
  let json: TokenResponse;
  try {
    json = (await res.json()) as TokenResponse;
  } catch {
    throw new Error(`Token endpoint returned ${res.status} with a non-JSON body`);
  }
  if (!res.ok || json.error || !json.access_token) {
    throw new Error(
      `Token request failed: ${json.error ?? res.status}${json.error_description ? ` (${json.error_description})` : ""}`,
    );
  }
  return {
    accessToken: json.access_token,
    refreshToken: json.refresh_token,
    idToken: json.id_token,
    scope: json.scope,
    expiresAt: Date.now() + (json.expires_in ?? 3600) * 1000,
  };
}

export function exchangeCode(
  d: Discovery,
  p: { clientId: string; code: string; redirectUri: string; verifier: string },
  fetchImpl: typeof fetch = fetch,
): Promise<TokenSet> {
  return tokenRequest(
    d,
    {
      grant_type: "authorization_code",
      client_id: p.clientId,
      code: p.code,
      redirect_uri: p.redirectUri,
      code_verifier: p.verifier,
    },
    fetchImpl,
  );
}

export async function refresh(
  d: Discovery,
  p: { clientId: string; refreshToken: string },
  fetchImpl: typeof fetch = fetch,
): Promise<TokenSet> {
  const t = await tokenRequest(
    d,
    { grant_type: "refresh_token", client_id: p.clientId, refresh_token: p.refreshToken },
    fetchImpl,
  );
  // Providers that do not rotate refresh tokens omit it; keep the old one.
  return { ...t, refreshToken: t.refreshToken ?? p.refreshToken };
}
