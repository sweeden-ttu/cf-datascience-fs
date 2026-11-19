"""Tests for the fsspec client, run against a real server on a temporary directory."""

from __future__ import annotations

import errno
import hashlib
import io
import os
import pickle
from concurrent.futures import ThreadPoolExecutor

import fsspec
import pytest
from conftest import bump_mtime, can_symlink, start_server, stop_server
from fsspec.callbacks import Callback

from cf_datascience_fs import CFDataScienceFile, CFDataScienceFileSystem, FileChangedError

# -- registration and URL handling ----------------------------------------------------


@pytest.mark.parametrize("protocol", ["cfds", "cfdss"])
def test_registered_with_fsspec(protocol):
    # Resolved through the "fsspec.specs" entry point declared in pyproject.toml.
    assert fsspec.get_filesystem_class(protocol) is CFDataScienceFileSystem


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("cfds://host:8765/a/b.csv", "/a/b.csv"),
        ("cfdss://host/a/b.csv", "/a/b.csv"),
        ("cfds://host:8765", "/"),
        ("cfds://host:8765/", "/"),
        ("cfds://host/dir/", "/dir"),
        ("/a/b", "/a/b"),
        ("a/b", "/a/b"),
        ("/", "/"),
        ("", "/"),
        ("cfds://host/odd?name#frag.csv", "/odd?name#frag.csv"),
    ],
)
def test_strip_protocol(raw, expected):
    assert CFDataScienceFileSystem._strip_protocol(raw) == expected


def test_strip_protocol_list():
    assert CFDataScienceFileSystem._strip_protocol(["cfds://h/a", "b/"]) == ["/a", "/b"]


@pytest.mark.parametrize(
    "url, expected",
    [
        ("cfds://drive.local:9000/a", {"host": "drive.local", "port": 9000}),
        ("cfds://drive.local/a", {"host": "drive.local"}),
        ("cfdss://drive.local:443/a", {"host": "drive.local", "port": 443, "use_ssl": True}),
        ("cfds://[::1]:9000/a", {"host": "::1", "port": 9000}),
        ("cfds:///a", {}),
        ("/a/b", {}),
    ],
)
def test_get_kwargs_from_urls(url, expected):
    assert CFDataScienceFileSystem._get_kwargs_from_urls(url) == expected


def test_base_url_and_unstrip_protocol():
    plain = CFDataScienceFileSystem(host="drive", port=9000, skip_instance_cache=True)
    assert plain.base_url == "http://drive:9000"
    assert plain.unstrip_protocol("/a/b") == "cfds://drive:9000/a/b"
    secure = CFDataScienceFileSystem(host="::1", use_ssl=True, skip_instance_cache=True)
    assert secure.base_url == "https://[::1]:8765"
    assert secure.unstrip_protocol("a") == "cfdss://[::1]:8765/a"


def test_url_to_fs(server):
    fs, path = fsspec.core.url_to_fs(f"cfds://127.0.0.1:{server.port}/datasets/train.csv")
    assert isinstance(fs, CFDataScienceFileSystem)
    assert (fs.host, fs.port, path) == ("127.0.0.1", server.port, "/datasets/train.csv")
    assert fs.isfile(path)


# -- browsing -------------------------------------------------------------------------


def test_ls(fs):
    assert fs.ls("/", detail=False) == ["/datasets", "/empty.bin", "/empty_dir", "/hello.txt"]
    assert fs.ls("", detail=False) == fs.ls("/", detail=False)
    assert fs.ls("/datasets/", detail=False) == [
        "/datasets/big.bin",
        "/datasets/nested",
        "/datasets/train.csv",
    ]
    assert fs.ls("/empty_dir") == []
    detailed = {entry["name"]: entry for entry in fs.ls("/", detail=True)}
    assert detailed["/hello.txt"]["type"] == "file"
    assert detailed["/hello.txt"]["size"] == 12
    assert detailed["/datasets"]["type"] == "directory"


def test_ls_on_a_file_returns_that_file(fs):
    assert fs.ls("/hello.txt", detail=False) == ["/hello.txt"]


def test_ls_missing_raises(fs):
    with pytest.raises(FileNotFoundError):
        fs.ls("/nope")


def test_info(fs):
    info = fs.info("/datasets/train.csv")
    assert info["name"] == "/datasets/train.csv"
    assert info["type"] == "file"
    assert info["size"] == len("id,value\n1,10\n2,20\n3,30\n")
    assert fs.info("/datasets")["type"] == "directory"
    assert fs.info("/")["type"] == "directory"
    with pytest.raises(FileNotFoundError):
        fs.info("/datasets/missing.csv")


def test_exists_isdir_isfile(fs):
    assert fs.exists("/hello.txt")
    assert not fs.exists("/nope")
    assert fs.isdir("/datasets")
    assert not fs.isdir("/hello.txt")
    assert fs.isfile("/hello.txt")
    assert not fs.isfile("/datasets")
    assert not fs.isfile("/nope")


