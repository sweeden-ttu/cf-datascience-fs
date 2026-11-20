"""HTTP server for the :mod:`cf_datascience_fs` client (``cfds://``).

The protocol is described in ``docs/API.md``. In short, under ``/v1``:

``GET /ls?path=P``                  JSON description of ``P`` and its entries
``GET /file?path=P``                the file's bytes (``Range``, ``If-Match``, ``If-Range``)
``PUT /file?path=P``                create or replace a file atomically
``DELETE /file?path=P``             delete a file or directory
``POST /mkdir``, ``/mv``, ``/cp``   make a directory, rename, copy one file
``GET /me``                         who the caller is, their quota and their mounts

There are two ways to run it:

Single tree (``--root``)
    One directory at ``/``, read-only unless ``--writable``. Optionally protected by a
    shared token.
Multi-user (``--users-root``)
    Every caller signs in with an OpenID Connect provider (``--oidc-issuer``) and sees
    ``/kaggle-datasets`` (shared, read-only) and ``/home`` (their own space, with a
    quota). ``/`` is virtual and lists the two.

This module imports only the standard library, plus PyJWT when ``--oidc-issuer`` is used.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import re
import socket
import ssl
import sys
import threading
from email.utils import formatdate
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import _fsops
from ._auth import Identity, NoAuth, OIDCAuth, StaticTokenAuth, validate_token
from ._fsops import (
    HTTPError,
    delete_path,
    describe,
    is_within,
    make_directory,
    make_etag,
    move_path,
    open_regular_file,
    parse_range,
    read_only,
    resolve_path,
    write_file,
)
from ._namespace import Mount, Namespace
from ._users import DEFAULT_QUOTA, UserStore
from ._version import __version__

__all__ = [
    "CFDSServer",
    "HTTPError",
    "describe",
    "is_within",
    "main",
    "make_etag",
    "make_server",
    "open_regular_file",
    "parse_range",
    "resolve_path",
    "validate_token",
]

logger = logging.getLogger("cf_datascience_fs.server")

API_PREFIX = "/v1"
DEFAULT_ROOT = "/mnt/TMP"
DEFAULT_DATASETS_ROOT = "/mnt/TMP/ai-rig/kaggle-datasets"
DATASETS_MOUNT = "/kaggle-datasets"
HOME_MOUNT = "/home"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
#: Client IDs registered at the OIDC provider for the cfds apps.
DEFAULT_AUDIENCES = ("cfds-vscode", "cfds-ios", "cfds-cli")
#: Browser origins allowed by default when callers must sign in (VS Code for the Web).
DEFAULT_CORS_ORIGINS = (
    "https://vscode.dev",
    "https://insiders.vscode.dev",
    "https://*.vscode-cdn.net",
)
CHUNK_SIZE = 1024 * 1024
#: Seconds a connection may sit idle (or stall mid-transfer) before it is dropped.
SOCKET_TIMEOUT = 120
#: Connections served at once. Each one costs a thread and up to two descriptors.
DEFAULT_MAX_CONNECTIONS = 256
#: How much of an unwanted request body is read and thrown away before closing.
MAX_DRAIN = 1024 * 1024
PROTECTED_RESOURCE_PATH = "/.well-known/oauth-protected-resource"

_READ = ("GET", "HEAD")
_ROUTES = {
    f"{API_PREFIX}/ls": _READ,
    f"{API_PREFIX}/file": ("GET", "HEAD", "PUT", "DELETE"),
    f"{API_PREFIX}/me": _READ,
    f"{API_PREFIX}/mkdir": ("POST",),
    f"{API_PREFIX}/mv": ("POST",),
    f"{API_PREFIX}/cp": ("POST",),
    PROTECTED_RESOURCE_PATH: _READ,
}
_CORS_ALLOW_HEADERS = "Authorization, Range, If-Match, If-None-Match, If-Range, Content-Type"
_CORS_EXPOSE_HEADERS = (
    "ETag, Content-Length, Content-Range, Last-Modified, Accept-Ranges, WWW-Authenticate"
)
_HEX = re.compile(rb"[0-9A-Fa-f]{1,16}")


def _origin_pattern(origin: str) -> re.Pattern:
    """``https://*.example.com`` matches any subdomain; everything else matches exactly."""
    origin = origin.rstrip("/").lower()
    scheme, sep, host = origin.partition("://")
    if not sep or not scheme or not host:
        raise ValueError(f"not an origin (scheme://host[:port]): {origin!r}")
    if host.startswith("*."):
        return re.compile(re.escape(scheme) + r"://[a-z0-9-]+(\.[a-z0-9-]+)*" + re.escape(host[1:]))
    return re.compile(re.escape(origin))


