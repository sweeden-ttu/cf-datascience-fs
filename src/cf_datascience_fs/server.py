"""Read-only HTTP server that exposes one directory tree to :mod:`cf_datascience_fs.core`.

It uses only the standard library, so the machine that holds the drive needs nothing
beyond Python. There are two endpoints, both ``GET`` (and ``HEAD``) only:

``/v1/ls?path=<path>[&list=0]``
    JSON description of ``path``: ``{"info": {...}, "entries": [...] | null}``.
    ``entries`` is the directory listing, or ``null`` for a file or when ``list=0``.

``/v1/file?path=<path>``
    The file's bytes. Honours a single ``Range: bytes=...`` header (``206``) and an
    ``If-Match: <etag>`` precondition (``412`` when the file changed).

Paths are POSIX style and relative to the served root. Every path is resolved with
symlinks followed and must stay inside the root; anything else is refused.
"""

from __future__ import annotations

import argparse
import errno
import hmac
import json
import logging
import os
import re
import socket
import ssl
import stat as stat_module
import sys
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
    return f'"{st.st_mtime_ns:x}-{st.st_size:x}"'


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


def describe(real_root: str, client_path: str, list_entries: bool = True) -> dict:
    """Build the ``/v1/ls`` response body for ``client_path``."""
    normalized, real = resolve_path(real_root, client_path)
    try:
        st = os.stat(real)
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
    with os.scandir(real) as iterator:
        for item in iterator:
            try:
                islink = item.is_symlink()
                if islink:
                    # Links that are broken or point outside the root are not served,
                    # so they are left out of listings as well.
                    target = os.path.realpath(item.path)
                    if not is_within(target, real_root):
                        continue
                    st = os.stat(target)
                else:
                    st = item.stat(follow_symlinks=False)
            except (OSError, ValueError):
                continue
            entries.append(_entry(prefix + item.name, st, islink))
    entries.sort(key=lambda entry: entry["name"])
    return entries


def _fd_real_path(fd: int) -> str | None:
    """Best-effort real path of an open descriptor (Linux only)."""
    link = f"/proc/self/fd/{fd}"
    try:
        return os.readlink(link)
    except OSError:
        return None


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
    for name in ("O_NOFOLLOW", "O_NONBLOCK", "O_CLOEXEC", "O_BINARY", "O_NOCTTY"):
        flags |= getattr(os, name, 0)
    try:
        fd = os.open(real, flags)
    except OSError as exc:
        raise _os_error_to_http(exc, normalized) from exc
    try:
        st = os.fstat(fd)
        if not stat_module.S_ISREG(st.st_mode):
            raise HTTPError(HTTPStatus.FORBIDDEN, "forbidden", f"not a regular file: {normalized}")
        opened = _fd_real_path(fd)
        if opened is not None and not is_within(opened, real_root):
            raise HTTPError(
                HTTPStatus.FORBIDDEN, "forbidden", f"path is outside the root: {normalized}"
            )
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
    ):
        root = os.fspath(root)
        if not os.path.isdir(root):
            raise NotADirectoryError(f"root is not a directory: {root}")
        self.real_root = os.path.realpath(root)
        self.token = token or None
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

    @property
    def port(self) -> int:
        return self.server_address[1]

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError, socket.timeout, ssl.SSLError)):
            logger.debug("connection from %s dropped: %s", client_address[0], exc)
        else:
            logger.exception("error handling a request from %s", client_address[0])


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

    # -- plumbing ---------------------------------------------------------------

    def log_message(self, format, *args):  # noqa: A002 - signature set by the base class
        message = (format % args).encode("unicode_escape").decode("ascii")
        logger.info("%s %s", self.address_string(), message)

    def send_response(self, code, message=None):
        self._response_started = True
        super().send_response(code, message)

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
        return hmac.compare_digest(
            supplied.strip().encode("utf-8", "surrogateescape"), expected.encode("utf-8")
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

    # -- dispatch ---------------------------------------------------------------

    def do_GET(self):  # noqa: N802
        self._dispatch()

    def do_HEAD(self):  # noqa: N802
        self._dispatch()

    def _reject_method(self):
        # A request body may follow that we will not read, so drop the connection.
        self.close_connection = True
        self._response_started = False
        self._send_error_json(
            HTTPError(
                HTTPStatus.METHOD_NOT_ALLOWED,
                "method_not_allowed",
                "this server is read-only; use GET or HEAD",
                headers={"Allow": "GET, HEAD"},
            )
        )

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _reject_method  # noqa: N815

    def _dispatch(self) -> None:
        self._response_started = False
        if self.headers.get("Content-Length", "0").strip() not in ("", "0") or self.headers.get(
            "Transfer-Encoding"
        ):
            # We never read request bodies; do not let one poison a kept-alive connection.
            self.close_connection = True
        try:
            if not self._authorized():
                raise HTTPError(
                    HTTPStatus.UNAUTHORIZED,
                    "unauthorized",
                    "a valid bearer token is required",
                    headers={"WWW-Authenticate": 'Bearer realm="cfds"'},
                )
            split = urlsplit(self.path)
            route = split.path.rstrip("/")
            try:
                params = parse_qs(
                    split.query,
                    keep_blank_values=True,
                    strict_parsing=False,
                    encoding="utf-8",
                    errors="surrogateescape",
                )
            except ValueError as exc:
                raise HTTPError(HTTPStatus.BAD_REQUEST, "bad_request", "bad query string") from exc
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
            byte_range = parse_range(self.headers.get("Range"), size)
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
            self.send_header("Last-Modified", formatdate(st.st_mtime, usegmt=True))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command == "HEAD" or length == 0:
                return
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    # The file shrank underneath us. The declared length can no longer be
                    # met, so close the connection and let the client see a short read.
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
) -> CFDSServer:
    """Create (but do not start) a server. Call ``serve_forever()`` on the result."""
    ssl_context = None
    if certfile:
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.load_cert_chain(certfile, keyfile)
    elif keyfile:
        raise ValueError("keyfile was given without certfile")
    return CFDSServer((host, port), root, token=token, ssl_context=ssl_context)


def _is_loopback(host: str) -> bool:
    return host in ("localhost", "::1") or host.startswith("127.")


def _read_token(args: argparse.Namespace) -> str | None:
    if args.token_file:
        with open(args.token_file, encoding="utf-8") as handle:
            token = handle.read().strip()
        if not token:
            raise SystemExit(f"token file is empty: {args.token_file}")
        return token
    return os.environ.get("CFDS_TOKEN") or None


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
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="default: %(default)s")
    parser.add_argument(
        "--token-file",
        help="file holding the bearer token clients must send (or set $CFDS_TOKEN)",
    )
    parser.add_argument("--certfile", help="TLS certificate (PEM); enables HTTPS")
    parser.add_argument("--keyfile", help="TLS private key (PEM), if not in --certfile")
    parser.add_argument("--log-level", default="INFO", help="default: %(default)s")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    token = _read_token(args)
    try:
        server = make_server(args.root, args.host, args.port, token, args.certfile, args.keyfile)
    except (OSError, ValueError, ssl.SSLError) as exc:
        print(f"cfds-server: {exc}", file=sys.stderr)
        return 2
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
