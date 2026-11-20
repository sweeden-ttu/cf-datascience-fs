"""What one caller sees: a set of mounts under a (possibly virtual) root.

A *mount* maps a client path prefix such as ``/home`` onto a real directory. A
single-user server has one mount at ``/``. A multi-user server gives every caller
``/kaggle-datasets`` (shared) and ``/home`` (their own space); ``/`` itself is then
virtual and only lists the mounts.
"""

from __future__ import annotations

import os
import stat as stat_module
import time
from dataclasses import dataclass
from http import HTTPStatus

from ._fsops import HTTPError, Quota, entry, normalize_path


@dataclass
class Mount:
    path: str
    real_root: str
    writable: bool = False
    description: str = ""
    quota: Quota | None = None

    def public(self) -> dict:
        return {"path": self.path, "writable": self.writable, "description": self.description}


class Namespace:
    def __init__(self, mounts: list[Mount]):
        if not mounts:
            raise ValueError("a namespace needs at least one mount")
        for mount in mounts:
            if mount.path != normalize_path(mount.path):
                raise ValueError(f"mount path must be normalized: {mount.path!r}")
        paths = [mount.path for mount in mounts]
        if len(set(paths)) != len(paths):
            raise ValueError("duplicate mount paths")
        if "/" in paths and len(paths) > 1:
            raise ValueError("a mount at '/' must be the only mount")
        if any(path.count("/") != 1 for path in paths if path != "/"):
            raise ValueError("mounts other than '/' must be one level deep, like '/home'")
        self.mounts = sorted(mounts, key=lambda mount: mount.path)
        self.virtual_root = paths != ["/"]

    def locate(self, client_path: str) -> tuple[Mount | None, str, str]:
        """Return ``(mount, path_inside_mount, normalized_path)``.

        ``mount`` is ``None`` for the virtual root. A path under no mount raises 404.
        """
        normalized = normalize_path(client_path)
        if not self.virtual_root:
            return self.mounts[0], normalized, normalized
        if normalized == "/":
            return None, "/", "/"
        head = "/" + normalized.split("/")[1]
        for mount in self.mounts:
            if mount.path == head:
                return mount, normalized[len(head) :] or "/", normalized
        raise HTTPError(
            HTTPStatus.NOT_FOUND, "not_found", f"no such file or directory: {normalized}"
        )

    def describe_root(self, list_entries: bool) -> dict:
        """``/v1/ls`` body for the virtual root."""
        mounts = []
        newest = 0.0
        for mount in self.mounts:
            try:
                st = os.stat(mount.real_root)
            except OSError:
                continue
            if not stat_module.S_ISDIR(st.st_mode):
                continue
            newest = max(newest, st.st_mtime)
            mounts.append(entry(mount.path, st, False, mount.writable))
        info = {
            "name": "/",
            "size": 0,
            "type": "directory",
            "mtime": newest or time.time(),
            "islink": False,
            "writable": False,
        }
        return {"info": info, "entries": mounts if list_entries else None}
