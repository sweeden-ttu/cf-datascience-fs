"""Read-only HTTP server that exposes one directory tree to :mod:`cf_datascience_fs.core`.

This module imports only the standard library. There are two endpoints, both ``GET``
(and ``HEAD``) only:

``/v1/ls?path=<path>[&list=0]``
    JSON description of ``path``: ``{"info": {...}, "entries": [...] | null}``.
    ``entries`` is the directory listing, or ``null`` for a file or when ``list=0``.

``/v1/file?path=<path>``
    The file's bytes. Honours a single ``Range: bytes=...`` header (``206``), an
    ``If-Match: <etag>`` precondition (``412`` when the file changed) and ``If-Range``.
    If the file changes while it is being sent, the connection is cut short so the
    client sees an incomplete transfer instead of a mix of old and new bytes.

Paths are POSIX style and relative to the served root. Every path is resolved with
symlinks followed and must stay inside the root; anything else is refused.
"""

from __future__ import annotations

import argparse
import errno
import hmac
import ipaddress
import json
import logging
import os
import re
import socket
import ssl
import stat as stat_module
import string
import sys
import threading
from email.utils import formatdate
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from ._version import __version__

logger = logging.getLogger("cf_datascience_fs.server")

API_PREFIX = "/v1"
DEFAULT_ROOT = "/mnt/TMP"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
CHUNK_SIZE = 1024 * 1024
#: Seconds a connection may sit idle (or stall mid-transfer) before it is dropped.
SOCKET_TIMEOUT = 120
#: Connections served at once. Each one costs a thread and up to two descriptors.
DEFAULT_MAX_CONNECTIONS = 256
#: How much of an unwanted request body is read and thrown away before closing.
MAX_DRAIN = 1024 * 1024
_TOKEN_CHARS = frozenset(string.ascii_letters + string.digits + "-._~+/=")

_RANGE_RE = re.compile(r"\s*bytes\s*=\s*(\d{0,18})\s*-\s*(\d{0,18})\s*", re.ASCII)
_NOT_FOUND_ERRNOS = {errno.ENOENT, errno.ENOTDIR, errno.ELOOP, errno.ENAMETOOLONG}


class HTTPError(Exception):
    """An error that maps directly onto an HTTP error response."""

    def __init__(self, status: int, code: str, message: str, headers: dict | None = None):
        super().__init__(message)
        self.status = int(status)
        self.code = code
        self.message = message
        self.headers = headers or {}


def _os_error_to_http(exc: OSError, path: str) -> HTTPError:
    if isinstance(exc, PermissionError) or exc.errno in (errno.EACCES, errno.EPERM):
        return HTTPError(HTTPStatus.FORBIDDEN, "forbidden", f"permission denied: {path}")
    if exc.errno in _NOT_FOUND_ERRNOS:
        return HTTPError(HTTPStatus.NOT_FOUND, "not_found", f"no such file or directory: {path}")
    logger.warning("unexpected OS error for %r: %s", path, exc)
    return HTTPError(HTTPStatus.INTERNAL_SERVER_ERROR, "io_error", f"I/O error reading {path}")


def is_within(real_path: str, real_root: str) -> bool:
    """True if ``real_path`` is ``real_root`` or below it. Both must be resolved paths."""
    if real_path == real_root:
        return True
    prefix = real_root if real_root.endswith(os.sep) else real_root + os.sep
    return real_path.startswith(prefix)


