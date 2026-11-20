"""File system operations confined to one directory tree ("mount").

Everything here works on a *real root* (an absolute, symlink-free path) and a client
path inside it. Paths are checked as strings first (``resolve_path``), and then every
open walks from the root one component at a time with ``O_NOFOLLOW``, so a directory
swapped for a symlink between the check and the use cannot lead outside the root.

Writes go through a temporary file in the target's directory and are renamed into
place, so readers never see a half-written file. This module imports only the
standard library.
"""

from __future__ import annotations

import errno
import logging
import os
import re
import secrets
import stat as stat_module
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from http import HTTPStatus
from typing import Callable

logger = logging.getLogger("cf_datascience_fs.server")

#: Prefix of in-progress upload files. They are hidden from listings, and clients may
#: not create names that start with it.
TMP_PREFIX = ".cfds-upload-"
READ_CHUNK = 1024 * 1024

_RANGE_RE = re.compile(r"\s*bytes\s*=\s*(\d{0,18})\s*-\s*(\d{0,18})\s*", re.ASCII)
_NOT_FOUND_ERRNOS = {errno.ENOENT, errno.ENOTDIR, errno.ELOOP, errno.ENAMETOOLONG}
_DIR_FD_OPEN = os.open in getattr(os, "supports_dir_fd", ())
_HAS_O_PATH = hasattr(os, "O_PATH")
_SCANDIR_FD = os.scandir in getattr(os, "supports_fd", ())
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_DIRECTORY = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)


class HTTPError(Exception):
    """An error that maps directly onto an HTTP error response."""

    def __init__(self, status: int, code: str, message: str, headers: dict | None = None):
        super().__init__(message)
        self.status = int(status)
        self.code = code
        self.message = message
        self.headers = headers or {}


def read_only(path: str) -> HTTPError:
    return HTTPError(HTTPStatus.FORBIDDEN, "read_only", f"read-only location: {path}")


def os_error_to_http(exc: OSError, path: str) -> HTTPError:
    if isinstance(exc, PermissionError) or exc.errno in (errno.EACCES, errno.EPERM):
        return HTTPError(HTTPStatus.FORBIDDEN, "forbidden", f"permission denied: {path}")
    if exc.errno in _NOT_FOUND_ERRNOS:
        return HTTPError(HTTPStatus.NOT_FOUND, "not_found", f"no such file or directory: {path}")
    if exc.errno == errno.ENOSPC or exc.errno == errno.EDQUOT:
        return HTTPError(
            HTTPStatus.INSUFFICIENT_STORAGE, "quota_exceeded", "the server's disk is full"
        )
    logger.warning("unexpected OS error for %r: %s", path, exc)
    return HTTPError(HTTPStatus.INTERNAL_SERVER_ERROR, "io_error", f"I/O error on {path}")


def is_within(real_path: str, real_root: str) -> bool:
    """True if ``real_path`` is ``real_root`` or below it. Both must be resolved paths."""
    if real_path == real_root:
        return True
    prefix = real_root if real_root.endswith(os.sep) else real_root + os.sep
    return real_path.startswith(prefix)


def normalize_path(client_path: str) -> str:
    """``/a//b/./c/`` -> ``/a/b/c``. Rejects NUL bytes and ``..``."""
    if "\x00" in client_path:
        raise HTTPError(HTTPStatus.BAD_REQUEST, "bad_request", "path contains a NUL byte")
    parts = [part for part in client_path.split("/") if part not in ("", ".")]
    if ".." in parts:
        raise HTTPError(HTTPStatus.BAD_REQUEST, "bad_request", "'..' is not allowed in paths")
    if os.sep != "/" and any(os.sep in part or ":" in part for part in parts):
        raise HTTPError(HTTPStatus.BAD_REQUEST, "bad_request", "invalid character in path")
    return "/" + "/".join(parts)


def resolve_path(real_root: str, client_path: str) -> tuple[str, str]:
    """Map a client path onto the local file system.

    Returns ``(normalized_client_path, real_local_path)``. Raises :class:`HTTPError`
    for malformed paths and for paths whose resolved location (symlinks followed)
    falls outside ``real_root``.
    """
    normalized = normalize_path(client_path)
    parts = [part for part in normalized.split("/") if part]
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


