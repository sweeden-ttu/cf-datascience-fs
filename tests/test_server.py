"""Tests that talk to the server with plain HTTP, independent of the fsspec client."""

from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import quote

import pytest
from conftest import bump_mtime, can_symlink, start_server, stop_server

from cf_datascience_fs import CFDataScienceFileSystem
from cf_datascience_fs import server as server_module
from cf_datascience_fs.server import (
    HTTPError,
    describe,
    is_within,
    main,
    make_server,
    open_regular_file,
    parse_range,
    resolve_path,
)


def request(server, method, target, headers=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    try:
        conn.request(method, target, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


def get(server, target, headers=None):
    return request(server, "GET", target, headers)


def file_url(path):
    return "/v1/file?path=" + quote(path)


def ls_url(path, **extra):
    url = "/v1/ls?path=" + quote(path)
    for key, value in extra.items():
        url += f"&{key}={value}"
    return url


# -- pure helpers -----------------------------------------------------------------


@pytest.mark.parametrize(
    "client_path, expected",
    [
        ("/", "/"),
        ("", "/"),
        ("a/b", "/a/b"),
        ("/a//b/", "/a/b"),
        ("/./a/./b", "/a/b"),
    ],
)
def test_resolve_path_normalizes(tmp_path, client_path, expected):
    root = os.path.realpath(tmp_path)
    normalized, real = resolve_path(root, client_path)
    assert normalized == expected
    assert is_within(real, root)


@pytest.mark.parametrize("bad", ["/..", "/a/../b", "../etc/passwd", "/a/..", "/a\x00b"])
def test_resolve_path_rejects_traversal(tmp_path, bad):
    with pytest.raises(HTTPError) as excinfo:
        resolve_path(os.path.realpath(tmp_path), bad)
    assert excinfo.value.status == 400


def test_is_within_does_not_match_sibling_prefix():
    assert is_within("/srv/data", "/srv/data")
    assert is_within("/srv/data/x", "/srv/data")
    assert not is_within("/srv/data2/x", "/srv/data")
    assert not is_within("/srv", "/srv/data")
    assert is_within("/anything", "/")


@pytest.mark.parametrize(
    "header, size, expected",
    [
        (None, 10, None),
        ("", 10, None),
        ("bytes=0-4", 10, (0, 4)),
        ("bytes=5-", 10, (5, 9)),
        ("bytes=-3", 10, (7, 9)),
        ("bytes=-30", 10, (0, 9)),
        ("bytes=2-200", 10, (2, 9)),
        ("bytes=9-9", 10, (9, 9)),
        (" bytes = 1 - 2 ", 10, (1, 2)),
        ("bytes=0-1,4-5", 10, None),  # multi-range: ignored, whole file is sent
        ("bytes=5-2", 10, None),  # invalid: ignored
        ("bytes=-", 10, None),
        ("items=0-4", 10, None),
        ("bytes=abc", 10, None),
    ],
)
def test_parse_range(header, size, expected):
    assert parse_range(header, size) == expected


@pytest.mark.parametrize(
    "header, size", [("bytes=10-", 10), ("bytes=99-100", 10), ("bytes=-0", 10), ("bytes=0-", 0)]
)
def test_parse_range_unsatisfiable(header, size):
    with pytest.raises(HTTPError) as excinfo:
        parse_range(header, size)
    assert excinfo.value.status == 416
    assert excinfo.value.headers["Content-Range"] == f"bytes */{size}"


# -- /v1/ls -----------------------------------------------------------------------


def test_ls_root(server):
    status, headers, body = get(server, "/v1/ls")
    assert status == 200
    assert headers["Content-Type"] == "application/json"
    payload = json.loads(body)
    assert payload["info"]["name"] == "/"
    assert payload["info"]["type"] == "directory"
    names = [entry["name"] for entry in payload["entries"]]
    assert names == ["/datasets", "/empty.bin", "/empty_dir", "/hello.txt"]
    by_name = {entry["name"]: entry for entry in payload["entries"]}
    assert by_name["/hello.txt"]["type"] == "file"
    assert by_name["/hello.txt"]["size"] == 12
    assert by_name["/datasets"]["type"] == "directory"
    assert isinstance(by_name["/hello.txt"]["mtime"], float)


def test_ls_file_has_no_entries(server):
    status, _, body = get(server, ls_url("/hello.txt"))
    payload = json.loads(body)
    assert status == 200
    assert payload["entries"] is None
    assert payload["info"]["size"] == 12
    assert payload["info"]["etag"]


def test_ls_list_0_skips_listing(server):
    status, _, body = get(server, ls_url("/datasets", list=0))
    payload = json.loads(body)
    assert status == 200
    assert payload["entries"] is None
    assert payload["info"] == {**payload["info"], "name": "/datasets", "type": "directory"}


def test_ls_empty_directory(server):
    _, _, body = get(server, ls_url("/empty_dir"))
    assert json.loads(body)["entries"] == []


def test_ls_missing_is_404(server):
    status, _, body = get(server, ls_url("/nope"))
    assert status == 404
    assert json.loads(body)["error"]["code"] == "not_found"


def test_ls_through_a_file_is_404(server):
    status, _, _ = get(server, ls_url("/hello.txt/child"))
    assert status == 404


def test_duplicate_path_parameter_is_rejected(server):
    status, _, _ = get(server, "/v1/ls?path=/&path=/datasets")
    assert status == 400


# -- /v1/file ---------------------------------------------------------------------


def test_file_whole(server):
    status, headers, body = get(server, file_url("/hello.txt"))
    assert status == 200
    assert body == b"hello world\n"
    assert headers["Content-Length"] == "12"
    assert headers["Accept-Ranges"] == "bytes"
    assert headers["Content-Type"] == "application/octet-stream"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["ETag"].startswith('"')
    assert "Last-Modified" in headers


def test_file_requires_path(server):
    status, _, _ = get(server, "/v1/file")
    assert status == 400


@pytest.mark.parametrize(
    "range_header, expected_body, content_range",
    [
        ("bytes=0-4", b"hello", "bytes 0-4/12"),
        ("bytes=6-", b"world\n", "bytes 6-11/12"),
        ("bytes=-6", b"world\n", "bytes 6-11/12"),
        ("bytes=6-1000", b"world\n", "bytes 6-11/12"),
    ],
)
def test_file_range(server, range_header, expected_body, content_range):
    status, headers, body = get(server, file_url("/hello.txt"), {"Range": range_header})
    assert status == 206
    assert body == expected_body
    assert headers["Content-Range"] == content_range
    assert headers["Content-Length"] == str(len(expected_body))


def test_file_range_past_end_is_416(server):
    status, headers, body = get(server, file_url("/hello.txt"), {"Range": "bytes=12-"})
    assert status == 416
    assert headers["Content-Range"] == "bytes */12"
    assert json.loads(body)["error"]["code"] == "range_not_satisfiable"


def test_file_multi_range_falls_back_to_whole_file(server):
    status, _, body = get(server, file_url("/hello.txt"), {"Range": "bytes=0-1,3-4"})
    assert status == 200
    assert body == b"hello world\n"


def test_file_empty(server):
    status, headers, body = get(server, file_url("/empty.bin"))
    assert (status, body, headers["Content-Length"]) == (200, b"", "0")


def raw_exchange(server, payload: bytes, timeout=10) -> bytes:
    """Send bytes on a fresh connection and return everything the server sends until EOF."""
    with socket.create_connection(("127.0.0.1", server.port), timeout=timeout) as sock:
        sock.sendall(payload)
        chunks = []
        while True:
            try:
                chunk = sock.recv(65536)
            except (ConnectionResetError, socket.timeout):
                break
            if not chunk:
                break
            chunks.append(chunk)
    return b"".join(chunks)


def test_file_head(server):
    status, headers, _ = request(server, "HEAD", file_url("/hello.txt"))
    assert status == 200
    assert headers["Content-Length"] == "12"
    # http.client never reads a HEAD body, so look at the wire to be sure none is sent.
    raw = raw_exchange(
        server,
        f"HEAD {file_url('/hello.txt')} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        "Connection: close\r\n\r\n".encode(),
    )
    head, separator, body = raw.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 200")
    assert (separator, body) == (b"\r\n\r\n", b"")
    # Errors on HEAD have no body either.
    raw = raw_exchange(
        server, b"HEAD /v1/file?path=/nope HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
    )
    head, _, body = raw.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 404")
    assert body == b""


def test_file_on_directory_is_400(server):
    status, _, body = get(server, file_url("/datasets"))
    assert status == 400
    assert json.loads(body)["error"]["code"] == "is_a_directory"


def test_file_missing_is_404(server):
    status, _, _ = get(server, file_url("/datasets/missing.csv"))
    assert status == 404


def test_file_if_match(server, drive):
    _, headers, _ = get(server, file_url("/hello.txt"))
    etag = headers["ETag"]
    status, _, body = get(server, file_url("/hello.txt"), {"If-Match": etag})
    assert (status, body) == (200, b"hello world\n")
    status, _, _ = get(server, file_url("/hello.txt"), {"If-Match": "*"})
    assert status == 200

    (drive / "hello.txt").write_bytes(b"something else entirely\n")
    bump_mtime(drive / "hello.txt")
    status, headers, body = get(server, file_url("/hello.txt"), {"If-Match": etag})
    assert status == 412
    assert json.loads(body)["error"]["code"] == "precondition_failed"
    assert headers["ETag"] != etag


def test_big_file_round_trip(server, big_bytes):
    status, headers, body = get(server, file_url("/datasets/big.bin"))
    assert status == 200
    assert int(headers["Content-Length"]) == len(big_bytes)
    assert body == big_bytes


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_fifo_is_listed_as_other_and_not_served(server, drive):
    os.mkfifo(drive / "pipe")
    _, _, body = get(server, ls_url("/"))
    by_name = {entry["name"]: entry for entry in json.loads(body)["entries"]}
    assert by_name["/pipe"]["type"] == "other"
    status, _, _ = get(server, file_url("/pipe"))
    assert status == 403


# -- confinement to the root --------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["/../outside/secret.txt", "/datasets/../../outside/secret.txt", "..", "/a/%2e%2e/b"]
)
def test_dotdot_is_rejected(server, outside, path):
    for endpoint in ("ls", "file"):
        status, _, body = get(server, f"/v1/{endpoint}?path={path}")
        assert status == 400, (endpoint, path)
        assert b"top secret" not in body


def test_absolute_looking_path_stays_inside_root(server, outside):
    status, _, body = get(server, file_url(str(outside)))
    assert status == 404
    assert b"top secret" not in body


def test_symlinks(server, drive, outside, tmp_path):
    if not can_symlink(tmp_path):
        pytest.skip("symlinks are not available")
    os.symlink(outside, drive / "leak.txt")
    os.symlink(outside.parent, drive / "leak_dir")
    os.symlink(drive / "does-not-exist", drive / "broken")
    os.symlink(drive / "hello.txt", drive / "alias.txt")
    os.symlink(drive / "datasets", drive / "alias_dir")

    # Links that leave the root are neither readable nor listable...
    for target in (file_url("/leak.txt"), ls_url("/leak.txt"), ls_url("/leak_dir")):
        status, _, body = get(server, target)
        assert status == 403, target
        assert b"top secret" not in body
    status, _, body = get(server, file_url("/leak_dir/secret.txt"))
    assert status == 403
    assert b"top secret" not in body
    status, _, _ = get(server, ls_url("/broken"))
    assert status == 404

    # ...and do not show up in listings.
    _, _, body = get(server, ls_url("/"))
    by_name = {entry["name"]: entry for entry in json.loads(body)["entries"]}
    assert "/leak.txt" not in by_name
    assert "/leak_dir" not in by_name
    assert "/broken" not in by_name

    # Links that stay inside the root behave like what they point to.
    assert by_name["/alias.txt"]["type"] == "file"
    assert by_name["/alias.txt"]["islink"] is True
    assert by_name["/alias_dir"]["type"] == "directory"
    status, _, body = get(server, file_url("/alias.txt"))
    assert (status, body) == (200, b"hello world\n")
    _, _, body = get(server, ls_url("/alias_dir"))
    names = [entry["name"] for entry in json.loads(body)["entries"]]
    assert "/alias_dir/train.csv" in names


@pytest.mark.skipif(sys.platform != "linux", reason="needs byte-string file names")
def test_non_utf8_file_name_round_trips(server, drive):
    raw_name = b"caf\xe9.txt"  # latin-1, not valid UTF-8
    try:
        with open(os.path.join(os.fsencode(drive), raw_name), "wb") as handle:
            handle.write(b"latin")
    except OSError:
        pytest.skip("the file system refuses non-UTF-8 names")
    fs = CFDataScienceFileSystem(
        host="127.0.0.1", port=server.port, retries=0, skip_instance_cache=True
    )
    names = [name for name in fs.ls("/", detail=False) if name.startswith("/caf")]
    assert len(names) == 1
    assert os.fsencode(names[0]) == b"/" + raw_name
    assert fs.cat_file(names[0]) == b"latin"
    assert fs.info(names[0])["size"] == 5


# -- protocol behaviour -------------------------------------------------------------


def test_unknown_endpoint_is_404(server):
    for target in ("/", "/v1", "/v1/other", "/v2/ls", "/hello.txt"):
        status, _, body = get(server, target)
        assert status == 404, target
        assert json.loads(body)["error"]["code"] == "not_found"


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_write_methods_are_refused(server, drive, method):
    status, headers, _ = request(server, method, file_url("/hello.txt"), body=b"overwrite")
    assert status == 405
    assert headers["Allow"] == "GET, HEAD"
    assert (drive / "hello.txt").read_bytes() == b"hello world\n"


def test_keep_alive_serves_many_requests_on_one_connection(server):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    try:
        conn.connect()
        first_socket = conn.sock
        for _ in range(5):
            conn.request("GET", file_url("/hello.txt"), headers={"Range": "bytes=0-4"})
            response = conn.getresponse()
            assert response.status == 206
            assert not response.will_close
            assert response.read() == b"hello"
            conn.request("GET", ls_url("/nope"))
            response = conn.getresponse()
            assert response.status == 404
            assert not response.will_close
            response.read()
            # http.client reconnects silently, so check it really is the same socket.
            assert conn.sock is first_socket
    finally:
        conn.close()


def test_concurrent_range_reads(server, big_bytes):
    errors = []

    def worker(index):
        try:
            start = index * 100_003
            end = start + 65_536
            status, _, body = get(
                server, file_url("/datasets/big.bin"), {"Range": f"bytes={start}-{end - 1}"}
            )
            assert status == 206
            assert body == big_bytes[start:end]
        except Exception as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(24)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors


# -- authentication -----------------------------------------------------------------


@pytest.fixture
def token_server(drive):
    server, thread = start_server(drive, token="s3cret-token")
    yield server
    stop_server(server, thread)


def test_token_required(token_server):
    for headers in (
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": "Basic s3cret-token"},
        {"Authorization": "s3cret-token"},
        {"Authorization": "Bearer "},
    ):
        for target in ("/v1/ls", file_url("/hello.txt"), "/not-an-endpoint"):
            status, response_headers, body = get(token_server, target, headers)
            assert status == 401, (headers, target)
            assert response_headers["WWW-Authenticate"].startswith("Bearer")
            assert b"hello world" not in body


def test_token_must_be_plain_ascii(drive):
    for bad in (
        "p\u00e4ssw\u00f6rd",
        "two words",
        "\u043f\u0430\u0440\u043e\u043b\u044c",
        "tab\there",
    ):
        with pytest.raises(ValueError):
            make_server(drive, port=0, token=bad)


def test_token_accepted(token_server):
    status, _, body = get(
        token_server, file_url("/hello.txt"), {"Authorization": "Bearer s3cret-token"}
    )
    assert (status, body) == (200, b"hello world\n")


# -- start-up -----------------------------------------------------------------------


def test_make_server_rejects_missing_root(tmp_path):
    with pytest.raises(NotADirectoryError):
        make_server(tmp_path / "missing", port=0)


def test_main_reports_missing_root(tmp_path, capsys):
    assert main(["--root", str(tmp_path / "missing"), "--port", "0"]) == 2
    assert "not a directory" in capsys.readouterr().err


def test_default_bind_is_loopback(drive):
    server = make_server(drive, port=0)
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()


# -- TLS ----------------------------------------------------------------------------


@pytest.fixture
def tls_files(tmp_path):
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("the openssl command is not available")
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    command = [
        openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
        "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
        "-keyout", str(key), "-out", str(cert),
    ]  # fmt: skip
    result = subprocess.run(command, capture_output=True, check=False)
    if result.returncode != 0:
        pytest.skip("openssl could not create a test certificate")
    return str(cert), str(key)


def test_tls_end_to_end(drive, tls_files):
    import fsspec

    cert, key = tls_files
    server, thread = start_server(drive, certfile=cert, keyfile=key, token="tok")
    try:
        url = f"cfdss://127.0.0.1:{server.port}/hello.txt"
        options = {"verify": cert, "token": "tok", "retries": 0, "skip_instance_cache": True}
        with fsspec.open(url, "rb", **options) as handle:
            assert handle.read() == b"hello world\n"
        # Without the CA the self-signed certificate must be rejected.
        untrusted = CFDataScienceFileSystem(
            host="127.0.0.1", port=server.port, use_ssl=True, token="tok", retries=0,
            skip_instance_cache=True,
        )  # fmt: skip
        with pytest.raises(ConnectionError):
            untrusted.ls("/")
    finally:
        stop_server(server, thread)


# -- regression tests from review ----------------------------------------------------


def swap_for_outside_link(drive, outside):
    """Resolve /d while it is a real directory, then replace it with a link to outside."""
    (drive / "d").mkdir()
    (drive / "d" / "inside.txt").write_text("inside")
    root = os.path.realpath(drive)
    stale = {
        "/d": resolve_path(root, "/d"),
        "/d/secret.txt": (
            "/d/secret.txt",
            os.path.join(root, "d", "secret.txt"),
        ),
    }
    shutil.rmtree(drive / "d")
    os.symlink(outside.parent, drive / "d")
    return root, stale


def test_directory_swapped_for_symlink_after_the_check_is_not_followed(
    drive, outside, tmp_path, monkeypatch
):
    """The path check and the open are separate steps; the open must not trust the check."""
    if not can_symlink(tmp_path):
        pytest.skip("symlinks are not available")
    if not server_module._DIR_FD_OPEN:
        pytest.skip("needs descriptor-relative open")
    root, stale = swap_for_outside_link(drive, outside)
    # Pretend the check ran before the swap, by handing back the pre-swap answer.
    monkeypatch.setattr(server_module, "resolve_path", lambda _root, path: stale[path])

    for list_entries in (True, False):
        with pytest.raises(HTTPError) as excinfo:
            describe(root, "/d", list_entries)
        assert excinfo.value.status in (403, 404)
    with pytest.raises(HTTPError) as excinfo:
        describe(root, "/d/secret.txt", False)
    assert excinfo.value.status in (403, 404)
    with pytest.raises(HTTPError) as excinfo:
        open_regular_file(root, "/d/secret.txt")
    assert excinfo.value.status in (403, 404)


def test_connection_limit(drive):
    server, thread = start_server(drive, max_connections=2)
    idle = []
    try:
        for _ in range(2):
            idle.append(socket.create_connection(("127.0.0.1", server.port), timeout=5))
        deadline = time.monotonic() + 5
        while len(server._connections) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(server._connections) == 2
        # A third connection is dropped instead of getting a thread of its own.
        reply = raw_exchange(server, b"GET /v1/ls HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n", timeout=5)
        assert reply == b""
        assert threading.active_count() < 20
        # Slots come back when connections close.
        for sock in idle:
            sock.close()
        idle.clear()
        deadline = time.monotonic() + 5
        status = None
        while status != 200 and time.monotonic() < deadline:
            try:
                status, _, _ = get(server, "/v1/ls")
            except OSError:
                time.sleep(0.02)
        assert status == 200
    finally:
        for sock in idle:
            sock.close()
        stop_server(server, thread)


def test_server_close_drops_open_connections(drive):
    server, thread = start_server(drive)
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    conn.request("GET", "/v1/ls")
    assert conn.getresponse().read()
    kept = conn.sock
    stop_server(server, thread)
    kept.settimeout(5)
    try:
        kept.sendall(b"GET /v1/ls HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        leftover = kept.recv(1024)
    except OSError:
        leftover = b""
    finally:
        conn.close()
    assert leftover == b""


@pytest.mark.parametrize(
    "host, expected",
    [
        ("127.0.0.1:8765", 200),
        ("[::1]:8765", 200),
        ("192.168.0.15", 200),
        ("localhost:8765", 200),
        ("drive-host:8765", 200),  # single-label names cannot be an outside website
        ("drive.corp.example:8765", 200),  # passed as allowed_hosts below
        ("DRIVE.corp.example.", 200),
        ("attacker.example:8765", 421),
        ("attacker.example", 421),
        ("127.0.0.1.attacker.example", 421),
    ],
)
def test_host_header_check_without_token(drive, host, expected):
    server, thread = start_server(drive, allowed_hosts=["drive.corp.example"])
    try:
        status, _, body = get(server, file_url("/hello.txt"), {"Host": host})
        assert status == expected
        if expected == 421:
            assert b"hello world" not in body
            assert json.loads(body)["error"]["code"] == "host_not_allowed"
    finally:
        stop_server(server, thread)


def test_host_header_is_not_checked_with_token(token_server):
    headers = {"Host": "any.name.example", "Authorization": "Bearer s3cret-token"}
    status, _, _ = get(token_server, file_url("/hello.txt"), headers)
    assert status == 200


@pytest.mark.parametrize(
    "extra_headers",
    [
        "Content-Length: 0\r\nContent-Length: {n}\r\n",
        "X : broken header\r\nContent-Length: {n}\r\n",
        "Content-Length: {n}\r\n",
        "Transfer-Encoding: chunked\r\n",
    ],
)
def test_request_body_is_never_run_as_a_second_request(server, extra_headers):
    smuggled = b"GET /v1/file?path=/hello.txt HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
    head = (
        "GET /v1/ls?path=/empty_dir HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        + extra_headers.format(n=len(smuggled))
        + "\r\n"
    ).encode()
    reply = raw_exchange(server, head + smuggled)
    assert reply.count(b"HTTP/1.1 ") == 1
    assert b"hello world" not in reply
    assert b"connection: close" in reply.lower()


def test_refused_method_reads_the_body_and_says_it_is_closing(server, drive):
    body = b"x" * 300_000
    status, headers, payload = request(server, "POST", file_url("/hello.txt"), body=body)
    assert status == 405
    assert headers["Connection"] == "close"
    assert json.loads(payload)["error"]["code"] == "method_not_allowed"
    assert (drive / "hello.txt").read_bytes() == b"hello world\n"


def test_expect_100_continue_is_not_invited(server):
    reply = raw_exchange(
        server,
        b"POST /v1/file?path=/x HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 5\r\n"
        b"Expect: 100-continue\r\n\r\n",
    )
    assert b"100 Continue" not in reply
    assert reply.startswith(b"HTTP/1.1 405")


@pytest.mark.parametrize(
    "request_line, status, code",
    [
        (b"PROPFIND /v1/ls HTTP/1.1", 405, "method_not_allowed"),
        (b"TRACE /v1/ls HTTP/1.1", 405, "method_not_allowed"),
        (b"GARBAGE", 400, "bad_request"),
        (b"GET /v1/ls HTTP/3.0", 505, "bad_request"),
        (b"GET http://[/v1/ls HTTP/1.1", 400, "bad_request"),
        (b"GET /v1/ls?path=" + b"a" * 70_000 + b" HTTP/1.1", 414, "bad_request"),
    ],
)
def test_protocol_errors_are_json_too(server, request_line, status, code):
    reply = raw_exchange(server, request_line + b"\r\nHost: 127.0.0.1\r\n\r\n")
    head, _, body = reply.partition(b"\r\n\r\n")
    assert head.startswith(f"HTTP/1.1 {status}".encode()), head[:60]
    assert b"application/json" in head
    assert json.loads(body)["error"]["code"] == code


def test_unrepresentable_mtime_does_not_break_downloads(server, monkeypatch):
    def explode(*args, **kwargs):
        raise ValueError("year 36812 is out of range")

    monkeypatch.setattr(server_module, "formatdate", explode)
    status, headers, body = get(server, file_url("/hello.txt"))
    assert (status, body) == (200, b"hello world\n")
    assert "Last-Modified" not in headers


def test_if_range(server):
    _, headers, _ = get(server, file_url("/hello.txt"))
    current = headers["ETag"]
    status, _, body = get(
        server, file_url("/hello.txt"), {"Range": "bytes=6-", "If-Range": current}
    )
    assert (status, body) == (206, b"world\n")
    status, _, body = get(
        server, file_url("/hello.txt"), {"Range": "bytes=6-", "If-Range": '"stale"'}
    )
    assert (status, body) == (200, b"hello world\n")


def make_sparse(path, size):
    with open(path, "wb") as handle:
        handle.truncate(size)


def drain(response) -> int:
    """Read a response to the end and return the byte count, tolerating a cut-off body."""
    received = 0
    try:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                return received
            received += len(chunk)
    except http.client.IncompleteRead as exc:
        return received + len(exc.partial)


def test_file_modified_during_transfer_is_cut_short(server, drive):
    """A full-length body that mixes old and new bytes must never be delivered."""
    size = 256 * 1024 * 1024  # far more than socket buffers can hold
    target = drive / "huge.bin"
    make_sparse(target, size)
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=30)
    try:
        conn.request("GET", file_url("/huge.bin"))
        response = conn.getresponse()
        assert response.status == 200
        assert int(response.getheader("Content-Length")) == size
        received = len(response.read(1024 * 1024))
        with open(target, "r+b") as handle:
            handle.write(b"changed")
        bump_mtime(target)
        received += drain(response)
        assert received < size
    finally:
        conn.close()


def test_file_truncated_during_transfer_is_cut_short(server, drive):
    size = 256 * 1024 * 1024
    target = drive / "huge.bin"
    make_sparse(target, size)
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=30)
    try:
        conn.request("GET", file_url("/huge.bin"))
        response = conn.getresponse()
        received = len(response.read(1024 * 1024))
        os.truncate(target, 10)
        received += drain(response)
        assert received < size
    finally:
        conn.close()


