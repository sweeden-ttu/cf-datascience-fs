"""Starts the real multi-user cfds-server plus a stand-in OIDC provider for the
VS Code end-to-end test. Prints one JSON line {"server": url, "issuer": url} and
serves until killed.

Needs the server package and its tests on PYTHONPATH (src/ and tests/ of a
checkout with multi-user support) and PyJWT[crypto].
"""

import json
import sys
import tempfile
import threading
from pathlib import Path

from fake_oidc import ISSUER, FakeProvider, IdPHandler

from cf_datascience_fs._auth import OIDCAuth
from cf_datascience_fs.server import make_server

tmp = Path(tempfile.mkdtemp(prefix="cfds-e2e-"))
datasets = tmp / "kaggle-datasets"
(datasets / "titanic").mkdir(parents=True)
(datasets / "titanic" / "train.csv").write_text("id,survived\n1,0\n2,1\n")
users = tmp / "users"
users.mkdir()

provider = FakeProvider()
# The browser half of sign-in: /authorize and /token, issuing tokens for cfds-vscode.
idp = IdPHandler(provider, client_id="cfds-vscode").start()

auth = OIDCAuth(ISSUER, ("cfds-vscode",), fetch=provider.fetch)
server = make_server(
    None,
    host="127.0.0.1",
    port=0,
    auth=auth,
    users_root=users,
    datasets_root=datasets,
    public_url="http://127.0.0.1",
)
threading.Thread(target=server.serve_forever, daemon=True).start()
print(json.dumps({"server": f"http://127.0.0.1:{server.server_address[1]}", "issuer": idp.url, "users": str(users)}), flush=True)
sys.stdin.read()  # until the parent closes our stdin or kills us
