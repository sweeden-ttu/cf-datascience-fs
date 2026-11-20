"""Multi-user mode: OIDC sign-in, /home per user with a quota, shared /kaggle-datasets."""

from __future__ import annotations

import http.client
import json
import os
import time
from urllib.parse import quote

import jwt
import pytest
from conftest import start_server, stop_server
from fake_oidc import ISSUER, FakeProvider

from cf_datascience_fs._auth import OIDCAuth
from cf_datascience_fs._users import UserStore, directory_name, disk_usage


@pytest.fixture
def provider():
    return FakeProvider()


@pytest.fixture
def datasets(tmp_path):
    root = tmp_path / "kaggle-datasets"
    (root / "titanic").mkdir(parents=True)
    (root / "titanic" / "train.csv").write_text("PassengerId,Survived\n1,0\n2,1\n")
    return root


@pytest.fixture
def users_root(tmp_path):
    root = tmp_path / "users"
    root.mkdir()
    return root


def start(provider, datasets, users_root, **kwargs):
    auth = OIDCAuth(ISSUER, ("cfds-vscode", "cfds-ios", "cfds-cli"), fetch=provider.fetch)
    return start_server(
        None,
        auth=auth,
        users_root=users_root,
        datasets_root=datasets,
        public_url="https://storage.example.test",
        **kwargs,
    )


@pytest.fixture
def mserver(provider, datasets, users_root):
    server, thread = start(provider, datasets, users_root)
    yield server
    stop_server(server, thread)


def call(server, method, endpoint, token=None, headers=None, body=None, **params):
    query = "&".join(f"{key}={quote(str(value), safe='/')}" for key, value in params.items())
    headers = dict(headers or {})
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    try:
        target = endpoint if endpoint.startswith("/") else f"/v1/{endpoint}?{query}"
        conn.request(method, target, body=body, headers=headers)
        response = conn.getresponse()
        data = response.read()
        is_json = "json" in (response.getheader("Content-Type") or "")
        return response.status, dict(response.getheaders()), json.loads(data) if is_json else data
    finally:
        conn.close()


# -- sign-in --------------------------------------------------------------------------


def test_no_token_gets_a_401_pointing_at_the_provider(mserver):
    status, headers, body = call(mserver, "GET", "ls", path="/")
    assert (status, body["error"]["code"]) == (401, "unauthorized")
    challenge = headers["WWW-Authenticate"]
    assert challenge.startswith('Bearer realm="cfds"')
    assert "error=" not in challenge
    assert (
        'resource_metadata="https://storage.example.test/.well-known/oauth-protected-resource"'
        in challenge
    )


def test_protected_resource_metadata_needs_no_token(mserver):
    status, _, body = call(mserver, "GET", "/.well-known/oauth-protected-resource")
    assert status == 200
    assert body["resource"] == "https://storage.example.test"
    assert body["authorization_servers"] == [ISSUER]


@pytest.mark.parametrize(
    "make_token",
    [
        lambda p: "garbage.garbage.garbage",
        lambda p: p.token(lifetime=-3600),
        lambda p: p.token(aud="some-other-app"),
        lambda p: p.token(issuer="https://evil.example"),
        lambda p: p.token(kid=p.add_key(publish=False)),
        lambda p: jwt.encode({"sub": "alice", "iss": ISSUER, "aud": "cfds-vscode"}, "k" * 32),
        lambda p: jwt.encode(
            {"sub": "alice", "iss": ISSUER, "aud": "cfds-vscode", "exp": time.time() + 60},
            None,
            algorithm="none",
        ),
        lambda p: p.token()[:-4] + "AAAA",
        lambda p: "opaque-but-unknown",
    ],
)
def test_bad_tokens_are_refused(mserver, provider, make_token):
    status, headers, body = call(mserver, "GET", "ls", make_token(provider), path="/")
    assert (status, body["error"]["code"]) == (401, "unauthorized")
    assert 'error="invalid_token"' in headers["WWW-Authenticate"]


def test_every_registered_client_is_accepted(mserver, provider):
    for client in ("cfds-vscode", "cfds-ios", "cfds-cli", ["cfds-ios", "x"]):
        status, _, _ = call(mserver, "GET", "ls", provider.token(aud=client), path="/")
        assert status == 200, client


