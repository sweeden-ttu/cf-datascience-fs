"""Read-write fsspec filesystem for ``cfds-server``, built on aiohttp.

:class:`CFDataScienceAsyncFileSystem` is registered for ``cfds://`` (HTTP) and
``cfdss://`` (HTTPS). It works from ordinary code, where fsspec runs its event loop
in a background thread, and from ``async`` code with ``asynchronous=True``::

    import fsspec
    fs = fsspec.filesystem("cfdss", host="storage.cloudfronts.group")
    fs.ls("/kaggle-datasets")
    with fs.open("/home/results/model.pkl", "wb") as f:
        f.write(blob)

Tokens come from the ``token`` argument, ``$CFDS_TOKEN``, or ``cfds login``.
"""

from __future__ import annotations

import asyncio
import datetime
import errno
import os
import secrets
import ssl
import tempfile
import urllib.request
import weakref
from urllib.parse import quote, urlsplit

import aiohttp
from fsspec.asyn import AsyncFileSystem, FSTimeoutError, sync, sync_wrapper
from fsspec.callbacks import DEFAULT_CALLBACK
from fsspec.spec import AbstractBufferedFile
from fsspec.utils import isfilelike, stringify_path

from ._credentials import TokenSource
from ._errors import FileChangedError, QuotaExceededError
from ._version import __version__

DEFAULT_PORT = 8765
_PROTOCOLS = ("cfds", "cfdss")
_TLS_PROTOCOL = "cfdss"
_CHUNK = 1024 * 1024
_RETRY_STATUSES = (502, 503, 504)
_IDEMPOTENT = ("GET", "HEAD", "PUT", "DELETE")


class _RangeNotSatisfiable(OSError):
    pass


def _split_url(path: str) -> tuple[str | None, str | None, str]:
    for protocol in _PROTOCOLS:
        prefix = protocol + "://"
        if path.startswith(prefix):
            netloc, _, rest = path[len(prefix) :].partition("/")
            return protocol, netloc, "/" + rest
    return None, None, path


def _raise_unless_missing(exc: OSError) -> None:
    if not isinstance(exc, (FileNotFoundError, NotADirectoryError)):
        raise exc


def _error_for(status: int, code: str | None, message: str | None, path: str) -> OSError:
    """Map a cfds error response onto the matching Python exception."""
    message = message or f"HTTP {status} from the cfds server"
    by_code = {
        "not_found": lambda: FileNotFoundError(errno.ENOENT, message, path),
        "parent_not_found": lambda: FileNotFoundError(errno.ENOENT, message, path),
        "is_a_directory": lambda: IsADirectoryError(errno.EISDIR, message, path),
        "not_a_directory": lambda: NotADirectoryError(errno.ENOTDIR, message, path),
        "exists": lambda: FileExistsError(errno.EEXIST, message, path),
        "not_empty": lambda: OSError(errno.ENOTEMPTY, message, path),
        "read_only": lambda: PermissionError(errno.EROFS, message, path),
        "forbidden": lambda: PermissionError(errno.EACCES, message, path),
        "cross_mount": lambda: OSError(errno.EXDEV, message, path),
        "invalid_move": lambda: OSError(errno.EINVAL, message, path),
        "quota_exceeded": lambda: QuotaExceededError(errno.EDQUOT, message, path),
        "precondition_failed": lambda: FileChangedError(f"{path}: {message}"),
        "source_changed": lambda: FileChangedError(f"{path}: {message}"),
        "range_not_satisfiable": lambda: _RangeNotSatisfiable(f"{path}: {message}"),
    }
    if code in by_code:
        error = by_code[code]()
        error.cfds_code = code
        return error
    if status == 401:
        return PermissionError(
            errno.EACCES, f"{message} (sign in with 'cfds login', or pass token=)", path
        )
    if status == 405:  # a server started without --writable refuses every write
        return PermissionError(errno.EROFS, message, path)
    if status in (403, 421):
        return PermissionError(errno.EACCES, message, path)
    if status == 404:
        return OSError(
            f"{message} (is a cfds server listening at this address, with a matching version?)"
        )
    if status == 400:
        return ValueError(message)
    if status == 507:
        return QuotaExceededError(errno.EDQUOT, message, path)
    return OSError(f"{path}: {message} (HTTP {status})")