def test_find_walk_glob_du(fs, big_bytes):
    assert fs.find("/datasets") == [
        "/datasets/big.bin",
        "/datasets/nested/deep.txt",
        "/datasets/train.csv",
    ]
    walked = {root: (sorted(dirs), sorted(files)) for root, dirs, files in fs.walk("/")}
    assert walked["/"] == (["datasets", "empty_dir"], ["empty.bin", "hello.txt"])
    assert walked["/datasets"] == (["nested"], ["big.bin", "train.csv"])
    assert walked["/datasets/nested"] == ([], ["deep.txt"])
    assert fs.glob("/datasets/*.csv") == ["/datasets/train.csv"]
    assert fs.glob("/**/*.txt") == ["/datasets/nested/deep.txt", "/hello.txt"]
    assert fs.du("/datasets") == len(big_bytes) + 10 + 24


def test_modified(fs, drive):
    modified = fs.modified("/hello.txt")
    assert modified.tzinfo is not None
    assert abs(modified.timestamp() - os.stat(drive / "hello.txt").st_mtime) < 1e-3


# -- reading --------------------------------------------------------------------------


def test_cat(fs, big_bytes):
    assert fs.cat("/hello.txt") == b"hello world\n"
    assert fs.cat_file("/empty.bin") == b""
    assert fs.cat_file("/datasets/big.bin") == big_bytes
    assert fs.cat(["/hello.txt", "/datasets/nested/deep.txt"]) == {
        "/hello.txt": b"hello world\n",
        "/datasets/nested/deep.txt": b"0123456789",
    }
    with pytest.raises(FileNotFoundError):
        fs.cat_file("/nope")
    with pytest.raises(IsADirectoryError):
        fs.cat_file("/datasets")


@pytest.mark.parametrize(
    "start, end",
    [
        (None, None), (0, 4), (3, None), (None, 4), (-3, None), (None, -3), (-6, -2), (2, -2),
        (4, 4), (5, 2), (8, 100), (10, None), (50, 60), (-100, None), (None, -100), (0, 0),
        (-4, 8), (-3, 1000), (None, 0),
    ],
)  # fmt: skip
def test_cat_file_ranges_match_python_slices(fs, start, end):
    data = b"0123456789"
    assert fs.cat_file("/datasets/nested/deep.txt", start=start, end=end) == data[start:end]


def test_cat_file_range_on_empty_file(fs):
    assert fs.cat_file("/empty.bin", start=0, end=10) == b""
    assert fs.cat_file("/empty.bin", start=-5) == b""


def test_cat_ranges(fs, big_bytes):
    paths = ["/datasets/big.bin", "/datasets/big.bin", "/hello.txt"]
    starts, ends = [0, 1_000_000, 6], [10, 1_000_100, 11]
    assert fs.cat_ranges(paths, starts, ends) == [
        big_bytes[0:10],
        big_bytes[1_000_000:1_000_100],
        b"world",
    ]


def test_open_read_seek_tell(fs):
    with fs.open("/datasets/nested/deep.txt", "rb") as handle:
        assert isinstance(handle, CFDataScienceFile)
        assert handle.size == 10
        assert handle.readable() and handle.seekable() and not handle.writable()
        assert handle.read(3) == b"012"
        assert handle.tell() == 3
        handle.seek(-2, os.SEEK_END)
        assert handle.read() == b"89"
        assert handle.read() == b""
        handle.seek(4)
        assert handle.read(100) == b"456789"
        handle.seek(1, os.SEEK_SET)
        handle.seek(2, os.SEEK_CUR)
        assert handle.read(1) == b"3"


def test_open_text_mode_and_iteration(fs):
    with fs.open("/datasets/train.csv", "r") as handle:
        assert handle.readline() == "id,value\n"
        assert [line.strip() for line in handle] == ["1,10", "2,20", "3,30"]


def test_open_empty_file(fs):
    with fs.open("/empty.bin", "rb") as handle:
        assert handle.read() == b""


def test_open_missing_and_directory(fs):
    with pytest.raises(FileNotFoundError):
        fs.open("/nope.bin", "rb")
    with pytest.raises(IsADirectoryError):
        fs.open("/datasets", "rb")