def test_key_rotation_is_picked_up(mserver, provider):
    assert call(mserver, "GET", "ls", provider.token(), path="/")[0] == 200
    new_kid = provider.add_key()
    mserver.auth._keys_fetched -= 120  # the last fetch was a while ago
    # The new key is not known yet; one forced refresh finds it.
    assert call(mserver, "GET", "ls", provider.token(kid=new_kid), path="/")[0] == 200


def test_unknown_key_ids_do_not_hammer_the_provider(mserver, provider):
    call(mserver, "GET", "ls", provider.token(), path="/")
    before = len(provider.calls)
    for _ in range(5):
        stray = provider.add_key(publish=False)
        call(mserver, "GET", "ls", provider.token(kid=stray), path="/")
    assert len(provider.calls) - before <= 1


def test_provider_down_is_a_503(mserver, provider):
    provider.down = True
    status, headers, body = call(mserver, "GET", "ls", provider.token(), path="/")
    assert (status, body["error"]["code"]) == (503, "auth_unavailable")
    assert "Retry-After" in headers


def test_opaque_token_goes_to_userinfo_and_is_cached(mserver, provider):
    provider.opaque["opaque-123"] = {"sub": "carol", "email": "carol@example.test"}
    for _ in range(3):
        status, _, me = call(mserver, "GET", "me", "opaque-123")
        assert status == 200
    assert me["user"]["sub"] == "carol" and me["user"]["email"] == "carol@example.test"
    assert provider.calls.count(ISSUER + "/userinfo") == 1


# -- what a user sees ------------------------------------------------------------------


def test_me(mserver, provider):
    token = provider.token("alice", email="alice@example.test", name="Alice", roles=["free"])
    status, _, me = call(mserver, "GET", "me", token)
    assert status == 200
    assert me["user"] == {
        "sub": "alice",
        "email": "alice@example.test",
        "name": "Alice",
        "roles": ["free"],
    }
    assert me["quota"] == {"limit": 107374182400, "used": 0, "available": 107374182400}
    assert me["home"] == "/home"
    assert me["datasets"] == "/kaggle-datasets"
    assert me["default_path"] == "/kaggle-datasets"
    assert me["mounts"] == [
        {
            "path": "/home",
            "writable": True,
            "description": "Your files",
        },
        {
            "path": "/kaggle-datasets",
            "writable": False,
            "description": "Kaggle datasets (shared, read-only)",
        },
    ]


def test_me_fills_profile_from_userinfo(mserver, provider):
    token = provider.token("dave")
    provider.opaque[token] = {"sub": "dave", "email": "dave@example.test", "name": "Dave"}
    _, _, me = call(mserver, "GET", "me", token)
    assert me["user"]["email"] == "dave@example.test" and me["user"]["name"] == "Dave"


def test_root_lists_the_two_mounts(mserver, provider):
    status, _, listing = call(mserver, "GET", "ls", provider.token(), path="/")
    assert status == 200
    assert listing["info"]["name"] == "/" and listing["info"]["writable"] is False
    names = [(entry["name"], entry["writable"]) for entry in listing["entries"]]
    assert names == [("/home", True), ("/kaggle-datasets", False)]


def test_datasets_are_readable_but_not_writable(mserver, provider):
    token = provider.token()
    status, _, listing = call(mserver, "GET", "ls", token, path="/kaggle-datasets/titanic")
    assert status == 200
    assert [entry["name"] for entry in listing["entries"]] == ["/kaggle-datasets/titanic/train.csv"]
    assert listing["entries"][0]["writable"] is False
    status, _, body = call(mserver, "GET", "file", token, path="/kaggle-datasets/titanic/train.csv")
    assert status == 200 and body.startswith(b"PassengerId")
    for method, endpoint, params in (
        ("PUT", "file", {"path": "/kaggle-datasets/titanic/x.csv"}),
        ("PUT", "file", {"path": "/kaggle-datasets/titanic/train.csv"}),
        ("DELETE", "file", {"path": "/kaggle-datasets/titanic/train.csv"}),
        ("POST", "mkdir", {"path": "/kaggle-datasets/new"}),
        ("POST", "mv", {"src": "/kaggle-datasets/titanic", "dst": "/kaggle-datasets/t2"}),
        ("PUT", "file", {"path": "/top-level.txt"}),
        ("POST", "mkdir", {"path": "/newmount"}),
        ("PUT", "file", {"path": "/home"}),
        ("DELETE", "file", {"path": "/home"}),
    ):
        status, _, body = call(mserver, method, endpoint, token, body=b"x", **params)
        assert (status, body["error"]["code"]) == (403, "read_only"), (method, params)


