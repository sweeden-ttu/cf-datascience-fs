"""Read-only fsspec filesystem for a remote storage drive served over HTTP.

Client side (data scientists)::

    import fsspec

    fs = fsspec.filesystem("cfds", host="drive-host", port=8765)
    fs.ls("/")
    with fsspec.open("cfds://drive-host:8765/datasets/train.csv") as f:
        ...

Server side (the machine that has the drive)::

    cfds-server --root /mnt/TMP --host 0.0.0.0 --port 8765
"""

from ._version import __version__

__all__ = [
    "DEFAULT_PORT",
    "CFDataScienceFile",
    "CFDataScienceFileSystem",
    "FileChangedError",
    "__version__",
]

_CLIENT_NAMES = frozenset(__all__) - {"__version__"}


def __getattr__(name):
    # The client is imported on first use, so that ``cf_datascience_fs.server`` (which
    # needs only the standard library) can run on a machine without fsspec or requests.
    if name in _CLIENT_NAMES:
        from . import core

        return getattr(core, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(__all__)