def etag_matches(header: str, etag: str | None) -> bool:
    candidates = [item.strip() for item in header.split(",")]
    if etag is None:
        return False
    return "*" in candidates or etag in candidates


def entry(name: str, st: os.stat_result, islink: bool, writable: bool = False) -> dict:
    if stat_module.S_ISDIR(st.st_mode):
        kind, size = "directory", 0
    elif stat_module.S_ISREG(st.st_mode):
        kind, size = "file", st.st_size
    else:
        kind, size = "other", 0
    result = {
        "name": name,
        "size": size,
        "type": kind,
        "mtime": st.st_mtime,
        "islink": islink,
        "writable": writable,
    }
    if kind == "file":
        result["etag"] = make_etag(st)
    return result


def _open_confined(real_root: str, real: str, normalized: str, flags: int) -> int:
    """Open the already-resolved path ``real`` without following any symlink.

    ``real`` contained no symlinks when it was computed, but someone with write access
    to the tree could swap a directory for a symlink before the open. So the path is
    walked from the root one component at a time, relative to the previous directory's
    descriptor, with ``O_NOFOLLOW``: a component that has become a symlink makes the
    open fail instead of leading outside.
    """
    if not _DIR_FD_OPEN:  # e.g. Windows: no descriptor-relative open available
        return os.open(real, flags | _NOFOLLOW)
    parts = [] if real == real_root else os.path.relpath(real, real_root).split(os.sep)
    fd = os.open(real_root, (_DIRECTORY if parts else flags) | _NOFOLLOW)
    try:
        for index, part in enumerate(parts):
            last = index == len(parts) - 1
            child = os.open(part, (flags if last else _DIRECTORY) | _NOFOLLOW, dir_fd=fd)
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


def _stat_confined(real_root: str, real: str, normalized: str) -> os.stat_result:
    """``os.stat`` that cannot be tricked into describing something outside the root."""
    if not _HAS_O_PATH:
        return os.stat(real)  # no descriptor-based check available on this platform
    fd = _open_confined(real_root, real, normalized, os.O_PATH)
    try:
        return os.fstat(fd)
    finally:
        os.close(fd)


def _display(prefix: str, inner: str) -> str:
    """Join a mount point and a path inside the mount."""
    if prefix in ("", "/"):
        return inner
    return prefix if inner == "/" else prefix + inner


def describe(
    real_root: str,
    client_path: str,
    list_entries: bool = True,
    *,
    prefix: str = "/",
    writable: bool = False,
) -> dict:
    """Build the ``/v1/ls`` response body for ``client_path``.

    Names in the result are ``prefix`` joined with the path inside the root.
    """
    normalized, real = resolve_path(real_root, client_path)
    try:
        st = _stat_confined(real_root, real, normalized)
        islink = normalized != "/" and os.path.islink(
            os.path.join(real_root, *normalized.strip("/").split("/"))
        )
        info = entry(_display(prefix, normalized), st, islink, writable)
        entries = None
        if list_entries and info["type"] == "directory":
            entries = _list_directory(real_root, normalized, real, prefix, writable)
    except OSError as exc:
        raise os_error_to_http(exc, normalized) from exc
    return {"info": info, "entries": entries}


def _list_directory(
    real_root: str, normalized: str, real: str, prefix: str, writable: bool
) -> list[dict]:
    base = _display(prefix, normalized).rstrip("/") + "/"
    entries = []
    dir_fd = None
    if _SCANDIR_FD:
        dir_fd = _open_confined(real_root, real, normalized, _DIRECTORY)
    try:
        with os.scandir(dir_fd if dir_fd is not None else real) as iterator:
            for item in iterator:
                if item.name.startswith(TMP_PREFIX):
                    continue  # an upload in progress
                try:
                    islink = item.is_symlink()
                    if islink:
                        # Links that are broken or point outside the root are not served,
                        # so they are left out of listings as well.
                        target = os.path.realpath(os.path.join(real, item.name))
                        if not is_within(target, real_root):
                            continue
                        st = _stat_confined(real_root, target, normalized + "/" + item.name)
                    else:
                        st = item.stat(follow_symlinks=False)
                except (OSError, ValueError, HTTPError):
                    continue
                entries.append(entry(base + item.name, st, islink, writable))
    finally:
        if dir_fd is not None:
            os.close(dir_fd)
    entries.sort(key=lambda item: item["name"])
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
        raise os_error_to_http(exc, normalized) from exc
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
        raise os_error_to_http(exc, normalized) from exc
    try:
        st = os.fstat(fd)
        if not stat_module.S_ISREG(st.st_mode):
            raise HTTPError(HTTPStatus.FORBIDDEN, "forbidden", f"not a regular file: {normalized}")
        return os.fdopen(fd, "rb", buffering=0), st
    except BaseException:
        os.close(fd)
        raise


