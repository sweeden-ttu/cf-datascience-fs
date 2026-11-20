"""A stand-in OpenID Connect provider: RSA keys, a JWKS and a userinfo endpoint, all
served through the ``fetch`` hook of :class:`cf_datascience_fs._auth.OIDCAuth`."""

from __future__ import annotations

import json
import time
import uuid

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

ISSUER = "https://oidc.example.test"


class FakeProvider:
    def __init__(self, issuer: str = ISSUER):
        self.issuer = issuer
        self.keys = {}
        self.published = []
        self.calls = []
        self.opaque = {}  # opaque token -> userinfo claims
        self.down = False
        self.add_key()

    def add_key(self, publish: bool = True) -> str:
        kid = uuid.uuid4().hex
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.keys[kid] = private
        if publish:
            self.published.append(kid)
        return kid

    def jwks(self) -> dict:
        keys = []
        for kid in self.published:
            data = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.keys[kid].public_key()))
            data.update({"kid": kid, "use": "sig", "alg": "RS256"})
            keys.append(data)
        return {"keys": keys}

    def token(
        self,
        sub: str = "alice",
        *,
        aud="cfds-vscode",
        kid: str | None = None,
        lifetime: float = 300,
        issuer: str | None = None,
        algorithm: str = "RS256",
        **claims,
    ) -> str:
        now = time.time()
        payload = {
            "sub": sub,
            "aud": aud,
            "iss": issuer or self.issuer,
            "iat": int(now),
            "nbf": int(now),
            "exp": int(now + lifetime),
            "scope": "openid profile email",
            **claims,
        }
        kid = kid or self.published[0]
        return jwt.encode(payload, self.keys[kid], algorithm=algorithm, headers={"kid": kid})

    def fetch(self, url: str, headers: dict) -> tuple[int, bytes]:
        self.calls.append(url)
        if self.down:
            raise OSError("connection refused")
        if url == self.issuer + "/.well-known/openid-configuration":
            document = {
                "issuer": self.issuer,
                "jwks_uri": self.issuer + "/oauth2/jwks",
                "userinfo_endpoint": self.issuer + "/userinfo",
                "authorization_endpoint": self.issuer + "/oauth2/authorize",
                "token_endpoint": self.issuer + "/oauth2/token",
            }
            return 200, json.dumps(document).encode()
        if url == self.issuer + "/oauth2/jwks":
            return 200, json.dumps(self.jwks()).encode()
        if url == self.issuer + "/userinfo":
            token = headers.get("Authorization", "").partition(" ")[2]
            claims = self.opaque.get(token)
            if claims is None:
                return 401, b""
            return 200, json.dumps(claims).encode()
        return 404, b""


class IdPHandler:
    """Builds a real HTTP server for the browser parts of sign-in (authorize and token
    endpoints), issuing tokens signed by a :class:`FakeProvider`."""

    def __init__(self, provider: FakeProvider, client_id: str = "cfds-cli"):
        self.provider = provider
        self.client_id = client_id
        self.codes = {}  # code -> (challenge, redirect_uri, sub)
        self.refresh_tokens = {}  # refresh token -> sub
        self.token_requests = []
        self.sub = "alice"

    def start(self):
        import base64
        import hashlib
        import secrets
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from urllib.parse import parse_qs, urlencode, urlsplit

        idp = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                parts = urlsplit(self.path)
                if parts.path == "/.well-known/openid-configuration":
                    base = f"http://127.0.0.1:{self.server.server_address[1]}"
                    self._json(
                        200,
                        {
                            "issuer": base,
                            "authorization_endpoint": base + "/authorize",
                            "token_endpoint": base + "/token",
                        },
                    )
                elif parts.path == "/authorize":
                    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
                    assert query["code_challenge_method"] == "S256"
                    assert query["client_id"] == idp.client_id
                    code = secrets.token_urlsafe(8)
                    idp.codes[code] = (query["code_challenge"], query["redirect_uri"], idp.sub)
                    target = (
                        query["redirect_uri"]
                        + "?"
                        + urlencode({"code": code, "state": query["state"]})
                    )
                    self.send_response(302)
                    self.send_header("Location", target)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    self._json(404, {})

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
                idp.token_requests.append(form)
                if form.get("grant_type") == "authorization_code":
                    challenge, redirect_uri, sub = idp.codes.pop(form.get("code"), (None,) * 3)
                    verifier = form.get("code_verifier", "").encode()
                    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier).digest())
                    if challenge is None or expected.rstrip(b"=").decode() != challenge:
                        return self._json(400, {"error": "invalid_grant"})
                    if redirect_uri != form.get("redirect_uri"):
                        return self._json(400, {"error": "invalid_grant"})
                elif form.get("grant_type") == "refresh_token":
                    sub = idp.refresh_tokens.get(form.get("refresh_token"))
                    if sub is None:
                        return self._json(400, {"error": "invalid_grant"})
                else:
                    return self._json(400, {"error": "unsupported_grant_type"})
                refresh = secrets.token_urlsafe(8)
                idp.refresh_tokens[refresh] = sub
                self._json(
                    200,
                    {
                        "access_token": idp.provider.token(sub, aud=idp.client_id),
                        "token_type": "Bearer",
                        "expires_in": 300,
                        "refresh_token": refresh,
                    },
                )

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