def test_users_have_separate_homes(mserver, provider, users_root):
    alice, bob = provider.token("alice"), provider.token("bob")
    status, _, entry = call(mserver, "PUT", "file", alice, body=b"alice's", path="/home/notes.txt")
    assert status == 201 and entry["name"] == "/home/notes.txt"
    assert (users_root / "alice" / "notes.txt").read_bytes() == b"alice's"
    status, _, body = call(mserver, "GET", "file", bob, path="/home/notes.txt")
    assert (status, body["error"]["code"]) == (404, "not_found")
    _, _, listing = call(mserver, "GET", "ls", bob, path="/home")
    assert listing["entries"] == []
    assert oct(os.stat(users_root / "alice").st_mode & 0o777) == "0o700"


def test_odd_subjects_get_hashed_directories(mserver, provider, users_root):
    token = provider.token("Alice@Example.COM/../x")
    status, _, _ = call(mserver, "PUT", "file", token, body=b"x", path="/home/a.txt")
    assert status == 201
    name = directory_name("Alice@Example.COM/../x")
    assert name.startswith("h-") and (users_root / name / "a.txt").exists()
    assert directory_name("h-looks-hashed") != "h-looks-hashed"
    assert directory_name("bob_2") == "bob_2"


def test_cross_mount_move_is_refused_and_copy_works(mserver, provider, users_root):
    token = provider.token("alice")
    status, _, body = call(
        mserver, "POST", "mv", token, src="/kaggle-datasets/titanic/train.csv", dst="/home/t.csv"
    )
    assert (status, body["error"]["code"]) == (400, "cross_mount")
    status, _, entry = call(
        mserver, "POST", "cp", token, src="/kaggle-datasets/titanic/train.csv", dst="/home/t.csv"
    )
    assert status == 201 and entry["name"] == "/home/t.csv"
    assert (users_root / "alice" / "t.csv").read_text().startswith("PassengerId")
    _, _, me = call(mserver, "GET", "me", token)
    assert me["quota"]["used"] == entry["size"]
    status, _, entry = call(mserver, "POST", "mv", token, src="/home/t.csv", dst="/home/u.csv")
    assert status == 200 and entry["name"] == "/home/u.csv"


def test_datasets_writer_role(provider, datasets, users_root):
    server, thread = start(provider, datasets, users_root, datasets_writer_role="curator")
    try:
        curator = provider.token("cur", roles=["curator"])
        status, _, _ = call(
            server, "PUT", "file", curator, body=b"x", path="/kaggle-datasets/new.csv"
        )
        assert status == 201
        status, _, _ = call(
            server,
            "PUT",
            "file",
            provider.token("eve"),
            body=b"x",
            path="/kaggle-datasets/new2.csv",
        )
        assert status == 403
    finally:
        stop_server(server, thread)


# -- quota ----------------------------------------------------------------------------


@pytest.fixture
def small(provider, datasets, users_root):
    server, thread = start(provider, datasets, users_root, default_quota=1000)
    yield server
    stop_server(server, thread)


def used(server, token):
    return call(server, "GET", "me", token)[2]["quota"]["used"]


def test_quota_is_enforced(small, provider, users_root):
    token = provider.token("alice")
    status, _, body = call(small, "PUT", "file", token, body=b"x" * 1001, path="/home/big")
    assert (status, body["error"]["code"]) == (507, "quota_exceeded")
    assert not (users_root / "alice" / "big").exists()
    assert used(small, token) == 0
    assert call(small, "PUT", "file", token, body=b"x" * 600, path="/home/a")[0] == 201
    assert used(small, token) == 600
    status, _, _ = call(small, "PUT", "file", token, body=b"x" * 500, path="/home/b")
    assert status == 507
    # Replacing a file only needs room for the difference.
    assert call(small, "PUT", "file", token, body=b"y" * 900, path="/home/a")[0] == 200
    assert used(small, token) == 900
    assert call(small, "PUT", "file", token, body=b"y" * 100, path="/home/a")[0] == 200
    assert used(small, token) == 100
    assert call(small, "DELETE", "file", token, path="/home/a")[0] == 204
    assert used(small, token) == 0