# -- writing ----------------------------------------------------------------------------


class Quota:
    """Bytes a user may store. Thread-safe; shared by all of that user's requests.

    ``used`` includes bytes reserved by uploads in progress, so two concurrent uploads
    cannot both squeeze into the same free space.
    """

    def __init__(self, limit: int, used: int = 0):
        import threading

        self.limit = int(limit)
        self.used = max(0, int(used))
        self._lock = threading.Lock()

    @property
    def available(self) -> int:
        return max(0, self.limit - self.used)

    def reserve(self, nbytes: int) -> None:
        if nbytes <= 0:
            return
        with self._lock:
            if self.used + nbytes > self.limit:
                raise HTTPError(
                    HTTPStatus.INSUFFICIENT_STORAGE,
                    "quota_exceeded",
                    f"not enough space: {nbytes} bytes needed, "
                    f"{max(0, self.limit - self.used)} of {self.limit} available",
                )
            self.used += nbytes

    def adjust(self, delta: int) -> None:
        """Record a change that already happened (may be negative)."""
        with self._lock:
            self.used = max(0, self.used + int(delta))

    def snapshot(self) -> dict:
        with self._lock:
            used = self.used
        return {"limit": self.limit, "used": used, "available": max(0, self.limit - used)}


def _lstat_at(dir_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _regular_size(st: os.stat_result | None) -> int:
    return st.st_size if st is not None and stat_module.S_ISREG(st.st_mode) else 0


def _check_name(name: str, normalized: str) -> None:
    if name.startswith(TMP_PREFIX):
        raise HTTPError(
            HTTPStatus.BAD_REQUEST,
            "bad_request",
            f"names starting with {TMP_PREFIX!r} are reserved",
        )
    if len(name.encode("utf-8", "surrogateescape")) > 255:
        raise HTTPError(HTTPStatus.BAD_REQUEST, "bad_request", f"name too long: {normalized}")


@contextmanager
def parent_directory(real_root: str, client_path: str) -> Iterator[tuple[str, int, str]]:
    """Yield ``(normalized, parent_dir_fd, name)`` for the last component of a path.

    The parent is opened confined to the root. The last component itself is *not*
    followed: operations act on the name in the parent (a symlink is replaced or
    removed, never written through).
    """
    normalized = normalize_path(client_path)
    if normalized == "/":
        raise read_only("/")
    parent, _, name = normalized.rpartition("/")
    parent = parent or "/"
    _check_name(name, normalized)
    try:
        _, real_parent = resolve_path(real_root, parent)
    except HTTPError as exc:
        if exc.status == HTTPStatus.NOT_FOUND:
            raise _parent_not_found(normalized) from exc
        raise
    try:
        fd = _open_confined(real_root, real_parent, parent, _DIRECTORY)
    except FileNotFoundError as exc:
        raise _parent_not_found(normalized) from exc
    except NotADirectoryError as exc:
        raise HTTPError(
            HTTPStatus.BAD_REQUEST, "not_a_directory", f"not a directory: {parent}"
        ) from exc
    except OSError as exc:
        raise os_error_to_http(exc, parent) from exc
    try:
        yield normalized, fd, name
    finally:
        os.close(fd)


def _parent_not_found(normalized: str) -> HTTPError:
    return HTTPError(
        HTTPStatus.NOT_FOUND,
        "parent_not_found",
        f"the parent directory of {normalized} does not exist",
    )


def _exists_error(normalized: str) -> HTTPError:
    return HTTPError(HTTPStatus.CONFLICT, "exists", f"already exists: {normalized}")


def _precondition_failed(normalized: str, current: os.stat_result | None) -> HTTPError:
    headers = {}
    if current is not None and stat_module.S_ISREG(current.st_mode):
        headers["ETag"] = make_etag(current)
    return HTTPError(
        HTTPStatus.PRECONDITION_FAILED,
        "precondition_failed",
        f"precondition failed for {normalized}",
        headers=headers,
    )


def _check_write_preconditions(
    normalized: str,
    current: os.stat_result | None,
    *,
    overwrite: bool,
    create: bool,
    if_match: str | None,
    if_none_match: str | None,
) -> None:
    if current is not None and stat_module.S_ISDIR(current.st_mode):
        raise HTTPError(HTTPStatus.BAD_REQUEST, "is_a_directory", f"is a directory: {normalized}")
    if if_none_match is not None and current is not None:
        if if_none_match.strip() == "*" or etag_matches(if_none_match, _etag_of(current)):
            raise _precondition_failed(normalized, current)
    if if_match is not None and not etag_matches(if_match, _etag_of(current)):
        raise _precondition_failed(normalized, current)
    if current is not None and not overwrite:
        raise _exists_error(normalized)
    if current is None and not create:
        raise HTTPError(HTTPStatus.NOT_FOUND, "not_found", f"no such file: {normalized}")


def _etag_of(st: os.stat_result | None) -> str | None:
    if st is None or not stat_module.S_ISREG(st.st_mode):
        return None
    return make_etag(st)


def write_file(
    real_root: str,
    client_path: str,
    chunks: Iterable[bytes],
    *,
    length: int | None,
    overwrite: bool = True,
    create: bool = True,
    if_match: str | None = None,
    if_none_match: str | None = None,
    quota: Quota | None = None,
    display_prefix: str = "/",
    before_body: Callable[[], None] | None = None,
) -> tuple[dict, bool]:
    """Atomically create or replace a file with the bytes from ``chunks``.

    ``length`` is the declared size, or ``None`` if unknown (chunked upload). All
    checks happen before ``before_body`` is called and the first chunk is consumed.
    Returns ``(entry, created)``.
    """
    with parent_directory(real_root, client_path) as (normalized, dir_fd, name):
        current = _lstat_at(dir_fd, name)
        checks = {
            "overwrite": overwrite,
            "create": create,
            "if_match": if_match,
            "if_none_match": if_none_match,
        }
        _check_write_preconditions(normalized, current, **checks)
        old_size = _regular_size(current)
        reserved = 0
        if quota is not None and length is not None:
            reserved = max(0, length - old_size)
            quota.reserve(reserved)
        temporary = f"{TMP_PREFIX}{secrets.token_hex(8)}.tmp"
        tmp_fd = None
        committed = False
        try:
            if before_body is not None:
                before_body()
            try:
                tmp_fd = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                    0o644,
                    dir_fd=dir_fd,
                )
            except OSError as exc:
                raise os_error_to_http(exc, normalized) from exc
            written = 0
            for chunk in chunks:
                if not chunk:
                    continue
                if quota is not None and length is None:
                    needed = max(0, written + len(chunk) - old_size) - reserved
                    if needed > 0:
                        quota.reserve(needed)
                        reserved += needed
                if length is not None and written + len(chunk) > length:
                    raise HTTPError(
                        HTTPStatus.BAD_REQUEST, "bad_request", "body longer than Content-Length"
                    )
                view = memoryview(chunk)
                try:
                    while view:
                        view = view[os.write(tmp_fd, view) :]
                except OSError as exc:
                    raise os_error_to_http(exc, normalized) from exc
                written += len(chunk)
            if length is not None and written != length:
                raise HTTPError(
                    HTTPStatus.BAD_REQUEST,
                    "bad_request",
                    f"body ended after {written} of {length} bytes",
                )
            os.fsync(tmp_fd)
            os.close(tmp_fd)
            tmp_fd = None

            # Check again right before committing: another request may have changed
            # the target while this body was arriving.
            current = _lstat_at(dir_fd, name)
            _check_write_preconditions(normalized, current, **checks)
            old_size = _regular_size(current)
            try:
                if current is None:
                    try:
                        _rename_no_replace(temporary, name, dir_fd, dir_fd)
                    except FileExistsError:
                        # Another upload created the file just now. Replacing it is
                        # fine unless the caller asked for create-only semantics.
                        if not overwrite or if_none_match is not None or if_match is not None:
                            raise
                        current = _lstat_at(dir_fd, name)
                        _check_write_preconditions(normalized, current, **checks)
                        old_size = _regular_size(current)
                        os.rename(temporary, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
                else:
                    os.rename(temporary, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            except FileExistsError as exc:
                raise _exists_error(normalized) from exc
            except IsADirectoryError as exc:
                raise HTTPError(
                    HTTPStatus.BAD_REQUEST, "is_a_directory", f"is a directory: {normalized}"
                ) from exc
            committed = True
            if quota is not None:
                quota.adjust(written - old_size - reserved)
            st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            return entry(_display(display_prefix, normalized), st, False, True), current is None
        finally:
            if tmp_fd is not None:
                os.close(tmp_fd)
            if not committed:
                try:
                    os.unlink(temporary, dir_fd=dir_fd)
                except FileNotFoundError:
                    pass
                except OSError:
                    logger.warning("could not remove temporary upload %s", temporary)
                if quota is not None:
                    quota.adjust(-reserved)


_NO_HARD_LINKS = {errno.EPERM, errno.EMLINK, errno.ENOSYS, getattr(errno, "ENOTSUP", -1)}
_NO_HARD_LINKS.add(getattr(errno, "EOPNOTSUPP", -1))


def _rename_no_replace(src: str, dst: str, src_fd: int, dst_fd: int) -> None:
    """Rename a non-directory, failing with ``FileExistsError`` if ``dst`` exists.

    ``link()`` + ``unlink()`` never replaces an existing name, unlike ``rename()``.
    File systems without hard links fall back to a checked ``rename()``, which leaves
    a small window in which a name that just appeared could be replaced.
    """
    try:
        os.link(src, dst, src_dir_fd=src_fd, dst_dir_fd=dst_fd, follow_symlinks=False)
    except OSError as exc:
        if exc.errno not in _NO_HARD_LINKS:
            raise
        if _lstat_at(dst_fd, dst) is not None:
            raise FileExistsError(errno.EEXIST, "File exists", dst) from exc
        os.rename(src, dst, src_dir_fd=src_fd, dst_dir_fd=dst_fd)
        return
    os.unlink(src, dir_fd=src_fd)


def make_directory(
    real_root: str, client_path: str, *, parents: bool = False, display_prefix: str = "/"
) -> tuple[dict, bool]:
    """Create a directory. Returns ``(entry, created)``."""
    normalized = normalize_path(client_path)
    if normalized == "/":
        if parents:
            st = _stat_confined(real_root, real_root, "/")
            return entry(_display(display_prefix, "/"), st, False, True), False
        raise _exists_error(_display(display_prefix, "/"))
    if parents:
        parts = normalized.strip("/").split("/")
        created = False
        for index in range(1, len(parts) + 1):
            step = "/" + "/".join(parts[:index])
            if _is_directory(real_root, step, last=index == len(parts)):
                continue
            try:
                make_directory(real_root, step, display_prefix=display_prefix)
            except HTTPError as exc:
                # Someone else created it in the meantime; fine if it is a directory.
                if exc.code != "exists" or not _is_directory(real_root, step):
                    raise
            else:
                created = True
        info = describe(real_root, normalized, False, prefix=display_prefix, writable=True)
        return info["info"], created
    with parent_directory(real_root, normalized) as (_, dir_fd, name):
        try:
            os.mkdir(name, 0o755, dir_fd=dir_fd)
        except FileExistsError as exc:
            raise _exists_error(_display(display_prefix, normalized)) from exc
        except OSError as exc:
            raise os_error_to_http(exc, normalized) from exc
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        return entry(_display(display_prefix, normalized), st, False, True), True


def _is_directory(real_root: str, client_path: str, *, last: bool = True) -> bool:
    """False if nothing is there; raises if something other than a directory is."""
    try:
        normalized, real = resolve_path(real_root, client_path)
        st = _stat_confined(real_root, real, normalized)
    except (OSError, HTTPError):
        return False
    if not stat_module.S_ISDIR(st.st_mode):
        if last:
            raise _exists_error(client_path)
        raise HTTPError(
            HTTPStatus.BAD_REQUEST, "not_a_directory", f"not a directory: {client_path}"
        )
    return True


def _remove_tree(parent_fd: int, name: str) -> int:
    """Delete a directory tree without following symlinks. Returns bytes freed."""
    freed = 0
    fd = os.open(name, _DIRECTORY | _NOFOLLOW, dir_fd=parent_fd)
    try:
        with os.scandir(fd) as iterator:
            items = list(iterator)
        for item in items:
            st = item.stat(follow_symlinks=False)
            if stat_module.S_ISDIR(st.st_mode):
                freed += _remove_tree(fd, item.name)
            else:
                os.unlink(item.name, dir_fd=fd)
                freed += _regular_size(st)
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=parent_fd)
    return freed


def delete_path(
    real_root: str, client_path: str, *, recursive: bool = False, if_match: str | None = None
) -> int:
    """Delete a file, link or directory. Returns the bytes freed."""
    with parent_directory(real_root, client_path) as (normalized, dir_fd, name):
        current = _lstat_at(dir_fd, name)
        if current is None:
            raise HTTPError(
                HTTPStatus.NOT_FOUND, "not_found", f"no such file or directory: {normalized}"
            )
        if if_match is not None and not etag_matches(if_match, _etag_of(current)):
            raise _precondition_failed(normalized, current)
        try:
            if stat_module.S_ISDIR(current.st_mode):
                if recursive:
                    return _remove_tree(dir_fd, name)
                try:
                    os.rmdir(name, dir_fd=dir_fd)
                except OSError as exc:
                    if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
                        raise HTTPError(
                            HTTPStatus.CONFLICT,
                            "not_empty",
                            f"directory not empty: {normalized} (use recursive=1)",
                        ) from exc
                    raise
                return 0
            os.unlink(name, dir_fd=dir_fd)
            return _regular_size(current)
        except OSError as exc:
            raise os_error_to_http(exc, normalized) from exc


def move_path(
    real_root: str,
    src: str,
    dst: str,
    *,
    overwrite: bool = False,
    display_prefix: str = "/",
) -> tuple[dict, int]:
    """Rename within one root. Returns ``(entry_of_dst, bytes_freed)``.

    ``bytes_freed`` is the size of a file that was replaced at ``dst``.
    """
    src_normalized = normalize_path(src)
    dst_normalized = normalize_path(dst)
    if dst_normalized == src_normalized:
        raise HTTPError(HTTPStatus.BAD_REQUEST, "invalid_move", "source and destination are equal")
    if dst_normalized.startswith(src_normalized.rstrip("/") + "/"):
        raise HTTPError(
            HTTPStatus.BAD_REQUEST, "invalid_move", "cannot move a directory into itself"
        )
    with parent_directory(real_root, src_normalized) as (_, src_fd, src_name):
        source = _lstat_at(src_fd, src_name)
        if source is None:
            raise HTTPError(
                HTTPStatus.NOT_FOUND, "not_found", f"no such file or directory: {src_normalized}"
            )
        with parent_directory(real_root, dst_normalized) as (_, dst_fd, dst_name):
            target = _lstat_at(dst_fd, dst_name)
            freed = 0
            if target is not None:
                if not overwrite or stat_module.S_ISDIR(target.st_mode):
                    raise _exists_error(_display(display_prefix, dst_normalized))
                if stat_module.S_ISDIR(source.st_mode):
                    raise _exists_error(_display(display_prefix, dst_normalized))
                freed = _regular_size(target)
            try:
                if target is None and not stat_module.S_ISDIR(source.st_mode):
                    _rename_no_replace(src_name, dst_name, src_fd, dst_fd)
                else:
                    os.rename(src_name, dst_name, src_dir_fd=src_fd, dst_dir_fd=dst_fd)
            except OSError as exc:
                if exc.errno == errno.EINVAL:
                    raise HTTPError(
                        HTTPStatus.BAD_REQUEST,
                        "invalid_move",
                        "cannot move a directory into itself",
                    ) from exc
                if exc.errno in (errno.ENOTEMPTY, errno.EEXIST, errno.EISDIR, errno.ENOTDIR):
                    raise _exists_error(_display(display_prefix, dst_normalized)) from exc
                raise os_error_to_http(exc, src_normalized) from exc
            st = os.stat(dst_name, dir_fd=dst_fd, follow_symlinks=False)
            islink = stat_module.S_ISLNK(st.st_mode)
            if islink:
                try:
                    st = os.stat(dst_name, dir_fd=dst_fd)
                except OSError:
                    pass
            return entry(_display(display_prefix, dst_normalized), st, islink, True), freed


def iter_file(handle, chunk_size: int = READ_CHUNK) -> Iterator[bytes]:
    while True:
        chunk = handle.read(chunk_size)
        if not chunk:
            return
        yield chunk
