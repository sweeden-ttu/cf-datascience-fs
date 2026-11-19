from __future__ import annotations

import hashlib
import os
import threading

import pytest

from cf_datascience_fs import CFDataScienceFileSystem
from cf_datascience_fs.server import make_server

BIG_SIZE = 5 * 1024 * 1024 + 123


@pytest.fixture(autouse=True)
def _direct_loopback(monkeypatch):
    """Never send test traffic through an HTTP proxy configured in the environment."""
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost,::1")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost,::1")
    monkeypatch.delenv("CFDS_TOKEN", raising=False)


def _deterministic_bytes(size: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < size:
        out += hashlib.sha256(str(counter).encode()).digest()
        counter += 1
    return bytes(out[:size])


@pytest.fixture(scope="session")
def big_bytes() -> bytes:
    return _deterministic_bytes(BIG_SIZE)


@pytest.fixture
def drive(tmp_path, big_bytes):
    """A small directory tree standing in for /mnt/TMP."""
    root = tmp_path / "drive"
    (root / "datasets" / "nested").mkdir(parents=True)
    (root / "empty_dir").mkdir()
    (root / "hello.txt").write_bytes(b"hello world\n")
    (root / "empty.bin").write_bytes(b"")
    (root / "datasets" / "train.csv").write_text("id,value\n1,10\n2,20\n3,30\n")
    (root / "datasets" / "nested" / "deep.txt").write_bytes(b"0123456789")
    (root / "datasets" / "big.bin").write_bytes(big_bytes)
    return root


def start_server(root, **kwargs):
    server = make_server(root, host="127.0.0.1", port=0, **kwargs)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.daemon = True
    thread.start()
    return server, thread


def stop_server(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.fixture
def server(drive):
    server, thread = start_server(drive)
    yield server
    stop_server(server, thread)


@pytest.fixture
def fs(server):
    return CFDataScienceFileSystem(
        host="127.0.0.1", port=server.port, retries=0, skip_instance_cache=True
    )


@pytest.fixture
def outside(tmp_path):
    """A file that lives next to, not inside, the served root."""
    secret = tmp_path / "outside" / "secret.txt"
    secret.parent.mkdir()
    secret.write_text("top secret")
    return secret


def can_symlink(tmp_path) -> bool:
    try:
        os.symlink(tmp_path, tmp_path / "__probe__")
    except (OSError, NotImplementedError):
        return False
    os.unlink(tmp_path / "__probe__")
    return True
