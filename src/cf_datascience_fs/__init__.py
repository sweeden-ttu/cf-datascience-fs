"""fsspec filesystem (``cfds://``) for a remote storage drive served over HTTP.

Client side (data scientists)::

    import fsspec

    fs = fsspec.filesystem("cfdss", host="storage.cloudfronts.group")
    fs.ls("/kaggle-datasets")
    with fs.open("/home/results/model.pkl", "wb") as f:
        ...

Sign in once with ``cfds login https://storage.cloudfronts.group``.

Server side (the machine that has the drive)::

    cfds-server --root /mnt/TMP --host 0.0.0.0 --port 8765
"""

from ._version import __version__

__all__ = [
    "DEFAULT_PORT",
    "CFDataScienceAsyncFile",
    "CFDataScienceAsyncFileSystem",
    "CFDataScienceFile",
    "CFDataScienceFileSystem",
    "FileChangedError",
    "QuotaExceededError",
    "__version__",
]

_LOCATIONS = {
    "DEFAULT_PORT": "core",
    "CFDataScienceFile": "core",
    "CFDataScienceFileSystem": "core",
    "CFDataScienceAsyncFile": "asyn",
    "CFDataScienceAsyncFileSystem": "asyn",
    "FileChangedError": "_errors",
    "QuotaExceededError": "_errors",
}


def __getattr__(name):
    # The clients are imported on first use, so that ``cf_datascience_fs.server`` (which
    # needs only the standard library) can run on a machine without fsspec or aiohttp.
    if name in _LOCATIONS:
        from importlib import import_module

        return getattr(import_module(f".{_LOCATIONS[name]}", __name__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(__all__)
