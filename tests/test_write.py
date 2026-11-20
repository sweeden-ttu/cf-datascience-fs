"""Write endpoints on a single-tree server started with ``writable=True``."""

from __future__ import annotations

import http.client
import json
import os
import socket
import threading
from urllib.parse import quote

import pytest
from conftest import start_server, stop_server

from cf_datascience_fs import _fsops


@pytest.fixture
def wserver(drive):
    server, thread = start_server(drive, writable=True)
    yield server
    stop_server(server, thread)


def call(server, method, endpoint, headers=None, body=None, **params):
    query = "&".join(f"{key}={quote(str(value), safe='/')}" for key, value in params.items())
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    try:
        conn.request(method, f"/v1/{endpoint}?{query}", body=body, headers=headers or {})
        response = conn.getresponse()
        data = response.read()
        payload = (
            json.loads(data) if data and "json" in response.getheader("Content-Type", "") else data
        )
        return response.status, dict(response.getheaders()), payload
    finally:
        conn.close()


def raw(server, payload: bytes, timeout=10) -> bytes:
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


def leftovers(root) -> list[str]:
    found = []
    for dirpath, _, files in os.walk(root):
        found += [
            os.path.join(dirpath, name) for name in files if name.startswith(_fsops.TMP_PREFIX)
        ]
    return found


# -- PUT ------------------------------------------------------------------------------


def test_put_creates_then_replaces(wserver, drive):
    status, headers, entry = call(wserver, "PUT", "file", body=b"first", path="/new.txt")
    assert status == 201
    assert entry["name"] == "/new.txt" and entry["size"] == 5 and entry["type"] == "file"
    assert entry["writable"] is True
    assert headers["ETag"] == entry["etag"]
    assert (drive / "new.txt").read_bytes() == b"first"

    status, _, entry2 = call(wserver, "PUT", "file", body=b"second!", path="/new.txt")
    assert status == 200
    assert entry2["size"] == 7 and entry2["etag"] != entry["etag"]
    assert (drive / "new.txt").read_bytes() == b"second!"
    assert leftovers(drive) == []


def test_put_empty_file(wserver, drive):
    status, _, entry = call(wserver, "PUT", "file", body=b"", path="/empty-new")
    assert status == 201 and entry["size"] == 0
    assert (drive / "empty-new").read_bytes() == b""


def test_put_overwrite_and_create_flags(wserver, drive):
    status, _, body = call(wserver, "PUT", "file", body=b"x", path="/hello.txt", overwrite=0)
    assert (status, body["error"]["code"]) == (409, "exists")
    assert (drive / "hello.txt").read_bytes() == b"hello world\n"
    status, _, body = call(wserver, "PUT", "file", body=b"x", path="/nope.txt", create=0)
    assert (status, body["error"]["code"]) == (404, "not_found")
    assert not (drive / "nope.txt").exists()
    status, _, _ = call(wserver, "PUT", "file", body=b"x", path="/hello.txt", create=0)
    assert status == 200


def test_put_preconditions(wserver, drive):
    _, headers, _ = call(wserver, "HEAD", "file", path="/hello.txt")
    etag = headers["ETag"]
    status, response_headers, body = call(
        wserver, "PUT", "file", {"If-Match": '"stale"'}, b"x", path="/hello.txt"
    )
    assert (status, body["error"]["code"]) == (412, "precondition_failed")
    assert response_headers["ETag"] == etag
    status, _, _ = call(wserver, "PUT", "file", {"If-None-Match": "*"}, b"x", path="/hello.txt")
    assert status == 412
    assert (drive / "hello.txt").read_bytes() == b"hello world\n"
    status, _, _ = call(wserver, "PUT", "file", {"If-Match": etag}, b"new", path="/hello.txt")
    assert status == 200
    status, _, _ = call(wserver, "PUT", "file", {"If-None-Match": "*"}, b"x", path="/fresh.txt")
    assert status == 201
    status, _, _ = call(wserver, "PUT", "file", {"If-Match": "*"}, b"x", path="/missing.txt")
    assert status == 412


@pytest.mark.parametrize(
    "path, status, code",
    [
        ("/", 403, "read_only"),
        ("/datasets", 400, "is_a_directory"),
        ("/no/such/dir.txt", 404, "parent_not_found"),
        ("/hello.txt/child", 400, "not_a_directory"),
        ("/" + _fsops.TMP_PREFIX + "x", 400, "bad_request"),
        ("/" + "n" * 256, 400, "bad_request"),
        ("/../escape.txt", 400, "bad_request"),
    ],
)
def test_put_refusals(wserver, drive, path, status, code):
    got, _, body = call(wserver, "PUT", "file", body=b"data", path=path)
    assert (got, body["error"]["code"]) == (status, code)
    assert leftovers(drive) == []


