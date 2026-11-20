"""``cfds``: sign in to a cfds server from the command line.

``cfds login https://storage.cloudfronts.group`` opens the sign-in page in a browser
(OAuth 2.0 authorization code flow with PKCE, RFC 7636, redirecting to a one-time local
port, RFC 8252) and saves the access token for the Python filesystem to use.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import secrets
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

from . import _credentials
from ._version import __version__

DEFAULT_SERVER = "https://storage.cloudfronts.group"
DEFAULT_CLIENT_ID = "cfds-cli"
SCOPES = "openid profile email"
LOGIN_TIMEOUT = 300


class LoginError(Exception):
    pass


def _get_json(url: str, token: str | None = None, timeout: float = 30) -> tuple[int, dict]:
    headers = {"Accept": "application/json", "User-Agent": f"cfds/{__version__}"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            status, raw = response.status, response.read(1024 * 1024)
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read(1024 * 1024)
    try:
        payload = json.loads(raw) if raw else {}
    except ValueError:
        payload = {}
    return status, payload if isinstance(payload, dict) else {}


def discover_issuer(server: str) -> str:
    """Ask the cfds server which sign-in provider it trusts (RFC 9728)."""
    status, payload = _get_json(server.rstrip("/") + "/.well-known/oauth-protected-resource")
    servers = payload.get("authorization_servers") if status == 200 else None
    if not servers or not isinstance(servers[0], str):
        raise LoginError(
            f"{server} does not say which sign-in provider to use (HTTP {status}); pass --issuer"
        )
    return servers[0]


def provider_metadata(issuer: str) -> dict:
    status, payload = _get_json(issuer.rstrip("/") + "/.well-known/openid-configuration")
    if (
        status != 200
        or not payload.get("authorization_endpoint")
        or not payload.get("token_endpoint")
    ):
        raise LoginError(f"cannot read the sign-in provider's settings at {issuer} (HTTP {status})")
    return payload


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


class _Callback(BaseHTTPRequestHandler):
    result: dict

    def log_message(self, format, *args):  # noqa: A002
        pass

    def do_GET(self):  # noqa: N802
        parts = urllib.parse.urlsplit(self.path)
        if parts.path != "/callback":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        query = urllib.parse.parse_qs(parts.query)
        self.server.result = {key: values[0] for key, values in query.items()}
        ok = "code" in self.server.result
        text = (
            "Signed in to cfds. You can close this tab."
            if ok
            else "Sign-in failed: "
            + html.escape(
                self.server.result.get("error_description")
                or self.server.result.get("error", "unknown error")
            )
        )
        body = f"<!doctype html><title>cfds</title><p>{text}</p>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.server.done.set()


def authorize(
    metadata: dict,
    client_id: str,
    *,
    port: int = 0,
    open_browser: bool = True,
    timeout: float = LOGIN_TIMEOUT,
    out=sys.stderr,
) -> dict:
    """Run the browser sign-in and return the provider's token response."""
    httpd = HTTPServer(("127.0.0.1", port), _Callback)
    httpd.result = None
    httpd.done = threading.Event()
    redirect_uri = f"http://127.0.0.1:{httpd.server_address[1]}/callback"
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    url = metadata["authorization_endpoint"] + "?" + urllib.parse.urlencode(params)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.2})
    thread.daemon = True
    thread.start()
    try:
        print(f"Opening the sign-in page. If no browser opens, visit:\n\n  {url}\n", file=out)
        if open_browser:
            webbrowser.open(url)
        if not httpd.done.wait(timeout):
            raise LoginError("timed out waiting for the browser sign-in")
    finally:
        httpd.shutdown()
        httpd.server_close()
    result = httpd.result or {}
    if result.get("state") != state:
        raise LoginError("the sign-in response did not match this request (state mismatch)")
    if "code" not in result:
        raise LoginError(
            "sign-in failed: " + (result.get("error_description") or result.get("error", "?"))
        )
    status, payload = _credentials.post_form(
        metadata["token_endpoint"],
        {
            "grant_type": "authorization_code",
            "code": result["code"],
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    if status != 200:
        detail = payload.get("error_description") or payload.get("error") or f"HTTP {status}"
        raise LoginError(f"the sign-in provider refused the code: {detail}")
    return payload


def login(
    server: str,
    *,
    issuer: str | None = None,
    client_id: str = DEFAULT_CLIENT_ID,
    port: int = 0,
    open_browser: bool = True,
    out=sys.stderr,
) -> dict:
    issuer = issuer or discover_issuer(server)
    metadata = provider_metadata(issuer)
    payload = authorize(metadata, client_id, port=port, open_browser=open_browser, out=out)
    record = _credentials.record_from_token_response(
        payload, issuer=issuer, token_endpoint=metadata["token_endpoint"], client_id=client_id
    )
    _credentials.save(server, record)
    return record


def whoami(server: str) -> dict:
    token = _credentials.TokenSource(server).get()
    if token is None:
        raise LoginError(f"not signed in to {server}; run: cfds login {server}")
    status, payload = _get_json(server.rstrip("/") + "/v1/me", token)
    if status == 401:
        raise LoginError(
            f"the saved sign-in for {server} is no longer valid; run: cfds login {server}"
        )
    if status != 200:
        raise LoginError(f"{server} answered HTTP {status}")
    return payload


def _gib(value) -> str:
    return f"{value / 1024**3:.1f} GiB" if isinstance(value, (int, float)) else "?"


def _describe(me: dict) -> str:
    user = me.get("user") or {}
    who = user.get("email") or user.get("name") or user.get("sub")
    lines = [f"Signed in as {who}"]
    quota = me.get("quota")
    if quota:
        lines.append(f"Storage: {_gib(quota.get('used'))} used of {_gib(quota.get('limit'))}")
    for mount in me.get("mounts") or []:
        mode = "read-write" if mount.get("writable") else "read-only"
        lines.append(f"  {mount.get('path')}  ({mode}) {mount.get('description', '')}".rstrip())
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cfds", description="Sign in to a cfds storage server.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    login_cmd = commands.add_parser("login", help="sign in through the browser and save a token")
    login_cmd.add_argument("server", nargs="?", default=DEFAULT_SERVER, help="default: %(default)s")
    login_cmd.add_argument("--issuer", help="sign-in provider (default: asked from the server)")
    login_cmd.add_argument("--client-id", default=DEFAULT_CLIENT_ID, help="default: %(default)s")
    login_cmd.add_argument("--port", type=int, default=0, help="local callback port (default: any)")
    login_cmd.add_argument(
        "--no-browser", action="store_true", help="print the sign-in URL instead of opening it"
    )
    for name, text in (
        ("logout", "forget the saved token"),
        ("whoami", "show who you are signed in as, and your quota"),
        ("token", "print the current access token (for curl and other tools)"),
    ):
        command = commands.add_parser(name, help=text)
        command.add_argument(
            "server", nargs="?", default=DEFAULT_SERVER, help="default: %(default)s"
        )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    server = args.server.rstrip("/")
    try:
        if args.command == "login":
            login(
                server,
                issuer=args.issuer,
                client_id=args.client_id,
                port=args.port,
                open_browser=not args.no_browser,
            )
            try:
                print(_describe(whoami(server)))
            except LoginError as exc:
                print(
                    f"Signed in, but {server} did not accept the token yet: {exc}", file=sys.stderr
                )
            print(f"Token saved in {_credentials.credentials_path()}", file=sys.stderr)
        elif args.command == "logout":
            _credentials.save(server, None)
            print(f"Signed out of {server}")
        elif args.command == "whoami":
            print(_describe(whoami(server)))
        elif args.command == "token":
            token = _credentials.TokenSource(server).get()
            if token is None:
                raise LoginError(f"not signed in to {server}; run: cfds login {server}")
            print(token)
    except (LoginError, ValueError, OSError) as exc:
        print(f"cfds: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