def resolve_path(real_root: str, client_path: str) -> tuple[str, str]:
    """Map a client path onto the local file system.

    Returns ``(normalized_client_path, real_local_path)``. The normalized path always
    starts with ``/`` and has no empty, ``.`` or ``..`` segments. Raises
    :class:`HTTPError` for malformed paths and for paths whose resolved location
    (symlinks followed) falls outside ``real_root``.
    """
    if "\x00" in client_path:
        raise HTTPError(HTTPStatus.BAD_REQUEST, "bad_request", "path contains a NUL byte")
    parts = [part for part in client_path.split("/") if part not in ("", ".")]
    if ".." in parts:
        raise HTTPError(HTTPStatus.BAD_REQUEST, "bad_request", "'..' is not allowed in paths")
    if os.sep != "/" and any(os.sep in part or ":" in part for part in parts):
        raise HTTPError(HTTPStatus.BAD_REQUEST, "bad_request", "invalid character in path")
    normalized = "/" + "/".join(parts)
    try:
        real = os.path.realpath(os.path.join(real_root, *parts))
    except (OSError, ValueError) as exc:
        raise HTTPError(HTTPStatus.NOT_FOUND, "not_found", f"cannot resolve: {normalized}") from exc
    if not is_within(real, real_root):
        raise HTTPError(
            HTTPStatus.FORBIDDEN, "forbidden", f"path is outside the root: {normalized}"
        )
    return normalized, real


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Parse a ``Range`` header against a file of ``size`` bytes.

    Returns ``(start, end)`` with ``end`` inclusive, or ``None`` when the whole file
    should be sent (no header, or a header we do not understand, which RFC 9110 lets a
    server ignore). Raises :class:`HTTPError` 416 when the range cannot be satisfied.
    """
    if not header:
        return None
    match = _RANGE_RE.fullmatch(header)
    if match is None:
        return None
    first, last = match.groups()
    if not first and not last:
        return None
    unsatisfiable = HTTPError(
        HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE,
        "range_not_satisfiable",
        f"range {header.strip()!r} cannot be satisfied for a file of {size} bytes",
        headers={"Content-Range": f"bytes */{size}"},
    )
    if not first:  # suffix range: the last N bytes
        length = int(last)
        if length == 0 or size == 0:
            raise unsatisfiable
        return max(0, size - length), size - 1
    start = int(first)
    if last and int(last) < start:
        return None
    if start >= size:
        raise unsatisfiable
    end = min(int(last), size - 1) if last else size - 1
    return start, end


def make_etag(st: os.stat_result) -> str:
    """Validator built from mtime, size and inode.

    It changes when a file is rewritten or replaced, as far as the file system's
    timestamp resolution can tell: a same-size rewrite within one timestamp tick of
    the previous write is not distinguishable.
    """
    return f'"{st.st_mtime_ns:x}-{st.st_size:x}-{st.st_ino:x}"'


def validate_token(token: str) -> str:
    """Bearer tokens must be plain ASCII (RFC 6750) so every HTTP client sends the same bytes."""
    if not token or not set(token) <= _TOKEN_CHARS:
        raise ValueError(
            "the token must be non-empty and use only letters, digits and -._~+/= characters"
        )
    return token


def _etag_matches(header: str, etag: str) -> bool:
    candidates = [item.strip() for item in header.split(",")]
    return "*" in candidates or etag in candidates


def _entry(name: str, st: os.stat_result, islink: bool) -> dict:
    if stat_module.S_ISDIR(st.st_mode):
        kind, size = "directory", 0
    elif stat_module.S_ISREG(st.st_mode):
        kind, size = "file", st.st_size
    else:
        kind, size = "other", 0
    entry = {"name": name, "size": size, "type": kind, "mtime": st.st_mtime, "islink": islink}
    if kind == "file":
        entry["etag"] = make_etag(st)
    return entry


_DIR_FD_OPEN = os.open in getattr(os, "supports_dir_fd", ())


def _open_confined(real_root: str, real: str, normalized: str, flags: int) -> int:
    """Open the already-resolved path ``real`` without following any symlink.

    ``resolve_path`` checks a path *string*, and ``real`` contained no symlinks when it
    was computed. Someone with write access to the tree could still swap a directory for
    a symlink before the open. So the path is walked from the root one component at a
    time, relative to the previous directory's descriptor, with ``O_NOFOLLOW``: a
    component that has become a symlink makes the open fail instead of leading outside.
    """
    nofollow = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    if not _DIR_FD_OPEN:  # e.g. Windows: no descriptor-relative open available
        return os.open(real, flags | nofollow)
    parts = [] if real == real_root else os.path.relpath(real, real_root).split(os.sep)
    directory = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow
    fd = os.open(real_root, directory if parts else flags | nofollow)
    try:
        for index, part in enumerate(parts):
            last = index == len(parts) - 1
            child = os.open(part, flags | nofollow if last else directory, dir_fd=fd)
            os.close(fd)
            fd = child
        if stat_module.S_ISLNK(os.fstat(fd).st_mode):
            # Only reachable with O_PATH, which opens a symlink itself rather than failing.
            raise HTTPError(
                HTTPStatus.FORBIDDEN, "forbidden", f"path is outside the root: {normalized}"
            )
    except BaseException:
        os.close(fd)
        raise
    return fd


_HAS_O_PATH = hasattr(os, "O_PATH")
_SCANDIR_FD = os.scandir in getattr(os, "supports_fd", ())


def _stat_confined(real_root: str, real: str, normalized: str) -> os.stat_result:
    """``os.stat`` that cannot be tricked into describing something outside the root."""
    if not _HAS_O_PATH:
        return os.stat(real)  # no descriptor-based check available on this platform
    fd = _open_confined(real_root, real, normalized, os.O_PATH)
    try:
        return os.fstat(fd)
    finally:
        os.close(fd)


def describe(real_root: str, client_path: str, list_entries: bool = True) -> dict:
    """Build the ``/v1/ls`` response body for ``client_path``."""
    normalized, real = resolve_path(real_root, client_path)
    try:
        st = _stat_confined(real_root, real, normalized)
        islink = normalized != "/" and os.path.islink(
            os.path.join(real_root, *normalized.strip("/").split("/"))
        )
        info = _entry(normalized, st, islink)
        entries = None
        if list_entries and info["type"] == "directory":
            entries = _list_directory(real_root, normalized, real)
    except OSError as exc:
        raise _os_error_to_http(exc, normalized) from exc
    return {"info": info, "entries": entries}


def _list_directory(real_root: str, normalized: str, real: str) -> list[dict]:
    prefix = normalized.rstrip("/") + "/"
    entries = []
    dir_fd = None
    if _SCANDIR_FD:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        dir_fd = _open_confined(real_root, real, normalized, flags)
    try:
        with os.scandir(dir_fd if dir_fd is not None else real) as iterator:
            for item in iterator:
                try:
                    islink = item.is_symlink()
                    if islink:
                        # Links that are broken or point outside the root are not served,
                        # so they are left out of listings as well.
                        target = os.path.realpath(os.path.join(real, item.name))
                        if not is_within(target, real_root):
                            continue
                        st = _stat_confined(real_root, target, prefix + item.name)
                    else:
                        st = item.stat(follow_symlinks=False)
                except (OSError, ValueError, HTTPError):
                    continue
                entries.append(_entry(prefix + item.name, st, islink))
    finally:
        if dir_fd is not None:
            os.close(dir_fd)
    entries.sort(key=lambda entry: entry["name"])
    return entries


def open_regular_file(real_root: str, client_path: str):
    """Open ``client_path`` for reading. Returns ``(binary_file, stat_result)``.

    Only regular files inside the root are opened. Devices, FIFOs and sockets are
    refused, and a path swapped for a symlink between the check and the open is caught.
    """
    normalized, real = resolve_path(real_root, client_path)
    try:
        st = os.stat(real)
    except OSError as exc:
        raise _os_error_to_http(exc, normalized) from exc
    if stat_module.S_ISDIR(st.st_mode):
        raise HTTPError(HTTPStatus.BAD_REQUEST, "is_a_directory", f"is a directory: {normalized}")
    if not stat_module.S_ISREG(st.st_mode):
        raise HTTPError(HTTPStatus.FORBIDDEN, "forbidden", f"not a regular file: {normalized}")

    flags = os.O_RDONLY
    for name in ("O_NONBLOCK", "O_BINARY", "O_NOCTTY"):
        flags |= getattr(os, name, 0)
    try:
        fd = _open_confined(real_root, real, normalized, flags)
    except OSError as exc:
        raise _os_error_to_http(exc, normalized) from exc
    try:
        st = os.fstat(fd)
        if not stat_module.S_ISREG(st.st_mode):
            raise HTTPError(HTTPStatus.FORBIDDEN, "forbidden", f"not a regular file: {normalized}")
        return os.fdopen(fd, "rb", buffering=0), st
    except BaseException:
        os.close(fd)
        raise


class CFDSServer(ThreadingHTTPServer):
    """Threaded HTTP server bound to one root directory."""

    daemon_threads = True
    request_queue_size = 128

    def __init__(
        self,
        address: tuple[str, int],
        root: str | os.PathLike,
        token: str | None = None,
        ssl_context: ssl.SSLContext | None = None,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
        allowed_hosts: tuple[str, ...] | list[str] = (),
    ):
        root = os.fspath(root)
        if not os.path.isdir(root):
            raise NotADirectoryError(f"root is not a directory: {root}")
        self.real_root = os.path.realpath(root)
        self.token = validate_token(token) if token else None
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
        no token is configured, since a browser cannot supply the token.
        """
        if self.token is not None:
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

    # -- plumbing ---------------------------------------------------------------

    def log_message(self, format, *args):  # noqa: A002 - signature set by the base class
        message = (format % args).encode("unicode_escape").decode("ascii")
        logger.info("%s %s", self.address_string(), message)

    def send_response(self, code, message=None):
        self._response_started = True
        super().send_response(code, message)

    def end_headers(self):
        if self.close_connection:
            # Tell the client, so it does not try to reuse a connection we are closing.
            self.send_header("Connection", "close")
        super().end_headers()

    def handle_expect_100(self):
        # Never invite a request body: nothing this server accepts has one.
        return True

    def send_error(self, code, message=None, explain=None):
        """Protocol-level errors raised by the base class, in the same JSON shape as ours."""
        self.close_connection = True
        if code == HTTPStatus.NOT_IMPLEMENTED:
            code, error_code = HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed"
            message = "this server is read-only; use GET or HEAD"
            headers = {"Allow": "GET, HEAD"}
        else:
            error_code, headers = "bad_request", {}
        if not getattr(self, "command", None):
            self.command = "GET"  # the request line was not parsed; a body is still fine
        if getattr(self, "request_version", "HTTP/0.9") == "HTTP/0.9":
            # Older Pythons leave an unparsable request marked HTTP/0.9, which makes
            # send_response() skip the status line and headers altogether.
            self.request_version = "HTTP/1.0"
        self._send_error_json(HTTPError(code, error_code, message or "bad request", headers))

    def _send_json(self, status: int, payload: dict, headers: dict | None = None) -> None:
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

    def _send_error_json(self, error: HTTPError) -> None:
        payload = {"error": {"code": error.code, "message": error.message}}
        self._send_json(error.status, payload, error.headers)

    def _authorized(self) -> bool:
        expected = self.server.token
        if expected is None:
            return True
        scheme, _, supplied = self.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "bearer":
            return False
        # Header values arrive decoded as Latin-1; tokens are restricted to ASCII.
        return hmac.compare_digest(
            supplied.strip().encode("latin-1", "replace"), expected.encode("ascii")
        )

    def _path_param(self, params: dict, default: str | None = None) -> str:
        values = params.get("path")
        if not values:
            if default is not None:
                return default
            raise HTTPError(HTTPStatus.BAD_REQUEST, "bad_request", "missing 'path' parameter")
        if len(values) > 1:
            raise HTTPError(HTTPStatus.BAD_REQUEST, "bad_request", "'path' given more than once")
        return values[0]

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
        if "100-continue" in self.headers.get("Expect", "").lower():
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

    # -- dispatch ---------------------------------------------------------------

    def do_GET(self):  # noqa: N802
        self._dispatch()

    def do_HEAD(self):  # noqa: N802
        self._dispatch()

    def _reject_method(self):
        self._response_started = False
        self.close_connection = True
        self._drain_request_body()
        try:
            self._send_error_json(
                HTTPError(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    "method_not_allowed",
                    "this server is read-only; use GET or HEAD",
                    headers={"Allow": "GET, HEAD"},
                )
            )
        except OSError:
            pass

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _reject_method  # noqa: N815

    def _dispatch(self) -> None:
        self._response_started = False
        if self._has_request_body():
            # We never read request bodies; do not let one be mistaken for a second
            # request on a kept-alive connection.
            self.close_connection = True
        try:
            if not self._authorized():
                raise HTTPError(
                    HTTPStatus.UNAUTHORIZED,
                    "unauthorized",
                    "a valid bearer token is required",
                    headers={"WWW-Authenticate": 'Bearer realm="cfds"'},
                )
            if not self.server.host_allowed(self.headers.get("Host")):
                raise HTTPError(
                    HTTPStatus.MISDIRECTED_REQUEST,
                    "host_not_allowed",
                    "this host name is not allowed without a token; start cfds-server with "
                    "--allowed-host <name>, or configure a token",
                )
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
            route = split.path.rstrip("/")
            if route == f"{API_PREFIX}/ls":
                self._handle_ls(params)
            elif route == f"{API_PREFIX}/file":
                self._handle_file(params)
            else:
                raise HTTPError(HTTPStatus.NOT_FOUND, "not_found", "unknown endpoint")
        except HTTPError as error:
            if self._response_started:
                self.close_connection = True
            else:
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

    # -- endpoints --------------------------------------------------------------

    def _handle_ls(self, params: dict) -> None:
        path = self._path_param(params, default="/")
        list_entries = params.get("list", ["1"])[-1] not in ("0", "false", "no")
        self._send_json(HTTPStatus.OK, describe(self.server.real_root, path, list_entries))

    def _handle_file(self, params: dict) -> None:
        path = self._path_param(params)
        handle, st = open_regular_file(self.server.real_root, path)
        with handle:
            size = st.st_size
            etag = make_etag(st)
            if_match = self.headers.get("If-Match")
            if if_match is not None and not _etag_matches(if_match, etag):
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