class CFDSServer(ThreadingHTTPServer):
    """Threaded HTTP server for one directory tree, or for many users."""

    daemon_threads = True
    request_queue_size = 128

    def __init__(
        self,
        address: tuple[str, int],
        root: str | os.PathLike | None = None,
        token: str | None = None,
        ssl_context: ssl.SSLContext | None = None,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
        allowed_hosts: tuple[str, ...] | list[str] = (),
        *,
        writable: bool = False,
        auth=None,
        users: UserStore | None = None,
        datasets_root: str | os.PathLike | None = None,
        datasets_writer_role: str | None = None,
        public_url: str | None = None,
        cors_origins: tuple[str, ...] | list[str] | None = None,
    ):
        if auth is not None and token:
            raise ValueError("give either a token or an auth object, not both")
        self.auth = auth or (StaticTokenAuth(token) if token else NoAuth())
        self.users = users
        if users is None:
            if root is None:
                raise ValueError("a root directory is required")
            root = os.fspath(root)
            if not os.path.isdir(root):
                raise NotADirectoryError(f"root is not a directory: {root}")
            self.real_root = os.path.realpath(root)
            self.writable = bool(writable)
            self.datasets_root = None
            self._single = Namespace(
                [Mount("/", self.real_root, self.writable, "Served directory")]
            )
        else:
            self.real_root = users.users_root
            self.writable = True
            self._single = None
            self.datasets_root = None
            if datasets_root is not None:
                datasets_root = os.fspath(datasets_root)
                if not os.path.isdir(datasets_root):
                    raise NotADirectoryError(f"datasets root is not a directory: {datasets_root}")
                self.datasets_root = os.path.realpath(datasets_root)
                if is_within(self.datasets_root, self.real_root) or is_within(
                    self.real_root, self.datasets_root
                ):
                    raise ValueError("the datasets root and the users root must not overlap")
        self.datasets_writer_role = datasets_writer_role
        self.public_url = public_url.rstrip("/") if public_url else None
        if cors_origins is None:
            cors_origins = DEFAULT_CORS_ORIGINS if self.auth.required else ()
        self._cors = [_origin_pattern(origin) for origin in cors_origins]
        if max_connections < 1:
            raise ValueError("max_connections must be at least 1")
        self.max_connections = int(max_connections)
        self._slots = threading.BoundedSemaphore(self.max_connections)
        self._connections: set = set()
        self._connections_lock = threading.Lock()
        self.allowed_hosts = self._default_hosts() | {host.lower() for host in allowed_hosts}
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, CFDSRequestHandler)
        if ssl_context is not None:
            # The handshake is deferred to the handler thread so that one slow client
            # cannot block the accept loop.
            self.socket = ssl_context.wrap_socket(
                self.socket, server_side=True, do_handshake_on_connect=False
            )
        self.uses_tls = ssl_context is not None

    # -- configuration --------------------------------------------------------------

    @property
    def token(self) -> str | None:
        """Kept for compatibility: whether a shared token is configured (not its value)."""
        return "configured" if isinstance(self.auth, StaticTokenAuth) else None

    @property
    def read_only(self) -> bool:
        return self.users is None and not self.writable

    @property
    def multi_user(self) -> bool:
        return self.users is not None

    def namespace_for(self, identity: Identity) -> Namespace:
        if self._single is not None:
            return self._single
        mounts = []
        if self.datasets_root is not None:
            writer = self.datasets_writer_role is not None and (
                self.datasets_writer_role in identity.roles
            )
            mounts.append(
                Mount(
                    DATASETS_MOUNT,
                    self.datasets_root,
                    writable=writer,
                    description="Kaggle datasets (shared, read-only)",
                )
            )
        home, quota = self.users.home(identity.sub, identity.email, identity.name)
        mounts.append(Mount(HOME_MOUNT, home, True, "Your files", quota))
        return Namespace(mounts)

    def cors_allowed(self, origin: str | None) -> bool:
        if not origin:
            return False
        origin = origin.strip().lower()
        return any(pattern.fullmatch(origin) for pattern in self._cors)

    @staticmethod
    def _default_hosts() -> set[str]:
        hosts = {"localhost"}
        try:
            hosts.add(socket.gethostname().lower())
            hosts.add(socket.getfqdn().lower())
        except OSError:
            pass
        return hosts

    def host_allowed(self, host_header: str | None) -> bool:
        """Whether an anonymous request with this ``Host`` header may be served.

        A web page on another site can make a visitor's browser send requests to this
        server by pointing its own DNS name at the server's address ("DNS rebinding").
        Such requests always carry the attacker's domain in ``Host``. IP addresses,
        single-label names, this machine's own names and names passed as
        ``allowed_hosts`` are accepted; other dotted names are not. Only enforced when
        callers do not authenticate, since a browser cannot supply a token by itself.
        """
        if self.auth.required:
            return True
        if not host_header:
            return True  # HTTP/1.0 clients; browsers always send Host
        try:
            hostname = urlsplit("//" + host_header.strip()).hostname or ""
        except ValueError:
            return False
        hostname = hostname.lower().rstrip(".")
        if hostname in self.allowed_hosts or "." not in hostname and ":" not in hostname:
            return True
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            return False
        return True

    @property
    def port(self) -> int:
        return self.server_address[1]

    # -- connection accounting ----------------------------------------------------

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            # Full. Dropping the connection keeps one client (or a port scanner) from
            # using up every thread and descriptor.
            logger.warning(
                "connection limit (%d) reached; refusing %s",
                self.max_connections,
                client_address[0],
            )
            self.shutdown_request(request)
            return
        with self._connections_lock:
            self._connections.add(request)
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._release(request)
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release(request)

    def _release(self, request) -> None:
        with self._connections_lock:
            if request not in self._connections:
                return
            self._connections.discard(request)
        self._slots.release()

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError, socket.timeout, ssl.SSLError)):
            logger.debug("connection from %s dropped: %s", client_address[0], exc)
        else:
            logger.exception("error handling a request from %s", client_address[0])

    def server_close(self):
        """Stop listening and drop every connection that is still open."""
        super().server_close()
        with self._connections_lock:
            connections = list(self._connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self.users is not None:
            self.users.close()


class CFDSRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"cfds-server/{__version__}"
    sys_version = ""
    timeout = SOCKET_TIMEOUT
    # Send headers and body together and without Nagle's algorithm. Otherwise every
    # small response waits ~40 ms for the client's delayed ACK.
    wbufsize = 64 * 1024
    disable_nagle_algorithm = True

    server: CFDSServer
    _response_started = False
    _cors_origin: str | None = None
    _body_pending = False
    _body_started = False
    _body_iter = None
    _continue_sent = False

    # -- plumbing ---------------------------------------------------------------

    def log_message(self, format, *args):  # noqa: A002 - signature set by the base class
        message = (format % args).encode("unicode_escape").decode("ascii")
        logger.info("%s %s", self.address_string(), message)

    def send_response(self, code, message=None):
        self._response_started = True
        super().send_response(code, message)

    def end_headers(self):
        if self._cors_origin:
            self.send_header("Access-Control-Allow-Origin", self._cors_origin)
            self.send_header("Access-Control-Expose-Headers", _CORS_EXPOSE_HEADERS)
            self.send_header("Vary", "Origin")
        if self.close_connection:
            # Tell the client, so it does not try to reuse a connection we are closing.
            self.send_header("Connection", "close")
        super().end_headers()

    def handle_expect_100(self):
        # The go-ahead is sent later, by an upload, once its checks have passed.
        return True

    def send_error(self, code, message=None, explain=None):
        """Protocol-level errors raised by the base class, in the same JSON shape as ours."""
        self.close_connection = True
        self._cors_origin = None
        if code == HTTPStatus.NOT_IMPLEMENTED:
            code, error_code = HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed"
            message = "unsupported method"
            headers = {"Allow": self._allow_header(None)}
        else:
            error_code, headers = "bad_request", {}
        if not getattr(self, "command", None):
            self.command = "GET"  # the request line was not parsed; a body is still fine
        if getattr(self, "request_version", "HTTP/0.9") == "HTTP/0.9":
            # Older Pythons leave an unparsable request marked HTTP/0.9, which makes
            # send_response() skip the status line and headers altogether.
            self.request_version = "HTTP/1.0"
        self._send_error_json(HTTPError(code, error_code, message or "bad request", headers))

    def _send_json(self, status: int, payload, headers: dict | None = None) -> None:
        body = json.dumps(payload).encode("ascii")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_empty(self, status: int, headers: dict | None = None) -> None:
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        if status != HTTPStatus.NO_CONTENT:
            self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_error_json(self, error: HTTPError) -> None:
        headers = dict(error.headers)
        if error.status == HTTPStatus.UNAUTHORIZED:
            headers["WWW-Authenticate"] = self._www_authenticate(error)
        payload = {"error": {"code": error.code, "message": error.message}}
        self._send_json(error.status, payload, headers)

    def _www_authenticate(self, error: HTTPError) -> str:
        value = 'Bearer realm="cfds"'
        bearer_error = getattr(error, "bearer_error", None)
        if bearer_error:
            value += f', error="{bearer_error}"'
        if isinstance(self.server.auth, OIDCAuth):
            value += f', resource_metadata="{self._public_url()}{PROTECTED_RESOURCE_PATH}"'
        return value

    def _public_url(self) -> str:
        if self.server.public_url:
            return self.server.public_url
        host = self.headers.get("Host") or f"{self.server.server_address[0]}:{self.server.port}"
        host = "".join(ch for ch in host if ch.isalnum() or ch in "-.:[]")
        return f"{'https' if self.server.uses_tls else 'http'}://{host}"

    def _bearer_token(self) -> str | None:
        header = self.headers.get("Authorization")
        if header is None:
            return None
        scheme, _, token = header.strip().partition(" ")
        if scheme.lower() != "bearer":
            return ""  # present but unusable: fails verification
        return token.strip()

    def _param(self, params: dict, name: str, default: str | None = None) -> str:
        values = params.get(name)
        if not values:
            if default is not None:
                return default
            raise HTTPError(HTTPStatus.BAD_REQUEST, "bad_request", f"missing '{name}' parameter")
        if len(values) > 1:
            raise HTTPError(HTTPStatus.BAD_REQUEST, "bad_request", f"'{name}' given more than once")
        return values[0]

    def _path_param(self, params: dict, default: str | None = None) -> str:
        return self._param(params, "path", default)

    def _flag(self, params: dict, name: str, default: bool) -> bool:
        values = params.get(name)
        if not values:
            return default
        value = values[-1].strip().lower()
        if value in ("1", "true", "yes"):
            return True
        if value in ("0", "false", "no"):
            return False
        raise HTTPError(HTTPStatus.BAD_REQUEST, "bad_request", f"'{name}' must be 0 or 1")

    # -- request bodies -----------------------------------------------------------

    def _has_request_body(self) -> bool:
        """True if a body might follow the headers, including when we cannot be sure."""
        if self.headers.defects or self.headers.get("Transfer-Encoding"):
            return True
        lengths = self.headers.get_all("Content-Length", [])
        return len(lengths) > 1 or any(value.strip() != "0" for value in lengths)

    def _drain_request_body(self) -> None:
        """Read and discard a small request body so the client can read our refusal."""
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not lengths[0].strip().isdigit():
            return
        if "100-continue" in self.headers.get("Expect", "").lower() and not self._continue_sent:
            return  # the client is waiting for a go-ahead and has not sent the body
        remaining = min(int(lengths[0]), MAX_DRAIN)
        try:
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 64 * 1024))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            pass

    def _discard_rest_of_body(self) -> None:
        """After an upload failed part-way, read (a bounded amount of) what the client is
        still sending, so that closing the connection does not reset it before our
        error response has been read."""
        if self._body_iter is None:
            return
        discarded = 0
        try:
            for chunk in self._body_iter:
                discarded += len(chunk)
                if discarded > MAX_DRAIN:
                    break
        except (HTTPError, OSError):
            pass

    def _send_continue(self) -> None:
        """Answer ``Expect: 100-continue`` once an upload has passed its checks."""
        if self._continue_sent or self.request_version < "HTTP/1.1":
            return
        if "100-continue" not in self.headers.get("Expect", "").lower():
            return
        self._continue_sent = True
        self.wfile.write(b"HTTP/1.1 100 Continue\r\n\r\n")
        self.wfile.flush()

    def _request_body(self):
        """Return ``(declared_length_or_None, chunk_iterator)`` for an upload."""
        bad = HTTPError(HTTPStatus.BAD_REQUEST, "bad_request", "malformed request body framing")
        if self.headers.defects:
            raise bad
        encodings = self.headers.get_all("Transfer-Encoding", [])
        lengths = self.headers.get_all("Content-Length", [])
        if encodings:
            if lengths or len(encodings) > 1 or encodings[0].strip().lower() != "chunked":
                raise bad
            self._body_iter = self._chunked_body()
            return None, self._body_iter
        if not lengths:
            self._body_pending = False
            return 0, iter(())
        if len(lengths) > 1 or not lengths[0].strip().isdigit():
            raise bad
        self._body_iter = self._fixed_body(int(lengths[0]))
        return int(lengths[0]), self._body_iter

    def _fixed_body(self, length: int):
        self._body_started = True
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, CHUNK_SIZE))
            if not chunk:
                raise HTTPError(
                    HTTPStatus.BAD_REQUEST, "bad_request", "the request body ended early"
                )
            remaining -= len(chunk)
            yield chunk
        self._body_pending = False

    def _chunked_body(self):
        bad = HTTPError(HTTPStatus.BAD_REQUEST, "bad_request", "malformed chunked request body")
        self._body_started = True
        while True:
            line = self.rfile.readline(1026)
            if not line.endswith(b"\n"):
                raise bad
            size_text = line.split(b";", 1)[0].strip()
            if not _HEX.fullmatch(size_text):
                raise bad
            size = int(size_text, 16)
            if size == 0:
                for _ in range(64):  # trailer fields, ignored
                    trailer = self.rfile.readline(8192)
                    if trailer in (b"\r\n", b"\n"):
                        self._body_pending = False
                        return
                    if not trailer.endswith(b"\n"):
                        raise bad
                raise bad
            while size > 0:
                chunk = self.rfile.read(min(size, CHUNK_SIZE))
                if not chunk:
                    raise bad
                size -= len(chunk)
                yield chunk
            if self.rfile.readline(3) not in (b"\r\n", b"\n"):
                raise bad

    # -- dispatch ---------------------------------------------------------------

    def do_GET(self):  # noqa: N802
        self._dispatch()

    do_HEAD = do_PUT = do_POST = do_DELETE = do_OPTIONS = do_PATCH = do_GET  # noqa: N815

    def _allow_header(self, route: str | None) -> str:
        methods = _ROUTES.get(route, _READ) if route else _READ
        if self.server.read_only or route == PROTECTED_RESOURCE_PATH:
            methods = [method for method in methods if method in _READ] or list(_READ)
        return ", ".join(methods)

    def _dispatch(self) -> None:
        self._response_started = False
        self._continue_sent = False
        self._body_started = False
        self._body_iter = None
        self._body_pending = self._has_request_body()
        origin = self.headers.get("Origin")
        self._cors_origin = origin.strip() if self.server.cors_allowed(origin) else None
        is_upload = False
        try:
            try:
                split = urlsplit(self.path)
                params = parse_qs(
                    split.query,
                    keep_blank_values=True,
                    strict_parsing=False,
                    encoding="utf-8",
                    errors="surrogateescape",
                )
            except ValueError as exc:
                raise HTTPError(HTTPStatus.BAD_REQUEST, "bad_request", "bad request URL") from exc
            route = split.path.rstrip("/") or "/"
            if self.command == "OPTIONS":
                self._handle_options(route)
                return
            if route not in _ROUTES:
                self._authenticate()  # do not reveal anything to anonymous callers
                raise HTTPError(HTTPStatus.NOT_FOUND, "not_found", "unknown endpoint")
            allowed = self._allow_header(route).split(", ")
            if self.command not in allowed:
                raise HTTPError(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    "method_not_allowed",
                    "this server is read-only; use GET or HEAD"
                    if self.server.read_only
                    else f"use {' or '.join(allowed)} for this endpoint",
                    headers={"Allow": ", ".join(allowed)},
                )
            if route == PROTECTED_RESOURCE_PATH:
                self._handle_protected_resource()
                return
            is_upload = self.command == "PUT"
            if self._body_pending and not is_upload:
                # Nothing else takes a body; do not let one be mistaken for a second
                # request on a kept-alive connection.
                self.close_connection = True
            identity = self._authenticate()
            namespace = self.server.namespace_for(identity)
            handler = getattr(self, "_" + self.command.lower() + "_" + route.rsplit("/", 1)[1])
            handler(namespace, params, identity)
        except HTTPError as error:
            if self._response_started:
                self.close_connection = True
            else:
                if self._body_pending:
                    self.close_connection = True
                    if not self._body_started:
                        self._drain_request_body()
                    else:
                        self._discard_rest_of_body()
                self._send_error_json(error)
        except (BrokenPipeError, ConnectionResetError, socket.timeout, ssl.SSLError):
            self.close_connection = True
        except Exception:
            logger.exception("unhandled error for %r", self.path)
            self.close_connection = True
            if not self._response_started:
                self._send_error_json(
                    HTTPError(
                        HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error", "internal server error"
                    )
                )
        finally:
            if self._body_pending:
                self.close_connection = True

    def _authenticate(self) -> Identity:
        identity = self.server.auth.authenticate(self._bearer_token())
        if not self.server.host_allowed(self.headers.get("Host")):
            raise HTTPError(
                HTTPStatus.MISDIRECTED_REQUEST,
                "host_not_allowed",
                "this host name is not allowed without a token; start cfds-server with "
                "--allowed-host <name>, or configure a token",
            )
        return identity

    def _handle_options(self, route: str) -> None:
        if route not in _ROUTES:
            raise HTTPError(HTTPStatus.NOT_FOUND, "not_found", "unknown endpoint")
        allow = self._allow_header(route)
        headers = {"Allow": allow}
        if self._cors_origin and self.headers.get("Access-Control-Request-Method"):
            headers.update(
                {
                    "Access-Control-Allow-Methods": allow,
                    "Access-Control-Allow-Headers": _CORS_ALLOW_HEADERS,
                    "Access-Control-Max-Age": "600",
                }
            )
        self._send_empty(HTTPStatus.NO_CONTENT, headers)

    def _handle_protected_resource(self) -> None:
        auth = self.server.auth
        if not isinstance(auth, OIDCAuth):
            raise HTTPError(HTTPStatus.NOT_FOUND, "not_found", "this server does not use OAuth")
        self._send_json(
            HTTPStatus.OK,
            {
                "resource": self._public_url(),
                "authorization_servers": [auth.issuer],
                "scopes_supported": ["openid", "profile", "email"],
                "bearer_methods_supported": ["header"],
                "resource_name": "cfds storage",
            },
        )

    # -- helpers for endpoints ------------------------------------------------------

    @staticmethod
    def _locate_for_write(namespace: Namespace, path: str):
        """Like ``namespace.locate`` but every refusal is a ``read_only``."""
        try:
            mount, inner, normalized = namespace.locate(path)
        except HTTPError as exc:
            if exc.code == "not_found" and namespace.virtual_root:
                raise read_only(_fsops.normalize_path(path)) from exc
            raise
        if mount is None or not mount.writable:
            raise read_only(normalized)
        return mount, inner, normalized

    @staticmethod
    def _not_mount_root(mount: Mount, inner: str, normalized: str) -> None:
        if inner == "/":
            raise read_only(normalized)

    # -- reading ----------------------------------------------------------------

    def _get_ls(self, namespace: Namespace, params: dict, identity: Identity) -> None:
        path = self._path_param(params, default="/")
        list_entries = params.get("list", ["1"])[-1] not in ("0", "false", "no")
        mount, inner, _ = namespace.locate(path)
        if mount is None:
            payload = namespace.describe_root(list_entries)
        else:
            payload = describe(
                mount.real_root, inner, list_entries, prefix=mount.path, writable=mount.writable
            )
        self._send_json(HTTPStatus.OK, payload)

    _head_ls = _get_ls

    def _get_me(self, namespace: Namespace, params: dict, identity: Identity) -> None:
        user = identity.public()
        auth = self.server.auth
        if isinstance(auth, OIDCAuth) and user["email"] is None and user["name"] is None:
            try:  # access tokens often carry no profile; the userinfo endpoint does
                claims = auth.userinfo(self._bearer_token() or "") or {}
            except HTTPError:
                claims = {}
            user["email"] = claims.get("email") if isinstance(claims.get("email"), str) else None
            name = claims.get("name") or claims.get("preferred_username")
            user["name"] = name if isinstance(name, str) else None
        home = next((m for m in namespace.mounts if m.path == HOME_MOUNT), None)
        if not namespace.virtual_root:
            home = namespace.mounts[0] if namespace.mounts[0].writable else None
        datasets = next((m for m in namespace.mounts if m.path == DATASETS_MOUNT), None)
        quota = home.quota.snapshot() if home is not None and home.quota is not None else None
        default_path = (datasets or home or namespace.mounts[0]).path
        self._send_json(
            HTTPStatus.OK,
            {
                "user": user,
                "quota": quota,
                "home": home.path if home is not None else None,
                "datasets": datasets.path if datasets is not None else None,
                "default_path": default_path,
                "mounts": [mount.public() for mount in namespace.mounts],
            },
        )

    _head_me = _get_me

    def _get_file(self, namespace: Namespace, params: dict, identity: Identity) -> None:
        path = self._path_param(params)
        mount, inner, normalized = namespace.locate(path)
        if mount is None:
            raise HTTPError(
                HTTPStatus.BAD_REQUEST, "is_a_directory", f"is a directory: {normalized}"
            )
        handle, st = open_regular_file(mount.real_root, inner)
        with handle:
            size = st.st_size
            etag = make_etag(st)
            if_match = self.headers.get("If-Match")
            if if_match is not None and not _fsops.etag_matches(if_match, etag):
                raise HTTPError(
                    HTTPStatus.PRECONDITION_FAILED,
                    "precondition_failed",
                    "the file changed since it was opened",
                    headers={"ETag": etag},
                )
            range_header = self.headers.get("Range")
            if_range = self.headers.get("If-Range")
            if if_range is not None and if_range.strip() != etag:
                range_header = None  # RFC 9110 13.1.5: stale validator, send everything
            byte_range = parse_range(range_header, size)
            try:
                last_modified = formatdate(st.st_mtime, usegmt=True)
            except (ValueError, OverflowError, OSError):
                last_modified = None  # a timestamp HTTP cannot express
            if byte_range is None:
                start, length = 0, size
                self.send_response(HTTPStatus.OK)
            else:
                start, end = byte_range
                length = end - start + 1
                self.send_response(HTTPStatus.PARTIAL_CONTENT)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("ETag", etag)
            if last_modified is not None:
                self.send_header("Last-Modified", last_modified)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command == "HEAD" or length == 0:
                return
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(CHUNK_SIZE, remaining))
                # If the file shrank, or was modified since the headers were sent, the
                # promised bytes can no longer be delivered. Stop without sending this
                # chunk and close, so the client sees an incomplete transfer rather than
                # a full-length mix of old and new content.
                if not chunk or make_etag(os.fstat(handle.fileno())) != etag:
                    logger.warning("%r changed while it was being sent; closing", path)
                    self.close_connection = True
                    return
                self.wfile.write(chunk)
                remaining -= len(chunk)

    _head_file = _get_file

    # -- writing ----------------------------------------------------------------

    def _put_file(self, namespace: Namespace, params: dict, identity: Identity) -> None:
        path = self._path_param(params)
        mount, inner, normalized = self._locate_for_write(namespace, path)
        self._not_mount_root(mount, inner, normalized)
        overwrite = self._flag(params, "overwrite", True)
        create = self._flag(params, "create", True)
        length, chunks = self._request_body()
        info, created = write_file(
            mount.real_root,
            inner,
            chunks,
            length=length,
            overwrite=overwrite,
            create=create,
            if_match=self.headers.get("If-Match"),
            if_none_match=self.headers.get("If-None-Match"),
            quota=mount.quota,
            display_prefix=mount.path,
            before_body=self._send_continue,
        )
        status = HTTPStatus.CREATED if created else HTTPStatus.OK
        self._send_json(status, info, {"ETag": info["etag"]})

    def _delete_file(self, namespace: Namespace, params: dict, identity: Identity) -> None:
        path = self._path_param(params)
        mount, inner, normalized = self._locate_for_write(namespace, path)
        self._not_mount_root(mount, inner, normalized)
        freed = delete_path(
            mount.real_root,
            inner,
            recursive=self._flag(params, "recursive", False),
            if_match=self.headers.get("If-Match"),
        )
        if mount.quota is not None:
            mount.quota.adjust(-freed)
        self._send_empty(HTTPStatus.NO_CONTENT)

    def _post_mkdir(self, namespace: Namespace, params: dict, identity: Identity) -> None:
        path = self._path_param(params)
        parents = self._flag(params, "parents", False)
        normalized = _fsops.normalize_path(path)
        if normalized == "/" or normalized in (mount.path for mount in namespace.mounts):
            if not parents:
                raise HTTPError(HTTPStatus.CONFLICT, "exists", f"already exists: {normalized}")
            mount, inner, _ = namespace.locate(normalized)
            if mount is None:
                info = namespace.describe_root(False)["info"]
            else:
                info = describe(
                    mount.real_root, inner, False, prefix=mount.path, writable=mount.writable
                )["info"]
            self._send_json(HTTPStatus.OK, info)
            return
        mount, inner, normalized = self._locate_for_write(namespace, path)
        info, created = make_directory(
            mount.real_root, inner, parents=parents, display_prefix=mount.path
        )
        self._send_json(HTTPStatus.CREATED if created else HTTPStatus.OK, info)

    def _post_mv(self, namespace: Namespace, params: dict, identity: Identity) -> None:
        src, dst = self._param(params, "src"), self._param(params, "dst")
        overwrite = self._flag(params, "overwrite", False)
        src_mount, src_inner, src_norm = namespace.locate(src)
        try:
            dst_mount, dst_inner, dst_norm = namespace.locate(dst)
        except HTTPError as exc:
            if exc.code != "not_found":
                raise
            dst_mount, dst_inner, dst_norm = None, "/", _fsops.normalize_path(dst)
        if src_mount is not None and dst_mount is not None and src_mount is not dst_mount:
            raise HTTPError(
                HTTPStatus.BAD_REQUEST,
                "cross_mount",
                f"cannot move between {src_mount.path} and {dst_mount.path}; copy instead",
            )
        for mount, inner, normalized in (
            (src_mount, src_inner, src_norm),
            (dst_mount, dst_inner, dst_norm),
        ):
            if mount is None or not mount.writable:
                raise read_only(normalized)
            self._not_mount_root(mount, inner, normalized)
        info, freed = move_path(
            src_mount.real_root,
            src_inner,
            dst_inner,
            overwrite=overwrite,
            display_prefix=src_mount.path,
        )
        if src_mount.quota is not None:
            src_mount.quota.adjust(-freed)
        self._send_json(HTTPStatus.OK, info)

    def _post_cp(self, namespace: Namespace, params: dict, identity: Identity) -> None:
        src, dst = self._param(params, "src"), self._param(params, "dst")
        overwrite = self._flag(params, "overwrite", False)
        src_mount, src_inner, src_norm = namespace.locate(src)
        if src_mount is None:
            raise HTTPError(HTTPStatus.BAD_REQUEST, "is_a_directory", f"is a directory: {src_norm}")
        dst_mount, dst_inner, dst_norm = self._locate_for_write(namespace, dst)
        self._not_mount_root(dst_mount, dst_inner, dst_norm)
        handle, st = open_regular_file(src_mount.real_root, src_inner)
        etag = make_etag(st)

        def chunks():
            while True:
                chunk = handle.read(CHUNK_SIZE)
                if make_etag(os.fstat(handle.fileno())) != etag:
                    raise HTTPError(
                        HTTPStatus.CONFLICT,
                        "source_changed",
                        f"{src_norm} changed while it was being copied",
                    )
                if not chunk:
                    return
                yield chunk

        with handle:
            info, created = write_file(
                dst_mount.real_root,
                dst_inner,
                chunks(),
                length=st.st_size,
                overwrite=overwrite,
                quota=dst_mount.quota,
                display_prefix=dst_mount.path,
            )
        self._send_json(
            HTTPStatus.CREATED if created else HTTPStatus.OK, info, {"ETag": info["etag"]}
        )


