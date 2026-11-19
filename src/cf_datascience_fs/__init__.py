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
from .core import DEFAULT_PORT, CFDataScienceFile, CFDataScienceFileSystem, FileChangedError

__all__ = [
    "DEFAULT_PORT",
    "CFDataScienceFile",
    "CFDataScienceFileSystem",
    "FileChangedError",
    "__version__",
]