def test_put_into_symlinked_directory_outside_is_refused(wserver, drive, outside):
    os.symlink(outside.parent, drive / "link-out")
    status, _, body = call(wserver, "PUT", "file", body=b"x", path="/link-out/evil.txt")
    assert status in (403, 404)
    assert not (outside.parent / "evil.txt").exists()


def test_put_replaces_a_symlink_instead_of_writing_through_it(wserver, drive, outside):
    os.symlink(outside, drive / "alias.txt")
    status, _, _ = call(wserver, "PUT", "file", body=b"mine", path="/alias.txt")
    assert status == 200
    assert outside.read_text() == "top secret"
    assert not os.path.islink(drive / "alias.txt")
    assert (drive / "alias.txt").read_bytes() == b"mine"


def test_put_chunked(wserver, drive):
    conn = http.client.HTTPConnection("127.0.0.1", wserver.port, timeout=10)
    try:
        conn.request(
            "PUT", "/v1/file?path=/chunked.bin", body=iter([b"abc", b"", b"defg", b"h" * 70000])
        )
        response = conn.getresponse()
        assert response.status == 201, response.read()
        response.read()
    finally:
        conn.close()
    assert (drive / "chunked.bin").read_bytes() == b"abcdefg" + b"h" * 70000


def test_put_chunked_on_the_wire_with_extensions_and_trailers(wserver, drive):
    reply = raw(
        wserver,
        b"PUT /v1/file?path=/wire.txt HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n"
        b"Connection: close\r\n\r\n"
        b"5;name=value\r\nhello\r\n1\r\n!\r\n0\r\nX-Trailer: 1\r\n\r\n",
    )
    assert reply.startswith(b"HTTP/1.1 201"), reply[:80]
    assert (drive / "wire.txt").read_bytes() == b"hello!"


@pytest.mark.parametrize(
    "framing",
    [
        b"Transfer-Encoding: chunked\r\nContent-Length: 5\r\n\r\n5\r\nhello\r\n0\r\n\r\n",
        b"Transfer-Encoding: gzip\r\n\r\nxxxx",
        b"Content-Length: 5\r\nContent-Length: 6\r\n\r\nhello!",
        b"Content-Length: -1\r\n\r\n",
        b"Transfer-Encoding: chunked\r\n\r\nzz\r\nhello\r\n0\r\n\r\n",
        b"Transfer-Encoding: chunked\r\n\r\n5\r\nhelloXX0\r\n\r\n",
    ],
)
def test_put_bad_framing_is_refused(wserver, drive, framing):
    reply = raw(wserver, b"PUT /v1/file?path=/framed.txt HTTP/1.1\r\nHost: x\r\n" + framing)
    assert reply.startswith(b"HTTP/1.1 400"), reply[:80]
    assert b"connection: close" in reply.lower()
    assert not (drive / "framed.txt").exists()
    assert leftovers(drive) == []


def test_put_cut_short_keeps_the_old_file(wserver, drive):
    with socket.create_connection(("127.0.0.1", wserver.port), timeout=10) as sock:
        sock.sendall(
            b"PUT /v1/file?path=/hello.txt HTTP/1.1\r\nHost: x\r\nContent-Length: 1000\r\n\r\n"
            + b"partial"
        )
        sock.shutdown(socket.SHUT_WR)
        sock.recv(65536)
    assert (drive / "hello.txt").read_bytes() == b"hello world\n"
    deadline = 50
    while leftovers(drive) and deadline:
        threading.Event().wait(0.05)
        deadline -= 1
    assert leftovers(drive) == []


def test_expect_100_continue_after_checks(wserver, drive):
    with socket.create_connection(("127.0.0.1", wserver.port), timeout=10) as sock:
        sock.sendall(
            b"PUT /v1/file?path=/expect.txt HTTP/1.1\r\nHost: x\r\nContent-Length: 4\r\n"
            b"Expect: 100-continue\r\n\r\n"
        )
        interim = sock.recv(65536)
        assert interim == b"HTTP/1.1 100 Continue\r\n\r\n"
        sock.sendall(b"body")
        final = sock.recv(65536)
    assert final.startswith(b"HTTP/1.1 201")
    assert (drive / "expect.txt").read_bytes() == b"body"