@pytest.mark.parametrize("block_size", [1024, 64 * 1024, 1024 * 1024, None])
@pytest.mark.parametrize("cache_type", ["readahead", "bytes", "none"])
def test_big_file_random_access(fs, big_bytes, block_size, cache_type):
    size = len(big_bytes)
    with fs.open("/datasets/big.bin", "rb", block_size=block_size, cache_type=cache_type) as handle:
        assert handle.size == size
        for offset, length in [
            (0, 10), (size - 10, 10), (size - 5, 50), (1_048_570, 20), (12_345, 70_000),
            (3_000_000, 1), (size // 2, 300_000), (7, 0),
        ]:  # fmt: skip
            handle.seek(offset)
            assert handle.read(length) == big_bytes[offset : offset + length], (offset, length)


def test_big_file_sequential_read_matches_checksum(fs, big_bytes):
    digest = hashlib.sha256()
    with fs.open("/datasets/big.bin", "rb", block_size=256 * 1024) as handle:
        while True:
            chunk = handle.read(100_000)
            if not chunk:
                break
            digest.update(chunk)
    assert digest.hexdigest() == hashlib.sha256(big_bytes).hexdigest()


def test_concurrent_reads_from_many_threads(server, big_bytes):
    def read(index):
        fs = CFDataScienceFileSystem(host="127.0.0.1", port=server.port, retries=0)
        start = index * 150_001
        return (
            fs.cat_file("/datasets/big.bin", start=start, end=start + 50_000)
            == (big_bytes[start : start + 50_000])
        )

    with ThreadPoolExecutor(max_workers=16) as pool:
        assert all(pool.map(read, range(32)))


def test_file_changed_while_open_raises(fs, drive, big_bytes):
    with fs.open("/datasets/big.bin", "rb", block_size=64 * 1024) as handle:
        assert handle.read(10) == big_bytes[:10]
        (drive / "datasets" / "big.bin").write_bytes(b"x" * len(big_bytes))
        bump_mtime(drive / "datasets" / "big.bin")
        handle.seek(2_000_000)
        with pytest.raises(FileChangedError):
            handle.read(10)


def test_special_characters_in_names(fs, drive):
    names = [
        "with space.txt", "per%cent.txt", "plus+sign.txt", "amp&ersand=x.txt", "ques?tion.txt",
        "ha#sh.txt", "unicodé-データ.txt", "semi;colon.txt", "quote'\".txt", "[brackets].txt",
    ]  # fmt: skip
    special = drive / "special dir"
    special.mkdir()
    for index, name in enumerate(names):
        (special / name).write_bytes(f"content {index}".encode())
    listed = fs.ls("/special dir", detail=False)
    assert sorted(listed) == sorted(f"/special dir/{name}" for name in names)
    for index, name in enumerate(names):
        path = f"/special dir/{name}"
        assert fs.cat_file(path) == f"content {index}".encode(), name
        assert fs.info(path)["name"] == path
        with fs.open(path, "rb") as handle:
            assert handle.read() == f"content {index}".encode()
        if "[" in name:
            continue  # older fsspec releases treat "[" in a URL as a glob pattern
        url = f"cfds://127.0.0.1:{fs.port}{path}"
        with fsspec.open(url, "rb", retries=0) as handle:
            assert handle.read() == f"content {index}".encode(), url


# -- downloading ----------------------------------------------------------------------


class _Recorder(Callback):
    def __init__(self):
        super().__init__()
        self.sizes, self.updates = [], []

    def set_size(self, size):
        self.sizes.append(size)
        super().set_size(size)

    def relative_update(self, inc=1):
        self.updates.append(inc)
        super().relative_update(inc)


def test_get_file(fs, tmp_path, big_bytes):
    target = tmp_path / "out" / "nested" / "big.bin"
    recorder = _Recorder()
    fs.get_file("/datasets/big.bin", str(target), callback=recorder)
    assert target.read_bytes() == big_bytes
    assert recorder.sizes == [len(big_bytes)]
    assert sum(recorder.updates) == len(big_bytes)


def test_get_file_into_file_object(fs):
    buffer = io.BytesIO()
    fs.get_file("/hello.txt", buffer)
    assert buffer.getvalue() == b"hello world\n"


def test_get_recursive(fs, tmp_path, big_bytes):
    target = tmp_path / "copy"
    fs.get("/datasets", str(target), recursive=True)
    assert (target / "train.csv").read_text().startswith("id,value")
    assert (target / "nested" / "deep.txt").read_bytes() == b"0123456789"
    assert (target / "big.bin").read_bytes() == big_bytes


def test_get_missing_raises(fs, tmp_path):
    with pytest.raises(FileNotFoundError):
        fs.get_file("/nope", str(tmp_path / "nope"))
    assert not (tmp_path / "nope").exists()


# -- read-only ------------------------------------------------------------------------


def test_every_write_operation_is_refused(fs, drive, tmp_path):
    local = tmp_path / "local.txt"
    local.write_text("local")
    operations = [
        lambda: fs.open("/new.txt", "wb"),
        lambda: fs.open("/hello.txt", "ab"),
        lambda: fs.open("/hello.txt", "w"),
        lambda: fs.open("/hello.txt", "r+b"),
        lambda: fs.pipe_file("/new.txt", b"data"),
        lambda: fs.pipe("/new.txt", b"data"),
        lambda: fs.touch("/new.txt"),
        lambda: fs.mkdir("/newdir"),
        lambda: fs.makedirs("/newdir/sub", exist_ok=True),
        lambda: fs.rmdir("/empty_dir"),
        lambda: fs.rm("/hello.txt"),
        lambda: fs.rm("/datasets", recursive=True),
        lambda: fs.rm_file("/hello.txt"),
        lambda: fs.mv("/hello.txt", "/moved.txt"),
        lambda: fs.cp_file("/hello.txt", "/copy.txt"),
        lambda: fs.copy("/hello.txt", "/copy.txt"),
        lambda: fs.put_file(str(local), "/uploaded.txt"),
        lambda: fs.put(str(local), "/uploaded.txt"),
        lambda: fs.write_bytes("/new.txt", b"data"),
        lambda: fs.write_text("/new.txt", "data"),
    ]
    before = sorted(str(path.relative_to(drive)) for path in drive.rglob("*"))
    for operation in operations:
        with pytest.raises(OSError) as excinfo:
            operation()
        assert excinfo.value.errno == errno.EROFS
    assert sorted(str(path.relative_to(drive)) for path in drive.rglob("*")) == before
    assert (drive / "hello.txt").read_bytes() == b"hello world\n"


# -- errors, auth, caching, pickling --------------------------------------------------


def test_server_unreachable_raises_connection_error(server):
    port = server.port
    fs = CFDataScienceFileSystem(
        host="127.0.0.1", port=port, retries=0, timeout=5, skip_instance_cache=True
    )
    assert fs.exists("/hello.txt")
    server.shutdown()
    server.server_close()
    fresh = CFDataScienceFileSystem(
        host="127.0.0.1", port=port, retries=0, timeout=5, skip_instance_cache=True
    )
    with pytest.raises(ConnectionError):
        fresh.ls("/")
    with pytest.raises(ConnectionError):
        fresh.cat_file("/hello.txt")
    # "Cannot reach the server" must never be reported as "the file is not there".
    for probe in (fresh.exists, fresh.isfile, fresh.isdir, fresh.find, fresh.du):
        with pytest.raises(ConnectionError):
            probe("/hello.txt")
    with pytest.raises(ConnectionError):
        fresh.glob("/datasets/*.csv")


def test_token_auth(drive, monkeypatch):
    server, thread = start_server(drive, token="s3cret-token")
    try:
        common = {"host": "127.0.0.1", "port": server.port, "retries": 0}
        anonymous = CFDataScienceFileSystem(**common, skip_instance_cache=True)
        with pytest.raises(PermissionError):
            anonymous.ls("/")
        with pytest.raises(PermissionError):
            anonymous.cat_file("/hello.txt")
        wrong = CFDataScienceFileSystem(**common, token="nope", skip_instance_cache=True)
        with pytest.raises(PermissionError):
            wrong.info("/hello.txt")
        for probe in (wrong.exists, wrong.isfile, wrong.isdir, wrong.find, wrong.du):
            with pytest.raises(PermissionError):
                probe("/")
        with pytest.raises(PermissionError):
            wrong.glob("/datasets/*.csv")
        with pytest.raises(PermissionError):
            list(wrong.walk("/"))
        assert list(wrong.walk("/", on_error="omit")) == []
        right = CFDataScienceFileSystem(**common, token="s3cret-token", skip_instance_cache=True)
        assert right.cat_file("/hello.txt") == b"hello world\n"

        monkeypatch.setenv("CFDS_TOKEN", "s3cret-token")
        from_env = CFDataScienceFileSystem(**common, skip_instance_cache=True)
        assert from_env.cat_file("/hello.txt") == b"hello world\n"
    finally:
        stop_server(server, thread)


def test_not_a_cfds_server_gives_a_clear_error(tmp_path):
    """Pointing the client at some other HTTP server must not look like a missing file."""
    import http.server
    import threading

    class Plain(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = b"<html>not found</html>"
            self.send_response(404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    other = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Plain)
    thread = threading.Thread(target=other.serve_forever, daemon=True)
    thread.start()
    try:
        fs = CFDataScienceFileSystem(
            host="127.0.0.1", port=other.server_address[1], retries=0, skip_instance_cache=True
        )
        with pytest.raises(OSError) as excinfo:
            fs.info("/hello.txt")
        assert not isinstance(excinfo.value, FileNotFoundError)
    finally:
        other.shutdown()
        other.server_close()


def test_listings_are_fresh_by_default(fs, drive):
    assert "/new.txt" not in fs.ls("/", detail=False)
    (drive / "new.txt").write_text("new")
    assert "/new.txt" in fs.ls("/", detail=False)
    assert fs.exists("/new.txt")


def test_listings_cache_is_opt_in(server, drive):
    fs = CFDataScienceFileSystem(
        host="127.0.0.1", port=server.port, retries=0, use_listings_cache=True,
        skip_instance_cache=True,
    )  # fmt: skip
    before = fs.ls("/", detail=False)
    (drive / "new.txt").write_text("new")
    assert fs.ls("/", detail=False) == before
    assert "/new.txt" in fs.ls("/", detail=False, refresh=True)
    (drive / "newer.txt").write_text("newer")
    assert "/newer.txt" not in fs.ls("/", detail=False)
    fs.invalidate_cache()
    assert "/newer.txt" in fs.ls("/", detail=False)
    # Mutating a returned listing must not corrupt the cache.
    fs.ls("/")[0]["name"] = "mutated"
    assert "mutated" not in fs.ls("/", detail=False)


def test_pickle_round_trip(fs):
    assert fs.cat_file("/hello.txt") == b"hello world\n"  # creates the HTTP session
    clone = pickle.loads(pickle.dumps(fs))
    assert isinstance(clone, CFDataScienceFileSystem)
    assert (clone.host, clone.port) == (fs.host, fs.port)
    assert clone.cat_file("/hello.txt") == b"hello world\n"


def test_instances_are_cached_per_server(server):
    first = CFDataScienceFileSystem(host="127.0.0.1", port=server.port)
    second = CFDataScienceFileSystem(host="127.0.0.1", port=server.port)
    other = CFDataScienceFileSystem(host="127.0.0.1", port=server.port + 1)
    assert first is second
    assert first is not other


# -- the way data scientists will actually use it -------------------------------------


def test_fsspec_open_with_url(server):
    url = f"cfds://127.0.0.1:{server.port}/datasets/train.csv"
    with fsspec.open(url, "rt") as handle:
        assert handle.read().splitlines()[0] == "id,value"


def test_fsspec_open_files_with_glob(server):
    files = fsspec.open_files(f"cfds://127.0.0.1:{server.port}/datasets/**/*.txt", "rb")
    assert [file.path for file in files] == ["/datasets/nested/deep.txt"]
    with files[0] as handle:
        assert handle.read() == b"0123456789"


def test_simplecache_chaining(server, tmp_path):
    url = f"simplecache::cfds://127.0.0.1:{server.port}/datasets/train.csv"
    options = {"simplecache": {"cache_storage": str(tmp_path / "cache")}}
    with fsspec.open(url, "rb", **options) as handle:
        assert handle.read().startswith(b"id,value")
    assert len(os.listdir(tmp_path / "cache")) == 1


def test_pandas_read_csv(server):
    pd = pytest.importorskip("pandas")
    frame = pd.read_csv(f"cfds://127.0.0.1:{server.port}/datasets/train.csv")
    assert list(frame.columns) == ["id", "value"]
    assert frame["value"].tolist() == [10, 20, 30]


def test_pandas_read_parquet(server, drive):
    pd = pytest.importorskip("pandas")
    pytest.importorskip("pyarrow")
    expected = pd.DataFrame({"id": range(5000), "value": [i * 0.5 for i in range(5000)]})
    expected.to_parquet(drive / "datasets" / "table.parquet")
    url = f"cfds://127.0.0.1:{server.port}/datasets/table.parquet"
    pd.testing.assert_frame_equal(pd.read_parquet(url), expected)
    only_id = pd.read_parquet(url, columns=["id"])
    assert only_id["id"].tolist() == list(range(5000))


def test_pyarrow_dataset_over_fsspec(fs, drive):
    pa = pytest.importorskip("pyarrow")
    ds = pytest.importorskip("pyarrow.dataset")
    pq = pytest.importorskip("pyarrow.parquet")
    (drive / "tables").mkdir()
    for part in range(3):
        table = pa.table({"part": [part] * 10, "n": list(range(10))})
        pq.write_table(table, drive / "tables" / f"part-{part}.parquet")
    dataset = ds.dataset("/tables", filesystem=fs, format="parquet")
    assert dataset.count_rows() == 30
    assert sorted(set(dataset.to_table()["part"].to_pylist())) == [0, 1, 2]


# -- regression tests from review ----------------------------------------------------


def test_env_token_is_read_at_request_time(drive, monkeypatch):
    """Setting $CFDS_TOKEN after a first failed attempt must work without a restart."""
    server, thread = start_server(drive, token="s3cret-token")
    try:
        url = f"cfds://127.0.0.1:{server.port}/hello.txt"
        with pytest.raises(PermissionError):
            fsspec.open(url, "rb", retries=0).open()
        monkeypatch.setenv("CFDS_TOKEN", "s3cret-token")
        with fsspec.open(url, "rb", retries=0) as handle:  # same cached instance as above
            assert handle.read() == b"hello world\n"
        monkeypatch.setenv("CFDS_TOKEN", "rotated-away")
        with pytest.raises(PermissionError):
            fsspec.open(url, "rb", retries=0).open()
    finally:
        stop_server(server, thread)


def test_invalid_token_is_rejected_up_front():
    for bad in ("pässwörd", "two words", ""):
        with pytest.raises(ValueError):
            CFDataScienceFileSystem(host="h", token=bad, skip_instance_cache=True)


def test_netrc_does_not_replace_the_token(drive, tmp_path, monkeypatch):
    netrc = tmp_path / "netrc"
    netrc.write_text("default login someone password hunter2\n")
    monkeypatch.setenv("NETRC", str(netrc))
    seen = []
    server, thread = start_server(drive, token="s3cret-token")
    original = server.RequestHandlerClass._authorized

    def spy(handler):
        seen.append(handler.headers.get("Authorization"))
        return original(handler)

    monkeypatch.setattr(server.RequestHandlerClass, "_authorized", spy)
    try:
        common = {"host": "127.0.0.1", "port": server.port, "retries": 0}
        with_token = CFDataScienceFileSystem(
            **common, token="s3cret-token", skip_instance_cache=True
        )
        assert with_token.cat_file("/hello.txt") == b"hello world\n"
        without = CFDataScienceFileSystem(**common, skip_instance_cache=True)
        with pytest.raises(PermissionError):
            without.cat_file("/hello.txt")
        assert seen == ["Bearer s3cret-token", None]  # the netrc password never left
    finally:
        stop_server(server, thread)


def test_filesystem_block_size_is_used_by_open(server):
    common = {"host": "127.0.0.1", "port": server.port, "retries": 0, "skip_instance_cache": True}
    tuned = CFDataScienceFileSystem(block_size=4096, **common)
    with tuned.open("/datasets/big.bin") as handle:
        assert handle.blocksize == 4096
    with tuned.open("/datasets/big.bin", block_size=8192) as handle:
        assert handle.blocksize == 8192
    default = CFDataScienceFileSystem(**common)
    with default.open("/datasets/big.bin") as handle:
        assert handle.blocksize == 5 * 2**20
    with fsspec.open(
        f"cfds://127.0.0.1:{server.port}/datasets/big.bin", "rb", block_size=2048, retries=0
    ) as handle:
        assert handle.blocksize == 2048


def test_open_file_can_be_pickled(fs, big_bytes):
    with fs.open("/datasets/big.bin", "rb") as handle:
        handle.seek(1000)
        clone = pickle.loads(pickle.dumps(handle))
        try:
            assert clone.tell() == 1000
            assert clone.read(50) == big_bytes[1000:1050]
        finally:
            clone.close()


def test_open_accepts_size_keyword(fs):
    with fs.open("/hello.txt", "rb", size=12) as handle:
        assert handle.read() == b"hello world\n"
    # A wrong size from the caller does not override what the server reports.
    with fs.open("/hello.txt", "rb", size=3) as handle:
        assert handle.read() == b"hello world\n"


def test_blockcache_chaining(server, tmp_path, big_bytes):
    url = f"blockcache::cfds://127.0.0.1:{server.port}/datasets/big.bin"
    options = {
        "blockcache": {"cache_storage": str(tmp_path / "blocks")},
        "cfds": {"retries": 0},
    }
    with fsspec.open(url, "rb", **options) as handle:
        handle.seek(3_000_000)
        assert handle.read(100) == big_bytes[3_000_000:3_000_100]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_open_refuses_special_files(fs, drive):
    os.mkfifo(drive / "pipe")
    assert fs.info("/pipe")["type"] == "other"
    with pytest.raises(PermissionError):
        fs.open("/pipe", "rb")
    with pytest.raises(PermissionError):
        fs.cat_file("/pipe")


def test_symlink_cycle_does_not_recurse_forever(fs, drive, tmp_path):
    if not can_symlink(tmp_path):
        pytest.skip("symlinks are not available")
    os.symlink(drive, drive / "datasets" / "loop")  # points back up the tree
    os.symlink(drive / "datasets" / "nested", drive / "shortcut")

    listed = {entry["name"]: entry for entry in fs.ls("/datasets")}
    assert listed["/datasets/loop"]["type"] == "directory"
    assert listed["/datasets/loop"]["islink"] is True

    expected = [
        "/datasets/big.bin",
        "/datasets/nested/deep.txt",
        "/datasets/train.csv",
        "/empty.bin",
        "/hello.txt",
    ]
    assert fs.find("/") == expected
    assert fs.glob("/**/*.txt") == ["/datasets/nested/deep.txt", "/hello.txt"]
    assert fs.du("/") > 0
    for topdown in (True, False):
        walked = {root: sorted(dirs) for root, dirs, _ in fs.walk("/", topdown=topdown)}
        assert sorted(walked) == ["/", "/datasets", "/datasets/nested", "/empty_dir"]
        assert walked["/"] == ["datasets", "empty_dir", "shortcut"]  # reported, not entered
        assert walked["/datasets"] == ["loop", "nested"]
    # A linked directory can still be used when it is named explicitly.
    assert fs.ls("/shortcut", detail=False) == ["/shortcut/deep.txt"]
    assert fs.find("/shortcut") == ["/shortcut/deep.txt"]
    assert fs.cat_file("/datasets/loop/hello.txt") == b"hello world\n"


def test_walk_maxdepth_and_pruning(fs):
    assert [root for root, _, _ in fs.walk("/", maxdepth=1)] == ["/"]
    assert sorted(root for root, _, _ in fs.walk("/", maxdepth=2)) == [
        "/",
        "/datasets",
        "/empty_dir",
    ]
    with pytest.raises(ValueError):
        list(fs.walk("/", maxdepth=0))
    visited = []
    for root, dirs, _ in fs.walk("/"):
        visited.append(root)
        if "datasets" in dirs:
            dirs.remove("datasets")  # pruning in place, as with os.walk
    assert visited == ["/", "/empty_dir"]
    detailed = dict((root, (dirs, files)) for root, dirs, files in fs.walk("/", detail=True))
    assert detailed["/"][1]["hello.txt"]["size"] == 12
    assert detailed["/"][0]["datasets"]["type"] == "directory"
    assert list(fs.walk("/missing")) == []


def make_sparse(path, size):
    with open(path, "wb") as handle:
        handle.truncate(size)


HUGE = 256 * 1024 * 1024  # far more than socket buffers hold, so a change lands mid-transfer


class _ModifyOnFirstChunk(Callback):
    """Rewrites the remote file as soon as the first bytes of it have arrived."""

    def __init__(self, target, action):
        super().__init__()
        self.target, self.action, self.done = target, action, False

    def relative_update(self, inc=1):
        if not self.done:
            self.done = True
            self.action(self.target)
        super().relative_update(inc)


def rewrite_in_place(target):
    with open(target, "r+b") as handle:
        handle.write(b"changed")
    bump_mtime(target)


@pytest.mark.parametrize("action", [rewrite_in_place, lambda target: os.truncate(target, 10)])
def test_get_file_detects_change_and_keeps_the_old_local_copy(fs, drive, tmp_path, action):
    make_sparse(drive / "huge.bin", HUGE)
    local = tmp_path / "downloads" / "huge.bin"
    local.parent.mkdir()
    local.write_bytes(b"previous good copy")
    with pytest.raises(FileChangedError):
        fs.get_file(
            "/huge.bin", str(local), callback=_ModifyOnFirstChunk(drive / "huge.bin", action)
        )
    assert local.read_bytes() == b"previous good copy"
    assert os.listdir(local.parent) == ["huge.bin"]  # no partial file left behind


def test_get_file_failure_leaves_no_new_file(fs, drive, tmp_path):
    make_sparse(drive / "huge.bin", HUGE)
    local = tmp_path / "downloads" / "huge.bin"
    with pytest.raises(FileChangedError):
        fs.get_file(
            "/huge.bin",
            str(local),
            callback=_ModifyOnFirstChunk(drive / "huge.bin", rewrite_in_place),
        )
    assert os.listdir(local.parent) == []


def test_removed_file_during_download_is_reported_as_changed(fs, drive, tmp_path):
    make_sparse(drive / "huge.bin", HUGE)

    def replace(target):
        os.truncate(target, 0)
        os.unlink(target)

    with pytest.raises(FileChangedError):
        fs.get_file(
            "/huge.bin",
            str(tmp_path / "out.bin"),
            callback=_ModifyOnFirstChunk(drive / "huge.bin", replace),
        )
    assert not (tmp_path / "out.bin").exists()


class _RewriteAfterFirstRead:
    """Wraps the server's open file and rewrites it on disk right after the first read."""

    def __init__(self, raw, target, state):
        self._raw, self._target, self._state = raw, target, state

    def read(self, size=-1):
        data = self._raw.read(size)
        if not self._state["done"]:
            self._state["done"] = True
            rewrite_in_place(self._target)
        return data

    def __getattr__(self, name):
        return getattr(self._raw, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self._raw.close()


@pytest.mark.parametrize(
    "read",
    [
        lambda fs, path: fs.cat_file(path),
        lambda fs, path: fs.cat_file(path, start=0, end=3 * 2**20),
        lambda fs, path: fs.open(path, "rb").read(),
    ],
    ids=["cat_file", "cat_file_range", "open_read"],
)
def test_single_request_reads_detect_change(fs, drive, monkeypatch, read):
    """cat_file and open().read() fetch in one request; a rewrite mid-way must not go unnoticed."""
    from cf_datascience_fs import server as server_module

    target = drive / "datasets" / "mid.bin"
    target.write_bytes(b"a" * (4 * server_module.CHUNK_SIZE))
    state = {"done": False}
    real_open = server_module.open_regular_file

    def open_regular_file(root, path):
        handle, st = real_open(root, path)
        return _RewriteAfterFirstRead(handle, target, state), st

    monkeypatch.setattr(server_module, "open_regular_file", open_regular_file)
    with pytest.raises(FileChangedError):
        read(fs, "/datasets/mid.bin")
    assert state["done"]


def test_interrupted_transfer_of_unchanged_file_is_not_called_a_change(fs, drive, monkeypatch):
    """When the bytes stop but the file is unchanged, say so instead of blaming a change."""
    from cf_datascience_fs import server as server_module

    target = drive / "datasets" / "mid.bin"
    target.write_bytes(b"a" * (4 * server_module.CHUNK_SIZE))
    real_open = server_module.open_regular_file

    class Truncating:
        def __init__(self, raw):
            self._raw, self._reads = raw, 0

        def read(self, size=-1):
            self._reads += 1
            return self._raw.read(size) if self._reads == 1 else b""  # pretend EOF

        def __getattr__(self, name):
            return getattr(self._raw, name)

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            self._raw.close()

    monkeypatch.setattr(
        server_module,
        "open_regular_file",
        lambda root, path: (lambda h, st: (Truncating(h), st))(*real_open(root, path)),
    )
    with pytest.raises(OSError, match="interrupted") as excinfo:
        fs.cat_file("/datasets/mid.bin")
    assert not isinstance(excinfo.value, FileChangedError)


def test_simplecache_does_not_keep_a_partial_download(server, drive, tmp_path):
    make_sparse(drive / "huge.bin", HUGE)
    fs = CFDataScienceFileSystem(
        host="127.0.0.1", port=server.port, retries=0, skip_instance_cache=True
    )
    original = fs.get_file

    def get_file(rpath, lpath, **kwargs):
        kwargs["callback"] = _ModifyOnFirstChunk(drive / "huge.bin", rewrite_in_place)
        return original(rpath, lpath, **kwargs)

    fs.get_file = get_file
    cached = fsspec.filesystem(
        "simplecache", fs=fs, cache_storage=str(tmp_path / "cache"), skip_instance_cache=True
    )
    with pytest.raises(FileChangedError):
        cached.open("/huge.bin", "rb")
    assert [name for name in os.listdir(tmp_path / "cache") if not name.endswith(".part")] == []
    assert os.listdir(tmp_path / "cache") == []


def test_invalidate_cache_covers_the_subtree(server, drive):
    fs = CFDataScienceFileSystem(
        host="127.0.0.1", port=server.port, retries=0, use_listings_cache=True,
        skip_instance_cache=True,
    )  # fmt: skip
    for path in ("/", "/datasets", "/datasets/nested"):
        fs.ls(path)
    (drive / "datasets" / "nested" / "new.txt").write_text("new")
    assert "/datasets/nested/new.txt" not in fs.ls("/datasets/nested", detail=False)
    fs.invalidate_cache("/datasets")
    assert "/datasets/nested/new.txt" in fs.ls("/datasets/nested", detail=False)
    assert "/datasets/nested/new.txt" in fs.find("/datasets")


def test_failed_refresh_evicts_the_stale_listing(server, drive):
    fs = CFDataScienceFileSystem(
        host="127.0.0.1", port=server.port, retries=0, use_listings_cache=True,
        skip_instance_cache=True,
    )  # fmt: skip
    assert fs.ls("/empty_dir") == []
    os.rmdir(drive / "empty_dir")
    assert fs.ls("/empty_dir") == []  # still cached
    with pytest.raises(FileNotFoundError):
        fs.ls("/empty_dir", refresh=True)
    with pytest.raises(FileNotFoundError):
        fs.ls("/empty_dir")


def test_timeout_pair_survives_json_round_trip(server):
    fs = CFDataScienceFileSystem(
        host="127.0.0.1", port=server.port, retries=0, timeout=(3, 30), skip_instance_cache=True
    )
    clone = CFDataScienceFileSystem.from_json(fs.to_json())
    assert clone.timeout == (3, 30)
    assert clone.cat_file("/hello.txt") == b"hello world\n"


def test_host_not_allowed_is_a_permission_error(drive):
    """A dotted DNS name the server was not told about is refused with a useful message."""
    server, thread = start_server(drive)
    try:
        fs = CFDataScienceFileSystem(
            host="127.0.0.1", port=server.port, retries=0, skip_instance_cache=True
        )
        fs.session.headers["Host"] = "drive.corp.example"
        with pytest.raises(PermissionError, match="--allowed-host"):
            fs.ls("/")
    finally:
        stop_server(server, thread)


def test_package_exports_are_lazy_but_complete():
    import cf_datascience_fs

    assert cf_datascience_fs.CFDataScienceFileSystem is CFDataScienceFileSystem
    assert cf_datascience_fs.FileChangedError is FileChangedError
    assert cf_datascience_fs.DEFAULT_PORT == 8765
    assert set(cf_datascience_fs.__all__) <= set(dir(cf_datascience_fs))
    with pytest.raises(AttributeError):
        cf_datascience_fs.does_not_exist  # noqa: B018
