"""Who is calling: bearer token checks for ``cfds-server``.

Three modes:

``NoAuth``
    Anyone who can reach the server. For local use.
``StaticTokenAuth``
    One shared token (``--token-file`` / ``$CFDS_TOKEN``).
``OIDCAuth``
    Access tokens from an OpenID Connect provider. JWTs are verified locally against
    the provider's signing keys (``iss``, ``exp``/``nbf`` and ``aud`` are checked);
    other tokens are sent to the provider's ``userinfo`` endpoint. Needs PyJWT with
    the ``crypto`` extra (``pip install cf-datascience-fs[server]``).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import string
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Callable

from ._fsops import HTTPError
from ._version import __version__

logger = logging.getLogger("cf_datascience_fs.server")

_TOKEN_CHARS = frozenset(string.ascii_letters + string.digits + "-._~+/=")
#: Asymmetric algorithms only: "none" and shared-secret HS* are never accepted.
_ALGORITHMS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512")
_MAX_DOCUMENT = 1024 * 1024

#: ``fetch(url, headers) -> (status, body)``. Replaceable for tests.
Fetch = Callable[[str, dict], "tuple[int, bytes]"]


@dataclass(frozen=True)
class Identity:
    sub: str
    email: str | None = None
    name: str | None = None
    roles: tuple[str, ...] = field(default_factory=tuple)

    def public(self) -> dict:
        return {"sub": self.sub, "email": self.email, "name": self.name, "roles": list(self.roles)}


ANONYMOUS = Identity("anonymous")


def validate_token(token: str) -> str:
    """Bearer tokens must be plain ASCII (RFC 6750) so every HTTP client sends the same bytes."""
    if not token or not set(token) <= _TOKEN_CHARS:
        raise ValueError(
            "the token must be non-empty and use only letters, digits and -._~+/= characters"
        )
    return token


def unauthorized(message: str, *, error: str | None = "invalid_token") -> HTTPError:
    """401. The server adds the ``WWW-Authenticate`` header."""
    exc = HTTPError(HTTPStatus.UNAUTHORIZED, "unauthorized", message)
    exc.bearer_error = error
    return exc


def _unavailable(message: str) -> HTTPError:
    return HTTPError(
        HTTPStatus.SERVICE_UNAVAILABLE,
        "auth_unavailable",
        message,
        headers={"Retry-After": "10"},
    )


def default_fetch(url: str, headers: dict, timeout: float = 10.0) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url, headers={"User-Agent": f"cfds-server/{__version__}", **headers}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status, response.read(_MAX_DOCUMENT)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(_MAX_DOCUMENT)


class NoAuth:
    required = False
    description = "no authentication"

    def authenticate(self, token: str | None) -> Identity:
        return ANONYMOUS


class StaticTokenAuth:
    required = True
    description = "shared bearer token"

    def __init__(self, token: str):
        self._token = validate_token(token).encode("ascii")

    def authenticate(self, token: str | None) -> Identity:
        if token is None:
            raise unauthorized("a bearer token is required", error=None)
        # Header values arrive decoded as Latin-1; tokens are restricted to ASCII.
        if not hmac.compare_digest(token.encode("latin-1", "replace"), self._token):
            raise unauthorized("invalid bearer token")
        return Identity("token")


def _roles(claims: dict) -> tuple[str, ...]:
    roles = claims.get("roles")
    if isinstance(roles, str):
        roles = roles.split()
    if not isinstance(roles, list):
        return ()
    return tuple(str(role) for role in roles if isinstance(role, (str, int)))


def _text(value) -> str | None:
    return value if isinstance(value, str) and value else None


def identity_from_claims(claims: dict) -> Identity:
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        raise unauthorized("the token has no subject")
    name = _text(claims.get("name")) or _text(claims.get("preferred_username"))
    return Identity(sub, _text(claims.get("email")), name, _roles(claims))


class OIDCAuth:
    required = True

    def __init__(
        self,
        issuer: str,
        audiences: tuple[str, ...] | list[str] = (),
        *,
        fetch: Fetch | None = None,
        leeway: float = 60.0,
        jwks_ttl: float = 3600.0,
        userinfo_ttl: float = 300.0,
        clock: Callable[[], float] = time.time,
    ):
        try:
            import jwt  # noqa: F401
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise RuntimeError(
                "OIDC sign-in needs PyJWT: pip install 'cf-datascience-fs[server]'"
            ) from exc
        self.issuer = issuer.rstrip("/")
        self.audiences = tuple(audiences)
        self.description = f"OpenID Connect ({self.issuer})"
        self._fetch = fetch or default_fetch
        self.leeway = leeway
        self.jwks_ttl = jwks_ttl
        self.userinfo_ttl = userinfo_ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._config: dict | None = None
        self._config_failed_at = float("-inf")
        self._keys: dict = {}
        self._keys_fetched = float("-inf")
        self._userinfo_cache: dict[str, tuple[float, dict | None]] = {}

    # -- provider metadata ----------------------------------------------------------

    def _get_json(self, url: str, headers: dict | None = None) -> tuple[int, dict | None]:
        try:
            status, body = self._fetch(url, headers or {})
        except (OSError, ValueError) as exc:
            logger.warning("cannot reach %s: %s", url, exc)
            raise _unavailable("the sign-in provider cannot be reached") from exc
        try:
            document = json.loads(body) if body else None
        except ValueError:
            document = None
        return status, document if isinstance(document, dict) else None

    def configuration(self) -> dict:
        with self._lock:
            if self._config is not None:
                return self._config
            if self._clock() - self._config_failed_at < 10:
                raise _unavailable("the sign-in provider cannot be reached")
        url = self.issuer + "/.well-known/openid-configuration"
        try:
            status, document = self._get_json(url)
            if status != 200 or document is None:
                raise _unavailable(
                    f"the sign-in provider's metadata is unavailable (HTTP {status})"
                )
            if str(document.get("issuer", "")).rstrip("/") != self.issuer:
                raise _unavailable("the sign-in provider reports a different issuer")
            if not document.get("jwks_uri"):
                raise _unavailable("the sign-in provider publishes no signing keys")
        except HTTPError:
            with self._lock:
                self._config_failed_at = self._clock()
            raise
        with self._lock:
            self._config = document
        return document

    def _refresh_keys(self, force: bool) -> None:
        import jwt

        now = self._clock()
        with self._lock:
            age = now - self._keys_fetched
            # An unknown key id refreshes at most once a minute, so random key ids in
            # forged tokens cannot make us hammer the provider.
            if age < (60 if force else self.jwks_ttl):
                return
            self._keys_fetched = now
        status, document = self._get_json(self.configuration()["jwks_uri"])
        if status != 200 or document is None:
            raise _unavailable(f"the sign-in provider's keys are unavailable (HTTP {status})")
        keys = {}
        for data in document.get("keys", []):
            if data.get("use", "sig") != "sig":
                continue
            try:
                keys[data.get("kid")] = (jwt.PyJWK.from_dict(data), data.get("alg"))
            except (jwt.PyJWTError, ValueError, KeyError, TypeError):
                logger.debug("skipping unusable key %r", data.get("kid"))
        with self._lock:
            self._keys = keys

    def _key_for(self, kid):
        self._refresh_keys(force=False)
        found = self._keys.get(kid)
        if found is None and kid is None and len(self._keys) == 1:
            found = next(iter(self._keys.values()))
        if found is None:
            self._refresh_keys(force=True)
            found = self._keys.get(kid)
        return found

    # -- tokens ---------------------------------------------------------------------

    def authenticate(self, token: str | None) -> Identity:
        if token is None:
            raise unauthorized("sign in to get an access token", error=None)
        if token.count(".") == 2:
            return identity_from_claims(self.verify_jwt(token))
        claims = self.userinfo(token)
        if claims is None:
            raise unauthorized("the access token is not valid")
        return identity_from_claims(claims)

    def verify_jwt(self, token: str) -> dict:
        import jwt

        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise unauthorized("malformed access token") from exc
        algorithm = header.get("alg")
        if algorithm not in _ALGORITHMS:
            raise unauthorized(f"token algorithm {algorithm!r} is not accepted")
        found = self._key_for(header.get("kid"))
        if found is None:
            raise unauthorized("the token was signed with an unknown key")
        key, key_algorithm = found
        if key_algorithm and key_algorithm != algorithm:
            raise unauthorized("the token's algorithm does not match its key")
        options = {"require": ["exp", "iss", "sub"], "verify_aud": bool(self.audiences)}
        try:
            return jwt.decode(
                token,
                key=key.key,
                algorithms=[algorithm],
                audience=list(self.audiences) or None,
                issuer=self.issuer,
                leeway=self.leeway,
                options=options,
            )
        except jwt.ExpiredSignatureError as exc:
            raise unauthorized("the access token has expired") from exc
        except jwt.InvalidAudienceError as exc:
            raise unauthorized("the access token was issued for another application") from exc
        except (jwt.PyJWTError, ValueError, TypeError) as exc:
            raise unauthorized(f"invalid access token: {exc}") from exc

    def userinfo(self, token: str) -> dict | None:
        """Claims from the provider's ``userinfo`` endpoint, or ``None`` if it refuses
        the token. Answers are cached for a few minutes."""
        digest = hashlib.sha256(token.encode("latin-1", "replace")).hexdigest()
        now = self._clock()
        with self._lock:
            cached = self._userinfo_cache.get(digest)
        if cached is not None and cached[0] > now:
            return cached[1]
        endpoint = self.configuration().get("userinfo_endpoint")
        if not endpoint:
            return None
        status, claims = self._get_json(endpoint, {"Authorization": f"Bearer {token}"})
        if status in (401, 403):
            claims, ttl = None, 30.0
        elif status == 200 and claims is not None and claims.get("sub"):
            ttl = self.userinfo_ttl
        else:
            raise _unavailable(f"the sign-in provider's userinfo failed (HTTP {status})")
        with self._lock:
            if len(self._userinfo_cache) > 10000:
                self._userinfo_cache = {
                    key: value for key, value in self._userinfo_cache.items() if value[0] > now
                }
            self._userinfo_cache[digest] = (now + ttl, claims)
        return claims