def test_expect_100_continue_not_sent_when_refused(wserver, drive):
    reply = raw(
        wserver,
        b"PUT /v1/file?path=/hello.txt&overwrite=0 HTTP/1.1\r\nHost: x\r\nContent-Length: 4\r\n"
        b"Expect: 100-continue\r\n\r\n",
    )
    assert b"100 Continue" not in reply
    assert reply.startswith(b"HTTP/1.1 409")
    assert b"connection: close" in reply.lower()


def test_uploads_in_progress_are_hidden(wserver, drive):
    (drive / (_fsops.TMP_PREFIX + "abc.tmp")).write_bytes(b"partial")
    _, _, listing = call(wserver, "GET", "ls", path="/")
    assert all(_fsops.TMP_PREFIX not in entry["name"] for entry in listing["entries"])


def test_keep_alive_after_upload(wserver, drive):
    conn = http.client.HTTPConnection("127.0.0.1", wserver.port, timeout=10)
    try:
        for index in range(3):
            conn.request("PUT", f"/v1/file?path=/k{index}.txt", body=b"x" * index)
            response = conn.getresponse()
            assert response.status == 201
            response.read()
        conn.request("GET", "/v1/file?path=/k2.txt")
        assert conn.getresponse().read() == b"xx"
    finally:
        conn.close()


# -- mkdir / delete / mv / cp ---------------------------------------------------------------


def test_mkdir(wserver, drive):
    status, _, entry = call(wserver, "POST", "mkdir", path="/made")
    assert status == 201 and entry["type"] == "directory" and entry["name"] == "/made"
    status, _, body = call(wserver, "POST", "mkdir", path="/made")
    assert (status, body["error"]["code"]) == (409, "exists")
    status, _, body = call(wserver, "POST", "mkdir", path="/a/b/c")
    assert (status, body["error"]["code"]) == (404, "parent_not_found")
    status, _, entry = call(wserver, "POST", "mkdir", path="/a/b/c", parents=1)
    assert status == 201 and entry["name"] == "/a/b/c"
    assert (drive / "a" / "b" / "c").is_dir()
    status, _, _ = call(wserver, "POST", "mkdir", path="/a/b/c", parents=1)
    assert status == 200
    status, _, body = call(wserver, "POST", "mkdir", path="/hello.txt", parents=1)
    assert (status, body["error"]["code"]) == (409, "exists")
    status, _, body = call(wserver, "POST", "mkdir", path="/hello.txt/sub", parents=1)
    assert (status, body["error"]["code"]) == (400, "not_a_directory")
    status, _, body = call(wserver, "POST", "mkdir", path="/")
    assert (status, body["error"]["code"]) == (409, "exists")
    status, _, _ = call(wserver, "POST", "mkdir", path="/", parents=1)
    assert status == 200


def test_delete(wserver, drive):
    status, _, _ = call(wserver, "DELETE", "file", path="/hello.txt")
    assert status == 204
    assert not (drive / "hello.txt").exists()
    status, _, body = call(wserver, "DELETE", "file", path="/hello.txt")
    assert (status, body["error"]["code"]) == (404, "not_found")
    status, _, body = call(wserver, "DELETE", "file", path="/datasets")
    assert (status, body["error"]["code"]) == (409, "not_empty")
    status, _, _ = call(wserver, "DELETE", "file", path="/empty_dir")
    assert status == 204
    status, _, _ = call(wserver, "DELETE", "file", path="/datasets", recursive=1)
    assert status == 204
    assert not (drive / "datasets").exists()
    status, _, body = call(wserver, "DELETE", "file", path="/")
    assert (status, body["error"]["code"]) == (403, "read_only")


def test_delete_if_match(wserver, drive):
    status, _, _ = call(wserver, "DELETE", "file", {"If-Match": '"old"'}, path="/hello.txt")
    assert status == 412
    _, headers, _ = call(wserver, "HEAD", "file", path="/hello.txt")
    status, _, _ = call(wserver, "DELETE", "file", {"If-Match": headers["ETag"]}, path="/hello.txt")
    assert status == 204


def test_delete_recursive_does_not_follow_symlinks(wserver, drive, outside):
    (drive / "tree").mkdir()
    os.symlink(outside.parent, drive / "tree" / "out")
    status, _, _ = call(wserver, "DELETE", "file", path="/tree", recursive=1)
    assert status == 204
    assert outside.read_text() == "top secret"


