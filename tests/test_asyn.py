"""The read-write client (``CFDataScienceAsyncFileSystem``), sync and async, and ``cfds login``."""

from __future__ import annotations

import asyncio
import errno
import json
import time
import urllib.request

import fsspec
import pytest
from conftest import start_server, stop_server
from fake_oidc import ISSUER, FakeProvider, IdPHandler

from cf_datascience_fs import (
    CFDataScienceAsyncFileSystem,
    FileChangedError,
    QuotaExceededError,
    _credentials,
    cli,
)
from cf_datascience_fs._auth import OIDCAuth


@pytest.fixture(autouse=True)
def _config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CFDS_CONFIG_DIR", str(tmp_path / "config"))


@pytest.fixture
def wserver(drive):
    server, thread = start_server(drive, writable=True)
    yield server
    stop_server(server, thread)


def client(server, **kwargs):
    kwargs.setdefault("retries", 0)
    return CFDataScienceAsyncFileSystem(
        host="127.0.0.1", port=server.port, skip_instance_cache=True, **kwargs
    )


@pytest.fixture
def wfs(wserver):
    fs = client(wserver)
    yield fs
    fs.close()


# -- writing through the sync interface ---------------------------------------------------


def test_pipe_cat_and_info(wfs, drive):
    wfs.pipe_file("/x.txt", b"payload")
    assert (drive / "x.txt").read_bytes() == b"payload"
    assert wfs.cat_file("/x.txt") == b"payload"
    assert wfs.info("/x.txt")["size"] == 7
    wfs.pipe({"/a.txt": b"A", "/b.txt": b"B"})
    assert wfs.cat(["/a.txt", "/b.txt"]) == {"/a.txt": b"A", "/b.txt": b"B"}
    with pytest.raises(FileExistsError):
        wfs.pipe_file("/a.txt", b"again", mode="create")


def test_open_for_writing(wfs, drive):
    with wfs.open("/written.bin", "wb", block_size=5 * 2**20) as handle:
        assert handle.writable()
        for index in range(20):
            handle.write(bytes([index]) * 1_000_000)
        # Nothing is visible on the server until the file is closed.
        assert not (drive / "written.bin").exists()
    data = (drive / "written.bin").read_bytes()
    assert len(data) == 20_000_000 and data[-1] == 19
    with wfs.open("/written.bin", "rb") as handle:
        handle.seek(19_999_999)
        assert handle.read() == bytes([19])


def test_text_mode_and_exclusive_create(wfs, drive):
    with wfs.open("/notes.txt", "w") as handle:
        handle.write("héllo\n")
    assert (drive / "notes.txt").read_text(encoding="utf-8") == "héllo\n"
    with pytest.raises(FileExistsError):
        wfs.open("/notes.txt", "xb")
    with wfs.open("/fresh.txt", "xb") as handle:
        handle.write(b"x")
    assert (drive / "fresh.txt").read_bytes() == b"x"
    with pytest.raises(NotImplementedError):
        wfs.open("/notes.txt", "ab")


def test_put_and_get(wfs, drive, tmp_path, big_bytes):
    local = tmp_path / "local"
    (local / "sub").mkdir(parents=True)
    (local / "big.bin").write_bytes(big_bytes)
    (local / "sub" / "small.txt").write_text("small")
    wfs.put(str(local), "/uploaded", recursive=True)
    assert (drive / "uploaded" / "big.bin").read_bytes() == big_bytes
    assert (drive / "uploaded" / "sub" / "small.txt").read_text() == "small"
    out = tmp_path / "out.bin"
    wfs.get("/uploaded/big.bin", str(out))
    assert out.read_bytes() == big_bytes


def test_upload_progress_callback(wfs, tmp_path, big_bytes):
    local = tmp_path / "big.bin"
    local.write_bytes(big_bytes)
    seen = []

    class Recorder(fsspec.callbacks.Callback):
        def call(self, *args, **kwargs):
            seen.append(self.value)

    wfs.put_file(str(local), "/big-copy.bin", callback=Recorder())
    assert seen and seen[-1] == len(big_bytes)