def _ssl_option(verify):
    if verify is False:
        return False
    if isinstance(verify, str):
        return ssl.create_default_context(cafile=verify)
    return None  # aiohttp's default verification


class CFDataScienceAsyncFileSystem(AsyncFileSystem):
    """Remote drive served by ``cfds-server``, readable and writable.

    Parameters
    ----------
    host, port:
        Where the server listens; in a URL, the authority part
        (``cfdss://storage.cloudfronts.group/home/a.csv``). The port defaults to 443
        with TLS and 8765 without.
    url:
        Alternative to host/port/use_ssl: ``https://storage.cloudfronts.group``.
    token:
        Bearer token, or a callable returning one. Defaults to ``$CFDS_TOKEN`` and then
        to the token saved by ``cfds login`` for this server.
    use_ssl:
        HTTPS instead of HTTP; ``cfdss://`` sets it.
    verify:
        ``True``, ``False`` or the path of a CA bundle.
    timeout:
        Seconds to wait to connect, or between bytes received.
    retries:
        Retries after a connection failure or a 502/503/504, for requests that are
        safe to repeat.
    block_size:
        Read-ahead block for :meth:`open` in read mode.
    auto_mkdir:
        Create missing parent directories when writing or copying a file (like an
        object store). ``False`` makes such writes fail with ``FileNotFoundError``.
    use_saved_credentials:
        Use the token saved by ``cfds login`` when no other token is given.
    """

    protocol = _PROTOCOLS
    root_marker = "/"
    cachable = True
    async_impl = True

    def __init__(
        self,
        host: str = "localhost",
        port: int | None = None,
        token=None,
        use_ssl: bool = False,
        verify: bool | str = True,
        timeout: float = 60,
        retries: int = 3,
        block_size: int | None = None,
        url: str | None = None,
        use_saved_credentials: bool = True,
        auto_mkdir: bool = True,
        asynchronous: bool = False,
        loop=None,
        batch_size: int | None = None,
        use_listings_cache: bool = False,
        listings_expiry_time: float | None = None,
        max_paths: int | None = None,
        **storage_options,
    ):
        super().__init__(
            asynchronous=asynchronous,
            loop=loop,
            batch_size=batch_size,
            use_listings_cache=use_listings_cache,
            listings_expiry_time=listings_expiry_time,
            max_paths=max_paths,
            **storage_options,
        )
        if url is not None:
            parsed = urlsplit(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise ValueError(f"url must look like https://host[:port]: {url!r}")
            host, port, use_ssl = parsed.hostname, parsed.port, parsed.scheme == "https"
        self.host = host
        self.use_ssl = bool(use_ssl)
        self.port = int(port) if port is not None else (443 if self.use_ssl else DEFAULT_PORT)
        self.verify = verify
        self.auto_mkdir = bool(auto_mkdir)
        self.timeout = float(timeout)
        self.retries = int(retries)
        self._block_size = int(block_size) if block_size is not None else None
        if self._block_size is not None:
            self.blocksize = self._block_size
        bracketed = f"[{host}]" if ":" in host and not host.startswith("[") else host
        self._netloc = f"{bracketed}:{self.port}"
        scheme = "https" if self.use_ssl else "http"
        self.base_url = f"{scheme}://{self._netloc}"
        self.tokens = TokenSource(self.base_url, token, use_saved=use_saved_credentials)
        self._session = None  # created lazily, on the loop that uses it

    # -- URL handling -------------------------------------------------------------

    @classmethod
    def _strip_protocol(cls, path):
        if isinstance(path, list):
            return [cls._strip_protocol(item) for item in path]
        path = stringify_path(path)
        _, _, path = _split_url(path)
        return "/" + path.strip("/")

    @staticmethod
    def _get_kwargs_from_urls(path):
        protocol, netloc, _ = _split_url(stringify_path(path))
        out = {}
        if protocol is None:
            return out
        if protocol == _TLS_PROTOCOL:
            out["use_ssl"] = True
        if netloc:
            parsed = urlsplit("//" + netloc)
            if parsed.hostname:
                out["host"] = parsed.hostname
            if parsed.port is not None:
                out["port"] = parsed.port
        return out

    def unstrip_protocol(self, name: str) -> str:
        protocol = _TLS_PROTOCOL if self.use_ssl else _PROTOCOLS[0]
        return f"{protocol}://{self._netloc}{self._strip_protocol(name)}"

    # -- HTTP plumbing ------------------------------------------------------------

    async def set_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(ssl=_ssl_option(self.verify), limit=64)
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(
                    total=None, sock_connect=self.timeout, sock_read=self.timeout
                ),
                headers={
                    "User-Agent": f"cf-datascience-fs/{__version__}",
                    "Accept-Encoding": "identity",
                },
                # Not trust_env: aiohttp would then send ~/.netrc passwords to the server.
                trust_env=False,
                auto_decompress=False,
            )
            if not self.asynchronous:
                weakref.finalize(self, self.close_session, self.loop, self._session)
        return self._session

    @staticmethod
    def close_session(loop, session):
        if loop is not None and loop.is_running():
            try:
                sync(loop, session.close, timeout=0.1)
                return
            except (TimeoutError, FSTimeoutError, NotImplementedError, RuntimeError):
                pass
        connector = getattr(session, "_connector", None)
        if connector is not None:
            connector._close()

    def close(self) -> None:
        """Close pooled connections. The filesystem can still be used afterwards."""
        session, self._session = self._session, None
        if session is not None and not session.closed:
            self.close_session(getattr(self, "_loop", None) and self.loop, session)

    def _url(self, endpoint: str, **params) -> str:
        # Built by hand so that undecodable file names (surrogate escapes) survive.
        parts = []
        for key, value in params.items():
            if value is None:
                continue
            if isinstance(value, bool):
                value = int(value)
            parts.append(f"{key}={quote(str(value), safe='/', errors='surrogateescape')}")
        return f"{self.base_url}/v1/{endpoint}?{'&'.join(parts)}"

    def _proxy_for(self, url: str) -> str | None:
        host = urlsplit(url).hostname or ""
        if urllib.request.proxy_bypass(host):
            return None
        proxies = urllib.request.getproxies()
        return proxies.get("https" if self.use_ssl else "http")

    async def _token(self, refresh: bool = False) -> str | None:
        if refresh:
            return await asyncio.get_running_loop().run_in_executor(None, self.tokens.refresh)
        return await asyncio.get_running_loop().run_in_executor(None, self.tokens.get)

    async def _send(self, method: str, url: str, path: str, headers=None, body=None, ok=()):
        """Send a request and return the open response (the caller releases it).

        ``body`` is ``None``, ``bytes``, or a zero-argument callable that returns a
        fresh async iterator of chunks (so that the request can be retried). Responses
        with an error status are turned into exceptions here.
        """
        session = await self.set_session()
        token = await self._token()
        refreshed = False
        attempt = 0
        while True:
            request_headers = dict(headers or {})
            if token:
                request_headers["Authorization"] = f"Bearer {token}"
            data = body() if callable(body) else body
            try:
                response = await session.request(
                    method,
                    url,
                    headers=request_headers,
                    data=data,
                    allow_redirects=False,
                    proxy=self._proxy_for(url),
                    expect100=callable(body),
                )
            except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as exc:
                if method in _IDEMPOTENT and attempt < self.retries:
                    attempt += 1
                    await asyncio.sleep(0.5 * 2 ** (attempt - 1))
                    continue
                raise ConnectionError(
                    f"cannot reach the cfds server at {self.base_url}: {exc}"
                ) from exc
            status = response.status
            if status < 300 or status in ok:
                return response
            if status in _RETRY_STATUSES and method in _IDEMPOTENT and attempt < self.retries:
                response.release()
                attempt += 1
                await asyncio.sleep(0.5 * 2 ** (attempt - 1))
                continue
            if status == 401 and not refreshed:
                refreshed = True
                new_token = await self._token(refresh=True)
                if new_token and new_token != token:
                    response.release()
                    token = new_token
                    continue
            try:
                payload = await response.json(content_type=None)
                error = payload.get("error") or {}
                code, message = error.get("code"), error.get("message")
            except (ValueError, AttributeError, aiohttp.ClientError):
                code, message = None, None
            finally:
                response.release()
            raise _error_for(status, code, message, path)

    async def _json(self, method: str, endpoint: str, where: str, /, **params):
        response = await self._send(method, self._url(endpoint, **params), where)
        try:
            if response.status == 204:
                return None
            return await response.json(content_type=None)
        except ValueError as exc:
            raise OSError(f"unexpected reply from the cfds server at {self.base_url}") from exc
        finally:
            response.release()

    # -- metadata -------------------------------------------------------------------

    @staticmethod
    def _clean_entry(entry: dict) -> dict:
        entry = dict(entry)
        entry.setdefault("size", 0)
        entry.setdefault("type", "other")
        return entry

    async def _describe(self, path: str, list_entries: bool) -> dict:
        payload = await self._json("GET", "ls", path, path=path, list=None if list_entries else 0)
        try:
            info = self._clean_entry(payload["info"])
            entries = payload.get("entries")
        except (KeyError, TypeError) as exc:
            raise OSError(f"unexpected reply from the cfds server at {self.base_url}") from exc
        if entries is not None:
            entries = [self._clean_entry(entry) for entry in entries]
        return {"info": info, "entries": entries}

    async def _ls(self, path, detail=True, refresh=False, **kwargs):
        path = self._strip_protocol(path)
        entries = None
        if not refresh:
            try:
                entries = self.dircache[path]
            except KeyError:
                entries = None
        if entries is None:
            self.dircache.pop(path, None)
            described = await self._describe(path, list_entries=True)
            if described["info"]["type"] == "directory":
                entries = described["entries"] or []
                self.dircache[path] = entries
            else:
                entries = [described["info"]]
        if detail:
            return [dict(entry) for entry in entries]
        return [entry["name"] for entry in entries]

    async def _info(self, path, **kwargs):
        path = self._strip_protocol(path)
        return (await self._describe(path, list_entries=False))["info"]

    async def _exists(self, path, **kwargs):
        try:
            await self._info(path)
        except (FileNotFoundError, NotADirectoryError):
            return False
        return True

    async def _isdir(self, path):
        try:
            return (await self._info(path))["type"] == "directory"
        except (FileNotFoundError, NotADirectoryError):
            return False

    async def _isfile(self, path):
        try:
            return (await self._info(path))["type"] == "file"
        except (FileNotFoundError, NotADirectoryError):
            return False

    async def _me(self) -> dict:
        """Who the server thinks we are, our quota and our mounts (``GET /v1/me``)."""
        return await self._json("GET", "me", "/")

    me = sync_wrapper(_me)

    def invalidate_cache(self, path=None):
        if path is None:
            self.dircache.clear()
        else:
            path = self._strip_protocol(path)
            below = path.rstrip("/") + "/"
            for key in [key for key in self.dircache if key == path or key.startswith(below)]:
                self.dircache.pop(key, None)
            self.dircache.pop(self._parent(path), None)
        super().invalidate_cache(path)

    @staticmethod
    def _split_listing(path: str, listing: list[dict]):
        """``(dirs, files, descend)`` for one directory listing; symlinked directories
        are in ``dirs`` but not in ``descend``."""
        dirs, files, descend = {}, {}, {}
        for info in listing:
            pathname = info["name"].rstrip("/") or "/"
            name = pathname.rsplit("/", 1)[-1]
            if info["type"] == "directory" and pathname != path:
                dirs[name] = info
                if not info.get("islink"):
                    descend[name] = pathname
            elif pathname == path:
                files[""] = info  # the path given was a file
            else:
                files[name] = info
        return dirs, files, descend

    async def _walk(self, path, maxdepth=None, on_error=_raise_unless_missing, **kwargs):
        """Like fsspec's ``walk``, but symlinked directories are listed and not entered,
        and errors other than "not found" are raised (``on_error="omit"`` skips them)."""
        if maxdepth is not None and maxdepth < 1:
            raise ValueError("maxdepth must be at least 1")
        path = self._strip_protocol(path)
        detail = kwargs.pop("detail", False)
        topdown = kwargs.pop("topdown", True)
        try:
            listing = await self._ls(path, detail=True, **kwargs)
        except OSError as exc:
            if on_error == "raise":
                raise
            if callable(on_error):
                on_error(exc)
            return
        dirs, files, descend = self._split_listing(path, listing)
        if topdown:
            yielded_dirs = dirs if detail else list(dirs)
            yield path, yielded_dirs, (files if detail else list(files))
            descend = {name: full for name, full in descend.items() if name in yielded_dirs}
        if maxdepth is None or maxdepth > 1:
            for full_path in descend.values():
                async for item in self._walk(
                    full_path,
                    maxdepth=None if maxdepth is None else maxdepth - 1,
                    on_error=on_error,
                    detail=detail,
                    topdown=topdown,
                    **kwargs,
                ):
                    yield item
        if not topdown:
            yield path, (dirs if detail else list(dirs)), (files if detail else list(files))

    def walk(self, path, maxdepth=None, topdown=True, on_error=_raise_unless_missing, **kwargs):
        """Synchronous :meth:`_walk` (defined here because older fsspec versions do not
        derive it from the async one)."""
        if maxdepth is not None and maxdepth < 1:
            raise ValueError("maxdepth must be at least 1")
        path = self._strip_protocol(path)
        detail = kwargs.pop("detail", False)
        try:
            listing = self.ls(path, detail=True, **kwargs)
        except OSError as exc:
            if on_error == "raise":
                raise
            if callable(on_error):
                on_error(exc)
            return
        dirs, files, descend = self._split_listing(path, listing)
        if topdown:
            yielded_dirs = dirs if detail else list(dirs)
            yield path, yielded_dirs, (files if detail else list(files))
            descend = {name: full for name, full in descend.items() if name in yielded_dirs}
        if maxdepth is None or maxdepth > 1:
            for full_path in descend.values():
                yield from self.walk(
                    full_path,
                    maxdepth=None if maxdepth is None else maxdepth - 1,
                    topdown=topdown,
                    on_error=on_error,
                    detail=detail,
                    **kwargs,
                )
        if not topdown:
            yield path, (dirs if detail else list(dirs)), (files if detail else list(files))

    async def _modified(self, path):
        mtime = (await self._info(path))["mtime"]
        return datetime.datetime.fromtimestamp(mtime, tz=datetime.timezone.utc)

    modified = sync_wrapper(_modified)

    def created(self, path):
        raise NotImplementedError("the cfds server does not report creation times")

    # -- reading --------------------------------------------------------------------

    async def _incomplete(self, path: str, etag: str | None, cause: Exception) -> OSError:
        try:
            current = (await self._info(path)).get("etag")
        except FileNotFoundError:
            return FileChangedError(f"{path}: the file was removed while it was being read")
        except OSError:
            current = etag
        if etag is not None and current != etag:
            return FileChangedError(f"{path}: the file changed on the server while it was read")
        return OSError(f"{path}: the transfer was interrupted: {cause}")

    async def _fetch(self, path, start, end, etag=None) -> bytes:
        headers = {}
        if etag:
            headers["If-Match"] = etag
        if start is not None and start < 0:
            headers["Range"] = f"bytes={start}"
        elif start or end is not None:
            first = start or 0
            if end is not None and end <= first:
                return b""
            headers["Range"] = f"bytes={first}-{'' if end is None else end - 1}"
        try:
            response = await self._send("GET", self._url("file", path=path), path, headers)
        except _RangeNotSatisfiable:
            return b""
        sent_etag = etag or response.headers.get("ETag")
        try:
            data = await response.read()
            declared = response.headers.get("Content-Length")
            if declared is not None and declared.isdigit() and int(declared) != len(data):
                raise aiohttp.ClientPayloadError(f"{len(data)} of {declared} bytes")
        except (aiohttp.ClientPayloadError, aiohttp.ClientConnectionError) as exc:
            raise await self._incomplete(path, sent_etag, exc) from exc
        finally:
            response.release()
        if response.status == 200 and "Range" in headers:
            data = data[start:] if start is not None and start < 0 else data[start or 0 : end]
        return data

    async def _cat_file(self, path, start=None, end=None, etag=None, **kwargs):
        path = self._strip_protocol(path)
        if start is None and end is None:
            return await self._fetch(path, None, None, etag)
        if (start is not None and start < 0) or (end is not None and end < 0):
            if start is not None and start < 0 and end is None:
                return await self._fetch(path, start, None, etag)
            size = (await self._info(path))["size"]
            if start is not None and start < 0:
                start = max(0, size + start)
            if end is not None and end < 0:
                end = max(0, size + end)
        return await self._fetch(path, start, end, etag)

    async def _get_file(self, rpath, lpath, callback=DEFAULT_CALLBACK, outfile=None, **kwargs):
        """Download one file. A local target is replaced only once the download is
        complete; a failed download leaves it as it was."""
        rpath = self._strip_protocol(rpath)
        if outfile is None and isfilelike(lpath):
            outfile = lpath
        info = await self._info(rpath)
        if info["type"] == "directory":
            if outfile is None:
                os.makedirs(lpath, exist_ok=True)
            return None
        etag = info.get("etag")
        response = await self._send(
            "GET", self._url("file", path=rpath), rpath, {"If-Match": etag} if etag else None
        )
        temporary = None
        try:
            if outfile is None:
                parent = os.path.dirname(os.path.abspath(lpath))
                os.makedirs(parent, exist_ok=True)
                temporary = f"{os.path.abspath(lpath)}.{secrets.token_hex(4)}.part"
                target = open(temporary, "xb")  # noqa: SIM115 - closed below
            else:
                target = outfile
            try:
                callback.set_size(info["size"])
                received = 0
                async for chunk in response.content.iter_chunked(_CHUNK):
                    target.write(chunk)
                    received += len(chunk)
                    callback.relative_update(len(chunk))
                declared = response.headers.get("Content-Length")
                if declared is not None and declared.isdigit() and int(declared) != received:
                    raise aiohttp.ClientPayloadError(f"{received} of {declared} bytes")
            except (aiohttp.ClientPayloadError, aiohttp.ClientConnectionError) as exc:
                raise await self._incomplete(rpath, etag, exc) from exc
            finally:
                if temporary is not None:
                    target.close()
            if temporary is not None:
                os.replace(temporary, lpath)
                temporary = None
        finally:
            response.release()
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    # -- writing --------------------------------------------------------------------

    async def _with_parents(self, path, attempt):
        """Run ``attempt()``; if the target's directory is missing and ``auto_mkdir`` is
        on, create it and try once more."""
        try:
            return await attempt()
        except FileNotFoundError as exc:
            if not self.auto_mkdir or getattr(exc, "cfds_code", None) != "parent_not_found":
                raise
        parent = self._parent(path)
        await self._json("POST", "mkdir", parent, path=parent, parents=1)
        self.invalidate_cache(parent)
        return await attempt()

    async def _upload(self, path, body, size, *, overwrite=True, if_match=None, callback=None):
        """PUT ``size`` bytes from ``body`` (bytes, or a callable giving a fresh async
        iterator) to ``path``. Returns the new entry."""
        path = self._strip_protocol(path)
        headers = {"Content-Type": "application/octet-stream", "Content-Length": str(size)}
        if if_match:
            headers["If-Match"] = if_match
        url = self._url("file", path=path, overwrite=None if overwrite else 0)
        response = await self._with_parents(
            path, lambda: self._send("PUT", url, path, headers, body)
        )
        try:
            entry = await response.json(content_type=None)
        finally:
            response.release()
        self.invalidate_cache(path)
        if callback is not None and not callable(body):
            callback.relative_update(size)
        return entry

    async def _pipe_file(self, path, value, mode="overwrite", **kwargs):
        value = bytes(value)
        return await self._upload(path, value, len(value), overwrite=mode != "create")

    async def _put_file(self, lpath, rpath, callback=DEFAULT_CALLBACK, mode="overwrite", **kwargs):
        if os.path.isdir(lpath):
            await self._makedirs(rpath, exist_ok=True)
            return None
        size = os.path.getsize(lpath)
        callback.set_size(size)
        loop = asyncio.get_running_loop()

        def chunks():
            async def generate():
                with open(lpath, "rb") as handle:
                    sent = 0
                    while sent < size:
                        chunk = await loop.run_in_executor(
                            None, handle.read, min(_CHUNK, size - sent)
                        )
                        if not chunk:
                            raise OSError(f"{lpath} shrank while it was being uploaded")
                        sent += len(chunk)
                        callback.relative_update(len(chunk))
                        yield chunk

            return generate()

        return await self._upload(rpath, chunks, size, overwrite=mode != "create")

    async def _upload_fileobj(self, path, fileobj, size, *, overwrite=True, if_match=None):
        def chunks():
            async def generate():
                fileobj.seek(0)
                while True:
                    chunk = fileobj.read(_CHUNK)
                    if not chunk:
                        return
                    yield chunk

            return generate()

        return await self._upload(path, chunks, size, overwrite=overwrite, if_match=if_match)

    upload_fileobj = sync_wrapper(_upload_fileobj)

    async def _mkdir(self, path, create_parents=True, **kwargs):
        path = self._strip_protocol(path)
        await self._json("POST", "mkdir", path, path=path, parents=1 if create_parents else None)
        self.invalidate_cache(path)

    async def _makedirs(self, path, exist_ok=False):
        path = self._strip_protocol(path)
        if not exist_ok and await self._exists(path):
            raise FileExistsError(errno.EEXIST, "already exists", path)
        await self._json("POST", "mkdir", path, path=path, parents=1)
        self.invalidate_cache(path)

    async def _rm_file(self, path, **kwargs):
        path = self._strip_protocol(path)
        info = await self._info(path)
        if info["type"] == "directory":
            raise IsADirectoryError(
                errno.EISDIR, "is a directory (use rm with recursive=True)", path
            )
        await self._json("DELETE", "file", path, path=path)
        self.invalidate_cache(path)

    async def _rmdir(self, path):
        path = self._strip_protocol(path)
        info = await self._info(path)
        if info["type"] != "directory":
            raise NotADirectoryError(errno.ENOTDIR, "not a directory", path)
        await self._json("DELETE", "file", path, path=path)
        self.invalidate_cache(path)

    rmdir = sync_wrapper(_rmdir)

    async def _rm(self, path, recursive=False, maxdepth=None, batch_size=None, **kwargs):
        """Delete files, or whole directories with ``recursive=True`` (done by the server
        in one request). Glob patterns are expanded first."""
        paths = path if isinstance(path, list) else [path]
        targets = []
        if maxdepth is not None:
            # Only part of each tree goes: delete what was found one entry at a time.
            targets = await self._expand_path(path, recursive=recursive, maxdepth=maxdepth)
            recursive = False
        for item in [] if maxdepth is not None else paths:
            if isinstance(item, str) and any(ch in item for ch in "*?["):
                targets += await self._expand_path(item, recursive=False)
            else:
                targets.append(self._strip_protocol(item))
        # Children first, so deleting a parent never makes a later entry fail.
        for target in sorted(set(targets), key=len, reverse=True):
            try:
                await self._json(
                    "DELETE", "file", target, path=target, recursive=1 if recursive else None
                )
            except FileNotFoundError:
                if not any(target.startswith(other.rstrip("/") + "/") for other in targets):
                    raise
            self.invalidate_cache(target)

    async def _cp_file(self, path1, path2, **kwargs):
        path1, path2 = self._strip_protocol(path1), self._strip_protocol(path2)
        info = await self._info(path1)
        if info["type"] == "directory":
            await self._makedirs(path2, exist_ok=True)
            return
        await self._with_parents(
            path2, lambda: self._json("POST", "cp", path2, src=path1, dst=path2, overwrite=1)
        )
        self.invalidate_cache(path2)

    async def _mv(self, path1, path2, recursive=False, maxdepth=None, overwrite=True, **kwargs):
        """Rename on the server. Directories move in one request (``recursive`` is not
        needed). Moving between ``/home`` and ``/kaggle-datasets`` is copy + delete."""
        path1, path2 = self._strip_protocol(path1), self._strip_protocol(path2)
        if path1 == path2:
            return
        try:
            await self._json(
                "POST", "mv", path1, src=path1, dst=path2, overwrite=1 if overwrite else None
            )
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            await self._copy(path1, path2, recursive=True)
            await self._rm(path1, recursive=True)
        self.invalidate_cache(path1)
        self.invalidate_cache(path2)

    def mv(self, path1, path2, recursive=False, maxdepth=None, **kwargs):
        if isinstance(path1, list) or isinstance(path2, list) or maxdepth is not None:
            return super().mv(path1, path2, recursive=recursive, maxdepth=maxdepth, **kwargs)
        return sync(self.loop, self._mv, path1, path2, recursive=recursive, **kwargs)

    async def _touch(self, path, truncate=True, **kwargs):
        if truncate or not await self._exists(path):
            await self._pipe_file(path, b"")
        else:
            raise NotImplementedError("the cfds server cannot update modification times")

    touch = sync_wrapper(_touch)

    # -- files ----------------------------------------------------------------------

    def _open(
        self,
        path,
        mode="rb",
        block_size=None,
        autocommit=True,
        cache_options=None,
        **kwargs,
    ):
        path = self._strip_protocol(path)
        if mode not in ("rb", "wb", "xb"):
            raise NotImplementedError(f"mode {mode!r} is not supported (use rb, wb or xb)")
        if not autocommit:
            raise NotImplementedError("transactions are not supported")
        kwargs.pop("size", None)
        kwargs.pop("etag", None)
        if mode != "rb":
            if mode == "xb" and self.exists(path):
                raise FileExistsError(errno.EEXIST, "already exists", path)
            return CFDataScienceAsyncFile(self, path, mode=mode, block_size=block_size, **kwargs)
        info = self.info(path)
        if info["type"] == "directory":
            raise IsADirectoryError(errno.EISDIR, "is a directory", path)
        if info["type"] != "file":
            raise PermissionError(errno.EACCES, "not a regular file", path)
        return CFDataScienceAsyncFile(
            self,
            path,
            mode=mode,
            block_size=block_size if block_size is not None else self._block_size,
            cache_options=cache_options,
            size=info["size"],
            etag=info.get("etag"),
            **kwargs,
        )