def test_quota_with_chunked_upload(small, provider, users_root):
    token = provider.token("alice")
    conn = http.client.HTTPConnection("127.0.0.1", small.port, timeout=10)
    try:
        conn.request(
            "PUT",
            "/v1/file?path=/home/chunked",
            body=iter([b"x" * 400, b"x" * 400, b"x" * 400]),
            headers={"Authorization": f"Bearer {token}"},
        )
        response = conn.getresponse()
        assert response.status == 507
        assert json.loads(response.read())["error"]["code"] == "quota_exceeded"
    finally:
        conn.close()
    assert not (users_root / "alice" / "chunked").exists()
    assert used(small, token) == 0


def test_quota_frees_on_recursive_delete_and_counts_copies(small, provider):
    token = provider.token("alice")
    call(small, "POST", "mkdir", token, path="/home/d/e", parents=1)
    call(small, "PUT", "file", token, body=b"x" * 300, path="/home/d/e/f")
    call(small, "POST", "cp", token, src="/home/d/e/f", dst="/home/g")
    assert used(small, token) == 600
    status, _, _ = call(small, "POST", "cp", token, src="/home/g", dst="/home/h")
    assert status == 201
    status, _, body = call(small, "POST", "cp", token, src="/home/g", dst="/home/i")
    assert (status, body["error"]["code"]) == (507, "quota_exceeded")
    assert call(small, "DELETE", "file", token, path="/home/d", recursive=1)[0] == 204
    assert used(small, token) == 600


def test_usage_is_measured_at_startup(provider, datasets, users_root):
    (users_root / "alice" / "sub").mkdir(parents=True)
    (users_root / "alice" / "sub" / "old.bin").write_bytes(b"z" * 123)
    server, thread = start(provider, datasets, users_root)
    try:
        assert used(server, provider.token("alice")) == 123
    finally:
        stop_server(server, thread)


def test_per_user_quota_override(provider, datasets, users_root):
    store = UserStore(users_root, 1000)
    store.home("vip")
    store._db.execute("UPDATE users SET quota_limit = 5000 WHERE sub = 'vip'")
    store.close()
    server, thread = start(provider, datasets, users_root, default_quota=1000)
    try:
        _, _, me = call(server, "GET", "me", provider.token("vip"))
        assert me["quota"]["limit"] == 5000
        _, _, me = call(server, "GET", "me", provider.token("other"))
        assert me["quota"]["limit"] == 1000
    finally:
        stop_server(server, thread)


def test_disk_usage_ignores_symlinks_and_removes_stale_uploads(tmp_path, outside):
    root = tmp_path / "home"
    root.mkdir()
    (root / "a").write_bytes(b"x" * 10)
    os.symlink(outside, root / "link")
    os.symlink(outside.parent, root / "dirlink")
    stale = root / ".cfds-upload-old.tmp"
    stale.write_bytes(b"y" * 5)
    os.utime(stale, (time.time() - 2 * 86400,) * 2)
    assert disk_usage(str(root)) == 10
    assert not stale.exists()


# -- browsers ---------------------------------------------------------------------------


def test_cors_preflight_and_response_headers(mserver, provider):
    preflight = {
        "Origin": "https://vscode.dev",
        "Access-Control-Request-Method": "PUT",
        "Access-Control-Request-Headers": "authorization, if-match",
    }
    status, headers, _ = call(mserver, "OPTIONS", "file", headers=preflight, path="/home/x")
    assert status == 204
    assert headers["Access-Control-Allow-Origin"] == "https://vscode.dev"
    assert "PUT" in headers["Access-Control-Allow-Methods"]
    assert "Authorization" in headers["Access-Control-Allow-Headers"]
    assert "If-Match" in headers["Access-Control-Allow-Headers"]

    origin = {"Origin": "https://abc123.vscode-cdn.net"}
    status, headers, _ = call(mserver, "GET", "ls", provider.token(), origin, path="/")
    assert status == 200
    assert headers["Access-Control-Allow-Origin"] == "https://abc123.vscode-cdn.net"
    assert "ETag" in headers["Access-Control-Expose-Headers"]
    # A 401 must be readable too, so the web extension can start a sign-in.
    status, headers, _ = call(mserver, "GET", "ls", None, origin, path="/")
    assert status == 401 and "Access-Control-Allow-Origin" in headers

    for evil in (
        "https://evil.example",
        "https://vscode-cdn.net.evil.example",
        "http://vscode.dev",
    ):
        _, headers, _ = call(mserver, "GET", "ls", provider.token(), {"Origin": evil}, path="/")
        assert "Access-Control-Allow-Origin" not in headers, evil