def make_server(
    root: str | os.PathLike = DEFAULT_ROOT,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    token: str | None = None,
    certfile: str | None = None,
    keyfile: str | None = None,
    max_connections: int = DEFAULT_MAX_CONNECTIONS,
    allowed_hosts: tuple[str, ...] | list[str] = (),
) -> CFDSServer:
    """Create (but do not start) a server. Call ``serve_forever()`` on the result."""
    ssl_context = None
    if certfile:
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
        ssl_context.load_cert_chain(certfile, keyfile)
    elif keyfile:
        raise ValueError("keyfile was given without certfile")
    return CFDSServer(
        (host, port),
        root,
        token=token,
        ssl_context=ssl_context,
        max_connections=max_connections,
        allowed_hosts=allowed_hosts,
    )


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
        description="Serve a directory read-only for the cfds:// fsspec filesystem.",
    )
    parser.add_argument(
        "--root",
        default=os.environ.get("CFDS_ROOT", DEFAULT_ROOT),
        help="directory to serve (default: $CFDS_ROOT or %(default)s)",
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help="address to bind; use 0.0.0.0 to accept remote clients (default: %(default)s)",
    )
    parser.add_argument("--port", type=_port, default=DEFAULT_PORT, help="default: %(default)s")
    parser.add_argument(
        "--token-file",
        help="file holding the bearer token clients must send (or set $CFDS_TOKEN)",
    )
    parser.add_argument("--certfile", help="TLS certificate (PEM); enables HTTPS")
    parser.add_argument("--keyfile", help="TLS private key (PEM), if not in --certfile")
    parser.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        metavar="NAME",
        help="extra DNS name clients may use for this server when no token is set "
        "(repeatable; IP addresses and single-label names are always allowed)",
    )
    parser.add_argument(
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
        server = make_server(
            args.root,
            args.host,
            args.port,
            token,
            args.certfile,
            args.keyfile,
            max_connections=args.max_connections,
            allowed_hosts=args.allowed_host,
        )
    except (OSError, ValueError, OverflowError, ssl.SSLError) as exc:
        print(f"cfds-server: {exc}", file=sys.stderr)
        return 2
    _ensure_descriptors(server.max_connections)
    if token is None and not _is_loopback(args.host):
        logger.warning(
            "no token configured: anyone who can reach %s:%s can read %s",
            args.host,
            server.port,
            server.real_root,
        )
    if token is not None and not server.uses_tls:
        logger.warning("token auth without TLS: the token and data travel unencrypted")
    logger.info(
        "serving %s read-only on %s://%s:%s",
        server.real_root,
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