def test_directories(wfs, drive):
    wfs.mkdir("/made/deep/er")
    assert (drive / "made" / "deep" / "er").is_dir()
    with pytest.raises(FileNotFoundError):
        wfs.mkdir("/nope/sub", create_parents=False)
    wfs.makedirs("/made/deep/er", exist_ok=True)
    with pytest.raises(FileExistsError):
        wfs.makedirs("/made/deep/er")
    wfs.rmdir("/made/deep/er")
    assert not (drive / "made" / "deep" / "er").exists()
    with pytest.raises(OSError) as excinfo:
        wfs.rmdir("/datasets")
    assert excinfo.value.errno == errno.ENOTEMPTY


def test_rm(wfs, drive):
    wfs.rm("/hello.txt")
    assert not (drive / "hello.txt").exists()
    with pytest.raises(FileNotFoundError):
        wfs.rm("/hello.txt")
    with pytest.raises(OSError):
        wfs.rm("/datasets")
    wfs.rm("/datasets/nested/*.txt")
    assert not (drive / "datasets" / "nested" / "deep.txt").exists()
    wfs.rm(["/datasets", "/datasets/train.csv"], recursive=True)
    assert not (drive / "datasets").exists()


def test_rm_file_refuses_directories(wfs):
    with pytest.raises(IsADirectoryError):
        wfs.rm_file("/datasets")


def test_copy_and_move(wfs, drive):
    wfs.cp_file("/hello.txt", "/copy.txt")
    assert (drive / "copy.txt").read_bytes() == b"hello world\n"
    wfs.mv("/copy.txt", "/moved.txt")
    assert not (drive / "copy.txt").exists()
    assert (drive / "moved.txt").read_bytes() == b"hello world\n"
    wfs.mv("/datasets", "/renamed")
    assert (drive / "renamed" / "nested" / "deep.txt").exists()
    wfs.copy("/renamed/nested", "/copied", recursive=True)
    assert (drive / "copied" / "deep.txt").read_bytes() == b"0123456789"


def test_touch(wfs, drive):
    wfs.touch("/touched")
    assert (drive / "touched").read_bytes() == b""
    with pytest.raises(NotImplementedError):
        wfs.touch("/hello.txt", truncate=False)


def test_listing_cache_is_invalidated_by_writes(wserver):
    fs = client(wserver, use_listings_cache=True)
    try:
        assert "/new.txt" not in fs.ls("/", detail=False)
        fs.pipe_file("/new.txt", b"x")
        assert "/new.txt" in fs.ls("/", detail=False)
        fs.rm("/new.txt")
        assert "/new.txt" not in fs.ls("/", detail=False)
    finally:
        fs.close()


def test_read_only_server_refuses_writes(server, drive, tmp_path):
    fs = client(server)
    local = tmp_path / "local.txt"
    local.write_text("local")
    before = sorted(str(path.relative_to(drive)) for path in drive.rglob("*"))
    for operation in (
        lambda: fs.pipe_file("/new.txt", b"data"),
        lambda: fs.touch("/new.txt"),
        lambda: fs.mkdir("/newdir"),
        lambda: fs.rm("/hello.txt"),
        lambda: fs.rm("/datasets", recursive=True),
        lambda: fs.mv("/hello.txt", "/moved.txt"),
        lambda: fs.cp_file("/hello.txt", "/copy.txt"),
        lambda: fs.put_file(str(local), "/uploaded.txt"),
        lambda: fs.write_text("/new.txt", "data"),
    ):
        with pytest.raises(PermissionError) as excinfo:
            operation()
        assert excinfo.value.errno == errno.EROFS
    assert sorted(str(path.relative_to(drive)) for path in drive.rglob("*")) == before
    fs.close()


def test_open_write_failure_surfaces_on_close(server):
    fs = client(server)
    handle = fs.open("/new.txt", "wb")
    handle.write(b"data")
    with pytest.raises(PermissionError):
        handle.close()
    fs.close()


def test_file_changed_while_reading(wfs, drive):
    with wfs.open("/datasets/big.bin", "rb", block_size=1024) as handle:
        handle.read(10)
        wfs.pipe_file("/datasets/big.bin", b"replaced")
        with pytest.raises(FileChangedError):
            handle.read(100_000)


def test_pandas_round_trip(wserver):
    pd = pytest.importorskip("pandas")
    url = f"cfds://127.0.0.1:{wserver.port}/frame.csv"
    frame = pd.DataFrame({"id": [1, 2, 3], "value": [10.5, 20.5, 30.5]})
    frame.to_csv(url, index=False, storage_options={"retries": 0})
    pd.testing.assert_frame_equal(pd.read_csv(url, storage_options={"retries": 0}), frame)


