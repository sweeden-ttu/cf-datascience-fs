"""Per-user storage: where each signed-in user's ``/home`` lives and how much it may hold.

Users are created the first time they make an authenticated request. The mapping from
the identity provider's ``sub`` to a directory, and each user's quota, are kept in a
small SQLite database at ``<users_root>/.cfds/users.sqlite3``. To give one user a
different quota, change ``quota_limit`` (bytes) in the ``users`` table; the server
picks it up within a minute.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
import stat as stat_module
import threading
import time
from dataclasses import dataclass

from ._fsops import TMP_PREFIX, Quota

logger = logging.getLogger("cf_datascience_fs.server")

#: Free tier: 100 GiB.
DEFAULT_QUOTA = 100 * 1024**3
STATE_DIR = ".cfds"
_SAFE_SUB = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}", re.ASCII)
_LIMIT_REFRESH = 60.0
_STALE_UPLOAD = 24 * 3600

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    sub         TEXT PRIMARY KEY,
    dir         TEXT NOT NULL UNIQUE,
    email       TEXT,
    name        TEXT,
    quota_limit INTEGER,
    created     REAL NOT NULL,
    last_seen   REAL NOT NULL
)
"""


def directory_name(sub: str) -> str:
    """Directory for a user: the ``sub`` itself when it is a plain lowercase name,
    otherwise ``h-`` and a hash of it. The two kinds never collide."""
    if _SAFE_SUB.fullmatch(sub) and not sub.startswith("h-"):
        return sub
    return "h-" + hashlib.sha256(sub.encode("utf-8", "surrogatepass")).hexdigest()[:40]


def disk_usage(path: str) -> int:
    """Apparent size of the regular files below ``path``; symlinks are not followed.

    Leftovers of uploads that were interrupted a day or more ago are deleted.
    """
    total = 0
    now = time.time()
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            iterator = os.scandir(current)
        except OSError:
            continue
        with iterator:
            for item in iterator:
                try:
                    st = item.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat_module.S_ISDIR(st.st_mode):
                    stack.append(item.path)
                elif stat_module.S_ISREG(st.st_mode):
                    if item.name.startswith(TMP_PREFIX) and now - st.st_mtime > _STALE_UPLOAD:
                        try:
                            os.unlink(item.path)
                            continue
                        except OSError:
                            pass
                    total += st.st_size
    return total


@dataclass
class _Cached:
    path: str
    quota: Quota
    checked: float


class UserStore:
    def __init__(self, users_root: str | os.PathLike, default_quota: int = DEFAULT_QUOTA):
        root = os.fspath(users_root)
        if not os.path.isdir(root):
            raise NotADirectoryError(f"users root is not a directory: {root}")
        self.users_root = os.path.realpath(root)
        self.default_quota = int(default_quota)
        state = os.path.join(self.users_root, STATE_DIR)
        os.makedirs(state, mode=0o700, exist_ok=True)
        self.db_path = os.path.join(state, "users.sqlite3")
        self._db = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(_SCHEMA)
        self._lock = threading.Lock()
        self._cache: dict[str, _Cached] = {}

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _row(self, sub: str, email: str | None, name: str | None) -> tuple[str, int]:
        """Return ``(dir, quota_limit)``, creating the user if needed."""
        now = time.time()
        with self._lock:
            row = self._db.execute(
                "SELECT dir, quota_limit, last_seen FROM users WHERE sub = ?", (sub,)
            ).fetchone()
            if row is None:
                directory = directory_name(sub)
                self._db.execute(
                    "INSERT INTO users (sub, dir, email, name, quota_limit, created, last_seen)"
                    " VALUES (?, ?, ?, ?, NULL, ?, ?)",
                    (sub, directory, email, name, now, now),
                )
                logger.info("new user %r (directory %s)", sub, directory)
                return directory, self.default_quota
            directory, limit, last_seen = row
            if now - last_seen > 3600:
                self._db.execute(
                    "UPDATE users SET last_seen = ?, email = COALESCE(?, email),"
                    " name = COALESCE(?, name) WHERE sub = ?",
                    (now, email, name, sub),
                )
            return directory, self.default_quota if limit is None else int(limit)

    def home(self, sub: str, email: str | None = None, name: str | None = None):
        """Return ``(real_path, quota)`` of a user's private space, creating it if needed."""
        cached = self._cache.get(sub)
        if cached is not None and time.monotonic() - cached.checked < _LIMIT_REFRESH:
            return cached.path, cached.quota
        directory, limit = self._row(sub, email, name)
        path = os.path.join(self.users_root, directory)
        if directory == STATE_DIR or os.path.dirname(path) != self.users_root:
            raise ValueError(f"unusable directory name for {sub!r}")
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
        if cached is None:
            quota = Quota(limit, disk_usage(path))
            with self._lock:
                cached = self._cache.setdefault(sub, _Cached(path, quota, time.monotonic()))
        cached.path = path
        cached.quota.limit = limit
        cached.checked = time.monotonic()
        return path, cached.quota