def make_server(
    root: str | os.PathLike | None = DEFAULT_ROOT,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    token: str | None = None,
    certfile: str | None = None,
    keyfile: str | None = None,
    max_connections: int = DEFAULT_MAX_CONNECTIONS,
    allowed_hosts: tuple[str, ...] | list[str] = (),
    *,
    writable: bool = False,
    auth=None,
    users_root: str | os.PathLike | None = None,
    datasets_root: str | os.PathLike | None = None,
    default_quota: int = DEFAULT_QUOTA,
    datasets_writer_role: str | None = None,
    public_url: str | None = None,
    cors_origins: tuple[str, ...] | list[str] | None = None,
) -> CFDSServer:
    """Create (but do not start) a server. Call ``serve_forever()`` on the result.

    With ``users_root`` the server is multi-user: ``root`` is ignored, every caller
    gets ``/home`` below ``users_root`` and, if given, ``datasets_root`` read-only at
    ``/kaggle-datasets``.
    """
    ssl_context = None
    if certfile:
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
        ssl_context.load_cert_chain(certfile, keyfile)
    elif keyfile:
        raise ValueError("keyfile was given without certfile")
    users = UserStore(users_root, default_quota) if users_root is not None else None
    try:
        return CFDSServer(
            (host, port),
            None if users is not None else root,
            token=token,
            ssl_context=ssl_context,
            max_connections=max_connections,
            allowed_hosts=allowed_hosts,
            writable=writable,
            auth=auth,
            users=users,
            datasets_root=datasets_root,
            datasets_writer_role=datasets_writer_role,
            public_url=public_url,
            cors_origins=cors_origins,
        )
    except BaseException:
        if users is not None:
            users.close()
        raise