# -- async ------------------------------------------------------------------------------


def test_async_usage(wserver, drive):
    async def main():
        fs = CFDataScienceAsyncFileSystem(
            host="127.0.0.1", port=wserver.port, asynchronous=True, skip_instance_cache=True
        )
        session = await fs.set_session()
        try:
            await fs._pipe_file("/async.txt", b"from asyncio")
            names = await fs._ls("/", detail=False)
            contents = await asyncio.gather(
                fs._cat_file("/async.txt"),
                fs._cat_file("/hello.txt"),
                fs._cat_file("/datasets/big.bin", start=0, end=4),
            )
            await fs._mkdir("/adir")
            await fs._mv("/async.txt", "/adir/async.txt")
            info = await fs._info("/adir/async.txt")
            await fs._rm("/adir", recursive=True)
            return names, contents, info
        finally:
            await session.close()

    names, contents, info = asyncio.run(main())
    assert "/async.txt" in names
    assert contents[0] == b"from asyncio" and contents[1] == b"hello world\n"
    assert len(contents[2]) == 4
    assert info["size"] == 12
    assert not (drive / "adir").exists()


# -- signing in -------------------------------------------------------------------------


@pytest.fixture
def provider():
    return FakeProvider()


@pytest.fixture
def mserver(provider, tmp_path):
    datasets = tmp_path / "kaggle"
    (datasets / "titanic").mkdir(parents=True)
    (datasets / "titanic" / "train.csv").write_text("a,b\n1,2\n")
    users = tmp_path / "users"
    users.mkdir()
    auth = OIDCAuth(ISSUER, ("cfds-vscode", "cfds-ios", "cfds-cli"), fetch=provider.fetch)
    server, thread = start_server(
        None, auth=auth, users_root=users, datasets_root=datasets, default_quota=10_000
    )
    yield server
    stop_server(server, thread)


def server_url(server):
    return f"http://127.0.0.1:{server.port}"


def test_token_argument_and_me(mserver, provider):
    fs = client(mserver, token=provider.token("alice", email="a@example.test"))
    me = fs.me()
    assert me["user"]["sub"] == "alice" and me["quota"]["limit"] == 10_000
    assert fs.ls("/", detail=False) == ["/home", "/kaggle-datasets"]
    assert fs.cat_file("/kaggle-datasets/titanic/train.csv") == b"a,b\n1,2\n"
    fs.pipe_file("/home/mine.txt", b"x" * 100)
    assert fs.me()["quota"]["used"] == 100
    with pytest.raises(QuotaExceededError) as excinfo:
        fs.pipe_file("/home/huge.bin", b"x" * 20_000)
    assert excinfo.value.errno == errno.EDQUOT
    with pytest.raises(PermissionError) as excinfo:
        fs.pipe_file("/kaggle-datasets/titanic/x.csv", b"x")
    assert excinfo.value.errno == errno.EROFS
    # A move across mounts falls back to copy + delete; out of the read-only one fails.
    with pytest.raises(PermissionError):
        fs.mv("/kaggle-datasets/titanic/train.csv", "/home/train.csv")
    fs.cp_file("/kaggle-datasets/titanic/train.csv", "/home/train.csv")
    assert fs.cat_file("/home/train.csv") == b"a,b\n1,2\n"
    fs.close()


def test_no_token_is_a_permission_error_mentioning_login(mserver):
    fs = client(mserver)
    with pytest.raises(PermissionError, match="cfds login"):
        fs.ls("/")
    fs.close()


def test_environment_token(mserver, provider, monkeypatch):
    monkeypatch.setenv("CFDS_TOKEN", provider.token("bob"))
    fs = client(mserver)
    assert fs.me()["user"]["sub"] == "bob"
    fs.close()