class CFDataScienceAsyncFile(AbstractBufferedFile):
    """A file on a cfds server.

    Reading: buffered and seekable, with HTTP range requests. The ETag is pinned when
    the file is opened; if the file changes on the server, the next read raises
    :class:`FileChangedError` instead of mixing old and new bytes.

    Writing (``wb``/``xb``): data is collected in a local temporary file and uploaded in
    one request when the file is closed, so the server only ever has the old file or
    the complete new one.
    """

    def __init__(self, fs, path, mode="rb", etag=None, **kwargs):
        self.etag = etag
        self._spool = None
        self._overwrite = mode != "xb"
        super().__init__(fs, path, mode="rb" if mode == "rb" else "wb", **kwargs)

    def _fetch_range(self, start, end):
        return self.fs.cat_file(self.path, start, end, etag=self.etag)

    def _initiate_upload(self):
        self._spool = tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024)

    def _upload_chunk(self, final=False):
        if self._spool is None:
            self._initiate_upload()
        self.buffer.seek(0)
        self._spool.write(self.buffer.read())
        if final:
            size = self._spool.tell()
            try:
                entry = self.fs.upload_fileobj(
                    self.path, self._spool, size, overwrite=self._overwrite
                )
            finally:
                self._spool.close()
                self._spool = None
            self.etag = entry.get("etag") if isinstance(entry, dict) else None
        return True

    def discard(self):
        if self._spool is not None:
            self._spool.close()
            self._spool = None