def _is_loopback(host: str) -> bool:
    return host in ("localhost", "::1") or host.startswith("127.")


def _read_token(args: argparse.Namespace) -> str | None:
    if args.token_file:
        with open(args.token_file, encoding="utf-8") as handle:
            token = handle.read().strip()
        if not token:
            raise ValueError(f"token file is empty: {args.token_file}")
        return token
    return os.environ.get("CFDS_TOKEN") or None


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        port = -1
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"not a port number (0-65535): {value!r}")
    return port


def _positive(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        number = 0
    if number < 1:
        raise argparse.ArgumentTypeError(f"not a positive integer: {value!r}")
    return number


_SIZE_UNITS = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}


def _size(value: str) -> int:
    """``107374182400``, ``100G`` or ``100GiB`` -> bytes (binary units)."""
    match = re.fullmatch(r"\s*(\d+)\s*([KMGT]?)(?:i?B)?\s*", value, re.IGNORECASE)
    if match is None:
        raise argparse.ArgumentTypeError(f"not a size like 100G: {value!r}")
    return int(match.group(1)) * _SIZE_UNITS[match.group(2).upper()]


def _ensure_descriptors(max_connections: int) -> None:
    """Raise the open-file limit if the connection cap would not fit under it."""
    try:
        import resource
    except ImportError:  # not available on Windows
        return
    needed = max_connections * 2 + 64
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft == resource.RLIM_INFINITY or soft >= needed:
        return
    target = needed if hard == resource.RLIM_INFINITY else min(needed, hard)
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    except (ValueError, OSError):
        target = soft
    if target < needed:
        logger.warning(
            "the open-file limit (%d) is low for --max-connections %d; "
            "raise it with 'ulimit -n' or lower --max-connections",
            target,
            max_connections,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cfds-server",
        description="Serve a directory (or per-user spaces) for the cfds:// fsspec filesystem.",
    )
    single = parser.add_argument_group("single tree")
    single.add_argument(
        "--root",
        default=os.environ.get("CFDS_ROOT", DEFAULT_ROOT),
        help="directory to serve at / (default: $CFDS_ROOT or %(default)s)",
    )
    single.add_argument(
        "--writable", action="store_true", help="allow uploads, deletes and renames under --root"
    )
    multi = parser.add_argument_group("multi-user")
    multi.add_argument(
        "--users-root",
        default=os.environ.get("CFDS_USERS_ROOT"),
        help="directory holding one private folder per user; enables multi-user mode "
        "(each user sees /home and /kaggle-datasets; --root is ignored)",
    )
    multi.add_argument(
        "--datasets-root",
        default=os.environ.get("CFDS_DATASETS_ROOT", DEFAULT_DATASETS_ROOT),
        help="shared read-only datasets shown at /kaggle-datasets (default: %(default)s)",
    )
    multi.add_argument(
        "--default-quota",
        type=_size,
        default=DEFAULT_QUOTA,
        help="storage per user, e.g. 100G (default: 100G)",
    )
    multi.add_argument(
        "--datasets-writer-role",
        metavar="ROLE",
        help="users with this role (from the token's 'roles' claim) may write to /kaggle-datasets",
    )
    auth = parser.add_argument_group("authentication")
    auth.add_argument(
        "--oidc-issuer",
        default=os.environ.get("CFDS_OIDC_ISSUER"),
        metavar="URL",
        help="require access tokens from this OpenID Connect provider "
        "(e.g. https://oidc.cloudfronts.group)",
    )
    auth.add_argument(
        "--audience",
        action="append",
        metavar="CLIENT_ID",
        help="accepted token audience (repeatable; default: " + ", ".join(DEFAULT_AUDIENCES) + ")",
    )
    auth.add_argument(
        "--token-file",
        help="file holding a shared bearer token clients must send (or set $CFDS_TOKEN)",
    )
    auth.add_argument(
        "--public-url",
        default=os.environ.get("CFDS_PUBLIC_URL"),
        metavar="URL",
        help="the address clients use, e.g. https://storage.cloudfronts.group "
        "(used in OAuth metadata; default: taken from each request)",
    )
    net = parser.add_argument_group("network")
    net.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help="address to bind; use 0.0.0.0 to accept remote clients (default: %(default)s)",
    )
    net.add_argument("--port", type=_port, default=DEFAULT_PORT, help="default: %(default)s")
    net.add_argument("--certfile", help="TLS certificate (PEM); enables HTTPS")
    net.add_argument("--keyfile", help="TLS private key (PEM), if not in --certfile")
    net.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        metavar="NAME",
        help="extra DNS name clients may use for this server when no token is set "
        "(repeatable; IP addresses and single-label names are always allowed)",
    )
    net.add_argument(
        "--cors-origin",
        action="append",
        metavar="ORIGIN",
        help="browser origin allowed to call the API, e.g. https://vscode.dev or "
        "https://*.vscode-cdn.net (repeatable; default with sign-in: "
        + ", ".join(DEFAULT_CORS_ORIGINS)
        + "; default without: none)",
    )
    net.add_argument(
        "--max-connections",
        type=_positive,
        default=DEFAULT_MAX_CONNECTIONS,
        help="connections served at once; more are refused (default: %(default)s)",
    )
    parser.add_argument(
        "--log-level",
        type=str.upper,
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        default="INFO",
        help="default: %(default)s",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        token = _read_token(args)
        auth = None
        if args.oidc_issuer:
            if token:
                raise ValueError("use either --oidc-issuer or a shared token, not both")
            audiences = tuple(args.audience) if args.audience else DEFAULT_AUDIENCES
            auth = OIDCAuth(args.oidc_issuer, audiences)
        datasets_root = None
        if args.users_root and args.datasets_root:
            if os.path.isdir(args.datasets_root):
                datasets_root = args.datasets_root
            else:
                logger.warning("datasets root %s does not exist; not shown", args.datasets_root)
        server = make_server(
            args.root,
            args.host,
            args.port,
            token,
            args.certfile,
            args.keyfile,
            max_connections=args.max_connections,
            allowed_hosts=args.allowed_host,
            writable=args.writable,
            auth=auth,
            users_root=args.users_root or None,
            datasets_root=datasets_root,
            default_quota=args.default_quota,
            datasets_writer_role=args.datasets_writer_role,
            public_url=args.public_url,
            cors_origins=args.cors_origin,
        )
    except (OSError, ValueError, OverflowError, RuntimeError, ssl.SSLError) as exc:
        print(f"cfds-server: {exc}", file=sys.stderr)
        return 2
    _ensure_descriptors(server.max_connections)
    if not server.auth.required and not _is_loopback(args.host):
        logger.warning(
            "no authentication: anyone who can reach %s:%s can %s %s",
            args.host,
            server.port,
            "read" if server.read_only else "read and change",
            server.real_root,
        )
    if server.auth.required and not server.uses_tls:
        logger.warning(
            "tokens are accepted without TLS here: put a TLS proxy in front, or use --certfile"
        )
    if server.multi_user:
        logger.info(
            "multi-user mode: homes under %s (%d bytes each), datasets %s, sign-in: %s",
            server.real_root,
            server.users.default_quota,
            server.datasets_root or "(none)",
            server.auth.description,
        )
    logger.info(
        "serving %s %s on %s://%s:%s",
        server.real_root,
        "read-only" if server.read_only else "read-write",
        "https" if server.uses_tls else "http",
        args.host,
        server.port,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