def test_unchanged_file_is_sent_in_full(server, drive):
    size = 64 * 1024 * 1024
    make_sparse(drive / "huge.bin", size)
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=30)
    try:
        conn.request("GET", file_url("/huge.bin"))
        assert drain(conn.getresponse()) == size
    finally:
        conn.close()


@pytest.mark.parametrize(
    "argv, message",
    [
        (["--port", "99999"], "not a port number"),
        (["--port", "-1"], "not a port number"),
        (["--log-level", "chatty"], "invalid choice"),
        (["--max-connections", "0"], "not a positive integer"),
    ],
)
def test_cli_rejects_bad_values_cleanly(drive, capsys, argv, message):
    with pytest.raises(SystemExit) as excinfo:
        main(["--root", str(drive), *argv])
    assert excinfo.value.code == 2
    assert message in capsys.readouterr().err


def test_cli_reports_token_file_problems(drive, tmp_path, capsys):
    assert main(["--root", str(drive), "--port", "0", "--token-file", str(tmp_path / "no")]) == 2
    assert "cfds-server:" in capsys.readouterr().err
    empty = tmp_path / "empty"
    empty.write_text("\n")
    assert main(["--root", str(drive), "--port", "0", "--token-file", str(empty)]) == 2
    assert "empty" in capsys.readouterr().err
    bad = tmp_path / "bad"
    bad.write_text("not a valid token!\n")
    assert main(["--root", str(drive), "--port", "0", "--token-file", str(bad)]) == 2
    assert "token" in capsys.readouterr().err


def test_server_module_needs_only_the_standard_library():
    """The drive host must be able to run the server without fsspec or requests."""
    program = (
        "import sys\n"
        "class Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in ('fsspec', 'requests', 'urllib3'):\n"
        "            raise ImportError('blocked: ' + name)\n"
        "sys.meta_path.insert(0, Block())\n"
        "import cf_datascience_fs.server as s\n"
        "import cf_datascience_fs\n"
        "assert cf_datascience_fs.__version__\n"
        "assert 'requests' not in sys.modules and 'fsspec' not in sys.modules\n"
        "sys.exit(s.main(['--version']))\n"
    )
    result = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "cfds-server" in result.stdout