def test_move(wserver, drive):
    status, _, entry = call(wserver, "POST", "mv", src="/hello.txt", dst="/moved.txt")
    assert status == 200 and entry["name"] == "/moved.txt" and entry["size"] == 12
    assert not (drive / "hello.txt").exists()
    status, _, body = call(wserver, "POST", "mv", src="/moved.txt", dst="/empty.bin")
    assert (status, body["error"]["code"]) == (409, "exists")
    status, _, _ = call(wserver, "POST", "mv", src="/moved.txt", dst="/empty.bin", overwrite=1)
    assert status == 200
    assert (drive / "empty.bin").read_bytes() == b"hello world\n"
    status, _, body = call(wserver, "POST", "mv", src="/datasets", dst="/datasets/nested/x")
    assert (status, body["error"]["code"]) == (400, "invalid_move")
    status, _, body = call(wserver, "POST", "mv", src="/datasets", dst="/empty.bin", overwrite=1)
    assert (status, body["error"]["code"]) == (409, "exists")
    status, _, entry = call(wserver, "POST", "mv", src="/datasets", dst="/data2")
    assert status == 200 and entry["type"] == "directory"
    status, _, body = call(wserver, "POST", "mv", src="/missing", dst="/x")
    assert (status, body["error"]["code"]) == (404, "not_found")
    status, _, body = call(wserver, "POST", "mv", src="/data2", dst="/")
    assert (status, body["error"]["code"]) == (403, "read_only")


def test_copy(wserver, drive):
    status, headers, entry = call(wserver, "POST", "cp", src="/datasets/big.bin", dst="/copy.bin")
    assert status == 201 and headers["ETag"] == entry["etag"]
    assert (drive / "copy.bin").read_bytes() == (drive / "datasets" / "big.bin").read_bytes()
    status, _, body = call(wserver, "POST", "cp", src="/hello.txt", dst="/copy.bin")
    assert (status, body["error"]["code"]) == (409, "exists")
    status, _, _ = call(wserver, "POST", "cp", src="/hello.txt", dst="/copy.bin", overwrite=1)
    assert status == 200
    assert (drive / "copy.bin").read_bytes() == b"hello world\n"
    status, _, body = call(wserver, "POST", "cp", src="/datasets", dst="/dir-copy")
    assert (status, body["error"]["code"]) == (400, "is_a_directory")


def test_missing_parameters(wserver):
    status, _, body = call(wserver, "POST", "mv", src="/hello.txt")
    assert (status, body["error"]["code"]) == (400, "bad_request")
    status, _, body = call(wserver, "POST", "mkdir", path="/x", parents="maybe")
    assert (status, body["error"]["code"]) == (400, "bad_request")


def test_wrong_method_lists_allowed_ones(wserver):
    status, headers, body = call(wserver, "GET", "mkdir", path="/x")
    assert (status, body["error"]["code"]) == (405, "method_not_allowed")
    assert headers["Allow"] == "POST"
    status, headers, _ = call(wserver, "PATCH", "file", path="/hello.txt")
    assert status == 405 and headers["Allow"] == "GET, HEAD, PUT, DELETE"


def test_read_only_server_refuses_every_write(server, drive):
    for method, endpoint, params in (
        ("PUT", "file", {"path": "/hello.txt"}),
        ("DELETE", "file", {"path": "/hello.txt"}),
        ("POST", "mkdir", {"path": "/x"}),
        ("POST", "mv", {"src": "/hello.txt", "dst": "/x"}),
        ("POST", "cp", {"src": "/hello.txt", "dst": "/x"}),
    ):
        status, headers, _ = call(
            server, method, endpoint, body=b"" if method != "DELETE" else None, **params
        )
        assert status == 405, (method, endpoint)
        assert headers["Allow"] == "GET, HEAD"
    assert sorted(os.listdir(drive)) == ["datasets", "empty.bin", "empty_dir", "hello.txt"]


def test_me_single_tree(wserver, server):
    status, _, me = call(wserver, "GET", "me")
    assert status == 200
    assert me["user"]["sub"] == "anonymous"
    assert me["home"] == "/" and me["default_path"] == "/" and me["quota"] is None
    assert me["mounts"] == [{"path": "/", "writable": True, "description": "Served directory"}]
    _, _, me = call(server, "GET", "me")
    assert me["home"] is None and me["mounts"][0]["writable"] is False


def test_concurrent_uploads_to_one_name(wserver, drive):
    bodies = [bytes([65 + index]) * 200_000 for index in range(6)]
    results = []

    def upload(body):
        results.append(call(wserver, "PUT", "file", body=body, path="/race.bin")[0])

    threads = [threading.Thread(target=upload, args=(body,)) for body in bodies]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results).count(201) == 1 and all(code in (200, 201) for code in results)
    assert (drive / "race.bin").read_bytes() in bodies
    assert leftovers(drive) == []