def test_login_saves_a_token_the_filesystem_uses(mserver, provider, monkeypatch, capsys):
    idp = IdPHandler(provider).start()
    try:
        monkeypatch.setattr(cli.webbrowser, "open", lambda url: urllib.request.urlopen(url).read())
        code = cli.main(["login", server_url(mserver), "--issuer", idp.url])
        assert code == 0
        out = capsys.readouterr()
        assert "Signed in as alice" in out.out
        assert "/kaggle-datasets  (read-only)" in out.out
        record = _credentials.load(server_url(mserver))
        assert record["refresh_token"] and record["client_id"] == "cfds-cli"
        fs = client(mserver)
        assert fs.me()["user"]["sub"] == "alice"
        fs.close()
        assert cli.main(["whoami", server_url(mserver)]) == 0
        assert cli.main(["token", server_url(mserver)]) == 0
        assert capsys.readouterr().out.strip().endswith(record["access_token"])
        assert cli.main(["logout", server_url(mserver)]) == 0
        assert _credentials.load(server_url(mserver)) is None
        assert cli.main(["whoami", server_url(mserver)]) == 1
    finally:
        idp.stop()


def test_login_rejects_a_forged_callback(mserver, provider, monkeypatch):
    idp = IdPHandler(provider).start()

    def attacker(url):
        from urllib.parse import parse_qs, urlsplit

        redirect = parse_qs(urlsplit(url).query)["redirect_uri"][0]
        urllib.request.urlopen(redirect + "?code=stolen&state=wrong").read()

    try:
        monkeypatch.setattr(cli.webbrowser, "open", attacker)
        with pytest.raises(cli.LoginError, match="state"):
            cli.login(server_url(mserver), issuer=idp.url)
        assert _credentials.load(server_url(mserver)) is None
    finally:
        idp.stop()


def test_issuer_is_discovered_from_the_server(mserver):
    assert cli.discover_issuer(server_url(mserver)) == ISSUER


def test_expired_saved_token_is_refreshed(mserver, provider):
    idp = IdPHandler(provider).start()
    try:
        idp.refresh_tokens["r1"] = "carol"
        _credentials.save(
            server_url(mserver),
            {
                "access_token": provider.token("carol", lifetime=-10),
                "expires_at": time.time() - 10,
                "refresh_token": "r1",
                "token_endpoint": idp.url + "/token",
                "client_id": "cfds-cli",
                "issuer": idp.url,
            },
        )
        fs = client(mserver)
        assert fs.me()["user"]["sub"] == "carol"
        record = _credentials.load(server_url(mserver))
        assert record["expires_at"] > time.time() and record["refresh_token"] != "r1"
        fs.close()
    finally:
        idp.stop()


def test_rejected_token_is_refreshed_once(mserver, provider):
    idp = IdPHandler(provider).start()
    try:
        idp.refresh_tokens["r2"] = "dave"
        _credentials.save(
            server_url(mserver),
            {
                "access_token": "revoked.not-a.jwt",
                "expires_at": time.time() + 3600,
                "refresh_token": "r2",
                "token_endpoint": idp.url + "/token",
                "client_id": "cfds-cli",
            },
        )
        fs = client(mserver)
        assert fs.ls("/home") == []
        assert [form["grant_type"] for form in idp.token_requests] == ["refresh_token"]
        fs.close()
    finally:
        idp.stop()


def test_credentials_file_is_private(mserver, tmp_path):
    _credentials.save("https://Storage.Example:443/", {"access_token": "abc"})
    assert _credentials.load("https://storage.example") == {"access_token": "abc"}
    path = _credentials.credentials_path()
    import os
    import stat

    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert json.loads(open(path).read()) == {"https://storage.example": {"access_token": "abc"}}


def test_server_keys():
    assert _credentials.server_key("https://a.example") == "https://a.example"
    assert _credentials.server_key("https://A.example:443/x") == "https://a.example"
    assert _credentials.server_key("http://a.example:8765") == "http://a.example:8765"
    assert _credentials.server_key("http://[::1]:80") == "http://[::1]"


def test_missing_parent_directories(wserver, drive):
    fs = client(wserver)
    fs.pipe_file("/deep/new/dirs/file.txt", b"x")
    assert (drive / "deep" / "new" / "dirs" / "file.txt").read_bytes() == b"x"
    fs.cp_file("/hello.txt", "/other/place/hello.txt")
    assert (drive / "other" / "place" / "hello.txt").exists()
    strict = client(wserver, auto_mkdir=False)
    with pytest.raises(FileNotFoundError):
        strict.pipe_file("/missing/file.txt", b"x")
    assert not (drive / "missing").exists()
    fs.close()
    strict.close()
