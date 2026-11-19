"""fsspec client for :mod:`cf_datascience_fs.server`.

The filesystem is read-only. Paths are absolute within the served root, so
``/datasets/train.csv`` here is ``/mnt/TMP/datasets/train.csv`` on the server when the
server was started with ``--root /mnt/TMP``.
"""

from __future__ import annotations

import datetime
import errno
import os
import threading
from urllib.parse import quote, urlsplit

import requests
from fsspec.callbacks import DEFAULT_CALLBACK
from fsspec.spec import AbstractBufferedFile, AbstractFileSystem
from fsspec.utils import isfilelike, stringify_path
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ._version import __version__

DEFAULT_PORT = 8765
_PROTOCOLS = ("cfds", "cfdss")
_TLS_PROTOCOL = "cfdss"
_DOWNLOAD_CHUNK = 1024 * 1024


def _split_url(path: str) -> tuple[str | None, str | None, str]:
    """Split ``cfds://host:port/a/b`` into ``("cfds", "host:port", "/a/b")``.

    Plain paths come back as ``(None, None, path)``. Only the part up to the first
    ``/`` after the scheme is treated as the host, so ``?`` and ``#`` in file names
    are left alone.
    """
    for protocol in _PROTOCOLS:
        prefix = protocol + "://"
        if path.startswith(prefix):
            netloc, _, rest = path[len(prefix) :].partition("/")
            return protocol, netloc, "/" + rest
    return None, None, path


def _read_only(path: str | None = None) -> OSError:
    return OSError(errno.EROFS, "cfds is a read-only file system", path)


class FileChangedError(OSError):
    """The remote file was modified after it was opened or stat-ed."""


class _RangeNotSatisfiable(OSError):
    """The requested byte range starts at or beyond the end of the file."""


def _check_length(response, received: int, path: str) -> None:
    """Raise if fewer (or more) bytes arrived than the server promised."""
    declared = response.headers.get("Content-Length")
    if declared is not None and declared.isdigit() and int(declared) != received:
        raise OSError(
            f"{path}: incomplete read from the cfds server "
            f"({received} of {declared} bytes); the file may have changed"
        )


