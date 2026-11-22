"""Access tokens for cfds servers: where they are stored and how they are refreshed.

``cfds login`` saves one record per server in ``credentials.json`` in the cfds config
directory (``$CFDS_CONFIG_DIR``, else ``$XDG_CONFIG_HOME/cfds`` or ``~/.config/cfds``;
``%APPDATA%\\cfds`` on Windows). The file is only readable by its owner.
"""

from __future__ import annotations

import json
import os
import string
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

from ._version import __version__

_TOKEN_CHARS = frozenset(string.ascii_letters + string.digits + "-._~+/=")
_DEFAULT_PORTS = {"http": 80, "https": 443}
#: Refresh a token this many seconds before it expires.
EXPIRY_MARGIN = 60


def check_token(token: str) -> str:
    if not token or not set(token) <= _TOKEN_CHARS:
        raise ValueError(
            "the token must be non-empty and use only letters, digits and -._~+/= characters"
        )
    return token


def config_dir() -> str:
    explicit = os.environ.get("CFDS_CONFIG_DIR")
    if explicit:
        return explicit
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        return os.path.join(os.environ["APPDATA"], "cfds")
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "cfds")


def credentials_path() -> str:
    return os.path.join(config_dir(), "credentials.json")


def server_key(url: str) -> str:
    """``HTTPS://Storage.Example:443/`` -> ``https://storage.example``."""
    parts = urllib.parse.urlsplit(url.strip())
    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"
    port = parts.port
    if port is not None and port != _DEFAULT_PORTS.get(scheme):
        host += f":{port}"
    return f"{scheme}://{host}"


_file_lock = threading.Lock()


def load_all() -> dict:
    try:
        with open(credentials_path(), encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load(server_url: str) -> dict | None:
    record = load_all().get(server_key(server_url))
    return record if isinstance(record, dict) else None


def save(server_url: str, record: dict | None) -> None:
    """Store (or with ``None`` remove) the record for one server, atomically."""
    with _file_lock:
        data = load_all()
        key = server_key(server_url)
        if record is None:
            data.pop(key, None)
        else:
            data[key] = record
        directory = config_dir()
        os.makedirs(directory, mode=0o700, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".credentials-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
            os.chmod(temporary, 0o600)
            os.replace(temporary, credentials_path())
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise


def post_form(url: str, form: dict, timeout: float = 30) -> tuple[int, dict]:
    body = urllib.parse.urlencode(form).encode("ascii")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": f"cf-datascience-fs/{__version__}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            status, raw = response.status, response.read(1024 * 1024)
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read(1024 * 1024)
    try:
        payload = json.loads(raw) if raw else {}
    except ValueError:
        payload = {}
    return status, payload if isinstance(payload, dict) else {}


def record_from_token_response(
    payload: dict, *, issuer: str, token_endpoint: str, client_id: str, previous: dict | None = None
) -> dict:
    access = payload.get("access_token")
    if not isinstance(access, str) or not access:
        raise ValueError("the sign-in provider sent no access token")
    check_token(access)
    record = {
        "access_token": access,
        "issuer": issuer,
        "token_endpoint": token_endpoint,
        "client_id": client_id,
    }
    expires_in = payload.get("expires_in")
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        record["expires_at"] = time.time() + float(expires_in)
    refresh = payload.get("refresh_token") or (previous or {}).get("refresh_token")
    if isinstance(refresh, str) and refresh:
        record["refresh_token"] = refresh
    return record


class TokenSource:
    """Finds the bearer token for one server.

    In order: the explicit ``token`` (a string, or a callable returning one), then
    ``$CFDS_TOKEN``, then the record saved by ``cfds login`` (refreshed when it is about
    to expire, if the provider gave a refresh token).
    """

    def __init__(
        self,
        server_url: str,
        token: str | Callable[[], str | None] | None = None,
        *,
        use_saved: bool = True,
    ):
        self.server_url = server_key(server_url)
        if isinstance(token, str):
            check_token(token)
        self._explicit = token
        self.use_saved = use_saved
        self._lock = threading.Lock()

    def get(self) -> str | None:
        if callable(self._explicit):
            token = self._explicit()
            return check_token(token) if token else None
        if self._explicit is not None:
            return self._explicit
        token = os.environ.get("CFDS_TOKEN") or None
        if token is not None:
            return check_token(token)
        if not self.use_saved:
            return None
        record = load(self.server_url)
        if record is None:
            return None
        expires_at = record.get("expires_at")
        if isinstance(expires_at, (int, float)) and expires_at - EXPIRY_MARGIN < time.time():
            refreshed = self.refresh(record)
            if refreshed is not None:
                return refreshed
        token = record.get("access_token")
        return token if isinstance(token, str) and token else None

    @property
    def can_refresh(self) -> bool:
        if self._explicit is not None or os.environ.get("CFDS_TOKEN") or not self.use_saved:
            return False
        record = load(self.server_url)
        return bool(record and record.get("refresh_token") and record.get("token_endpoint"))

    def refresh(self, record: dict | None = None) -> str | None:
        """Use the saved refresh token. Returns the new access token, or ``None``."""
        with self._lock:
            record = load(self.server_url) if record is None else record
            if not record or not record.get("refresh_token") or not record.get("token_endpoint"):
                return None
            current = load(self.server_url) or {}
            if current.get("access_token") not in (None, record.get("access_token")):
                return current["access_token"]  # another process refreshed it already
            try:
                status, payload = post_form(
                    record["token_endpoint"],
                    {
                        "grant_type": "refresh_token",
                        "refresh_token": record["refresh_token"],
                        "client_id": record.get("client_id", ""),
                    },
                )
            except OSError:
                return None
            if status != 200:
                return None
            try:
                new = record_from_token_response(
                    payload,
                    issuer=record.get("issuer", ""),
                    token_endpoint=record["token_endpoint"],
                    client_id=record.get("client_id", ""),
                    previous=record,
                )
            except ValueError:
                return None
            save(self.server_url, new)
            return new["access_token"]