class CFDataScienceFileSystem(AbstractFileSystem):
    """Read-only view of a remote directory tree served by ``cfds-server``.

    Parameters
    ----------
    host, port:
        Where ``cfds-server`` is listening. In a URL they are the authority part:
        ``cfds://host:port/path``.
    token:
        Bearer token, if the server requires one. Defaults to ``$CFDS_TOKEN``.
    use_ssl:
        Talk HTTPS instead of HTTP. The ``cfdss://`` scheme sets this for you.
    verify:
        Passed to ``requests``: ``True``, ``False`` or the path of a CA bundle.
    timeout:
        Seconds to wait for the server to connect or to send more data.
    retries:
        How many times to retry a request after a connection failure or a
        502/503/504 response.
    block_size:
        Default read-ahead block for :meth:`open`, in bytes.
    use_listings_cache, listings_expiry_time, max_paths:
        fsspec directory-listing cache settings. The cache is off by default because
        the drive can change at any time.
    """

    protocol = _PROTOCOLS
    root_marker = "/"
    cachable = True

    def __init__(
        self,
        host: str = "localhost",
        port: int | None = None,
        token: str | None = None,
        use_ssl: bool = False,
        verify: bool | str = True,
        timeout: float | tuple[float, float] = 60,
        retries: int = 3,
        block_size: int | None = None,
        use_listings_cache: bool = False,
        listings_expiry_time: float | None = None,
        max_paths: int | None = None,
        **storage_options,
    ):
        super().__init__(
            use_listings_cache=use_listings_cache,
            listings_expiry_time=listings_expiry_time,
            max_paths=max_paths,
            **storage_options,
        )
        self.host = host
        self.port = int(port) if port is not None else DEFAULT_PORT
        self.token = token if token is not None else (os.environ.get("CFDS_TOKEN") or None)
        self.use_ssl = bool(use_ssl)
        self.verify = verify
        self.timeout = timeout
        self.retries = int(retries)
        if block_size is not None:
            self.blocksize = int(block_size)
        netloc = f"[{host}]" if ":" in host and not host.startswith("[") else host
        self._netloc = f"{netloc}:{self.port}"
        self.base_url = f"{'https' if self.use_ssl else 'http'}://{self._netloc}"
        self._session = None
        self._session_pid = None
        self._session_lock = threading.Lock()

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
            # urlsplit understands "host", "host:port" and "[v6-addr]:port".
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

    @property
    def session(self) -> requests.Session:
        """A ``requests`` session, recreated after a fork so sockets are not shared."""
        pid = os.getpid()
        if self._session is None or self._session_pid != pid:
            with self._session_lock:
                if self._session is None or self._session_pid != pid:
                    session = requests.Session()
                    retry = Retry(
                        total=self.retries,
                        connect=self.retries,
                        read=self.retries,
                        status=self.retries,
                        backoff_factor=0.5,
                        status_forcelist=(502, 503, 504),
                        allowed_methods=("GET", "HEAD"),
                        raise_on_status=False,
                    )
                    adapter = HTTPAdapter(max_retries=retry, pool_maxsize=16)
                    session.mount("http://", adapter)
                    session.mount("https://", adapter)
                    session.headers["User-Agent"] = f"cf-datascience-fs/{__version__}"
                    session.headers["Accept-Encoding"] = "identity"
                    if self.token:
                        session.headers["Authorization"] = f"Bearer {self.token}"
                    self._session, self._session_pid = session, pid
        return self._session

    def _url(self, endpoint: str, path: str, **params) -> str:
        # Built by hand so that undecodable file names (surrogate escapes) survive.
        query = "path=" + quote(path, safe="/", errors="surrogateescape")
        for key, value in params.items():
            query += f"&{key}={quote(str(value), safe='')}"
        return f"{self.base_url}/v1/{endpoint}?{query}"

    def _request(self, endpoint: str, path: str, headers=None, stream=False, **params):
        url = self._url(endpoint, path, **params)
        try:
            # verify is passed per request: requests lets $REQUESTS_CA_BUNDLE override a
            # session-level setting, which would silently ignore an explicit CA bundle.
            response = self.session.get(
                url,
                headers=headers,
                stream=stream,
                timeout=self.timeout,
                verify=self.verify,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise ConnectionError(
                f"cannot reach the cfds server at {self.base_url}: {exc}"
            ) from exc
        if response.status_code >= 300:
            try:
                self._raise_for_response(response, path)
            finally:
                response.close()
        return response

    @staticmethod
    def _raise_for_response(response, path: str):
        status = response.status_code
        code, message = None, None
        try:
            error = response.json().get("error") or {}
            code, message = error.get("code"), error.get("message")
        except (ValueError, AttributeError):
            pass
        message = message or f"HTTP {status} from the cfds server"
        if code == "is_a_directory":
            raise IsADirectoryError(errno.EISDIR, message, path)
        if status == 404:
            if code == "not_found":
                raise FileNotFoundError(errno.ENOENT, message, path)
            raise OSError(
                f"{message} (is a cfds server listening at this address, with a matching version?)"
            )
        if status in (401, 403):
            raise PermissionError(errno.EACCES, message, path)
        if status == 400:
            raise ValueError(message)
        if status == 412:
            raise FileChangedError(f"{path}: {message}")
        if status == 416:
            raise _RangeNotSatisfiable(f"{path}: {message}")
        raise OSError(f"{path}: {message} (HTTP {status})")

    # -- metadata -----------------------------------------------------------------

    @staticmethod
    def _clean_entry(entry: dict) -> dict:
        entry = dict(entry)
        entry.setdefault("size", 0)
        entry.setdefault("type", "other")
        return entry

    def _describe(self, path: str, list_entries: bool) -> dict:
        params = {} if list_entries else {"list": 0}
        response = self._request("ls", path, **params)
        try:
            payload = response.json()
            info = self._clean_entry(payload["info"])
            entries = payload.get("entries")
        except (ValueError, KeyError, TypeError) as exc:
            raise OSError(f"unexpected reply from the cfds server at {self.base_url}") from exc
        if entries is not None:
            entries = [self._clean_entry(entry) for entry in entries]
        return {"info": info, "entries": entries}

    def ls(self, path, detail=True, refresh=False, **kwargs):
        path = self._strip_protocol(path)
        entries = None
        if not refresh:
            try:
                entries = self.dircache[path]
            except KeyError:
                entries = None
        if entries is None:
            described = self._describe(path, list_entries=True)
            if described["info"]["type"] == "directory":
                entries = described["entries"] or []
                self.dircache[path] = entries
            else:
                entries = [described["info"]]
        if detail:
            return [dict(entry) for entry in entries]
        return [entry["name"] for entry in entries]

    def info(self, path, **kwargs):
        path = self._strip_protocol(path)
        return self._describe(path, list_entries=False)["info"]

    def invalidate_cache(self, path=None):
        if path is None:
            self.dircache.clear()
        else:
            path = self._strip_protocol(path)
            self.dircache.pop(path, None)
            self.dircache.pop(self._parent(path), None)
        super().invalidate_cache(path)

    def modified(self, path):
        mtime = self.info(path)["mtime"]
        return datetime.datetime.fromtimestamp(mtime, tz=datetime.timezone.utc)

    def created(self, path):
        raise NotImplementedError("the cfds server does not report creation times")

    # -- reading ------------------------------------------------------------------

    def _open(
        self,
        path,
        mode="rb",
        block_size=None,
        autocommit=True,
        cache_options=None,
        **kwargs,
    ):
        if mode != "rb":
            raise _read_only(path)
        info = self.info(path)
        if info["type"] == "directory":
            raise IsADirectoryError(errno.EISDIR, "is a directory", path)
        return CFDataScienceFile(
            self,
            path,
            mode=mode,
            block_size=block_size,
            cache_options=cache_options,
            size=info["size"],
            etag=info.get("etag"),
            **kwargs,
        )

    def _fetch(self, path: str, start: int | None, end: int | None, etag: str | None = None):
        """Bytes ``start`` (inclusive) to ``end`` (exclusive); ``None`` means open-ended."""
        headers = {}
        if etag:
            headers["If-Match"] = etag
        if start is not None and start < 0:
            headers["Range"] = f"bytes={start}"  # suffix form: the last -start bytes
        elif start or end is not None:
            first = start or 0
            if end is not None and end <= first:
                return b""
            headers["Range"] = f"bytes={first}-{'' if end is None else end - 1}"
        try:
            response = self._request("file", path, headers=headers)
        except _RangeNotSatisfiable:
            return b""  # reading at or past the end of the file, like a Python slice
        try:
            data = response.content
        except requests.RequestException as exc:
            raise OSError(f"{path}: the connection broke while reading: {exc}") from exc
        finally:
            response.close()
        _check_length(response, len(data), path)
        if response.status_code == 200 and "Range" in headers:
            # Something between us and the server ignored the Range header.
            if start is not None and start < 0:
                data = data[start:]
            else:
                data = data[start or 0 : end]
        return data

    def cat_file(self, path, start=None, end=None, **kwargs):
        path = self._strip_protocol(path)
        if start is None and end is None:
            return self._fetch(path, None, None)
        if (start is not None and start < 0) or (end is not None and end < 0):
            if start is not None and start < 0 and end is None:
                return self._fetch(path, start, None)
            size = self.info(path)["size"]
            if start is not None and start < 0:
                start = max(0, size + start)
            if end is not None and end < 0:
                end = max(0, size + end)
        return self._fetch(path, start, end)

    def get_file(self, rpath, lpath, callback=DEFAULT_CALLBACK, outfile=None, **kwargs):
        """Download one file in a single streamed request."""
        rpath = self._strip_protocol(rpath)
        if outfile is None and isfilelike(lpath):
            outfile = lpath
        info = self.info(rpath)
        if info["type"] == "directory":
            if outfile is None:
                os.makedirs(lpath, exist_ok=True)
            return None
        headers = {"If-Match": info["etag"]} if info.get("etag") else None
        response = self._request("file", rpath, headers=headers, stream=True)
        close_outfile = outfile is None
        try:
            if close_outfile:
                parent = os.path.dirname(os.path.abspath(lpath))
                os.makedirs(parent, exist_ok=True)
                outfile = open(lpath, "wb")  # noqa: SIM115 - closed in the finally below
            callback.set_size(info["size"])
            received = 0
            for chunk in response.iter_content(chunk_size=_DOWNLOAD_CHUNK):
                outfile.write(chunk)
                received += len(chunk)
                callback.relative_update(len(chunk))
            _check_length(response, received, rpath)
        except requests.RequestException as exc:
            raise OSError(f"{rpath}: the connection broke while downloading: {exc}") from exc
        finally:
            response.close()
            if close_outfile and outfile is not None:
                outfile.close()

    # -- everything that would write ---------------------------------------------

    def mkdir(self, path, create_parents=True, **kwargs):
        raise _read_only(path)

    def makedirs(self, path, exist_ok=False):
        raise _read_only(path)

    def rmdir(self, path):
        raise _read_only(path)

    def rm_file(self, path):
        raise _read_only(path)

    def _rm(self, path):
        raise _read_only(path)

    def rm(self, path, recursive=False, maxdepth=None):
        raise _read_only(path if isinstance(path, str) else None)

    def cp_file(self, path1, path2, **kwargs):
        raise _read_only(path2)

    def mv(self, path1, path2, recursive=False, maxdepth=None, **kwargs):
        raise _read_only(path2 if isinstance(path2, str) else None)

    def pipe_file(self, path, value, mode="overwrite", **kwargs):
        raise _read_only(path)

    def put_file(self, lpath, rpath, callback=DEFAULT_CALLBACK, mode="overwrite", **kwargs):
        raise _read_only(rpath)

    def touch(self, path, truncate=True, **kwargs):
        raise _read_only(path)


class CFDataScienceFile(AbstractBufferedFile):
    """Buffered, seekable, read-only file backed by HTTP range requests.

    The file's ETag is pinned when it is opened. If the file changes on the server
    while it is being read, the next read raises :class:`OSError` rather than
    returning a mix of old and new bytes.
    """

    def __init__(self, fs, path, mode="rb", etag=None, **kwargs):
        if mode != "rb":
            raise _read_only(path)
        self.etag = etag
        super().__init__(fs, path, mode=mode, **kwargs)

    def _fetch_range(self, start, end):
        return self.fs._fetch(self.path, start, end, etag=self.etag)
