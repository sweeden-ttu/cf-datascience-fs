# cf-datascience-fs

CloudFronts Data Science file system: browse, read and write a remote storage drive,
including large blobs, from Python through [fsspec](https://filesystem-spec.readthedocs.io/).

There are three parts:

- **`cfds-server`** runs on the machine that has the drive. It serves either one directory
  (for example `/mnt/TMP`), or, for the hosted service, a private space per signed-in user
  plus a shared read-only dataset folder. The server code needs only the Python standard
  library (plus PyJWT when it checks sign-in tokens).
- **`cfds://` / `cfdss://`** is an fsspec filesystem for data scientists' own machines.
  Anything that understands fsspec URLs (pandas, pyarrow, Dask, xarray, ...) can read and
  write through it, from ordinary or `async` code.
- **`cfds login`** signs in through the browser and saves a token for the filesystem.

The HTTP protocol, shared with the VS Code extension and the iOS app, is in
[docs/API.md](docs/API.md). How the hosted service is set up is in
[docs/DEPLOY.md](docs/DEPLOY.md).
- **`cfds-server`** runs on the machine that has the drive (for example `/mnt/TMP`) and
  serves it **read-only** over HTTP. It needs only the Python standard library.
- **`cfds://`** is an fsspec filesystem that data scientists use from their own machines.
  Anything that understands fsspec URLs (pandas, pyarrow, Dask, xarray, ...) can read from it.
- **[`ios/`](ios/README.md)** is an iPhone app with a File Provider extension that shows the
  same storage in the iOS Files app, with sign-up and sign-in through Cloudfronts OIDC.

## Install

```bash
pip install "git+https://github.com/sweeden-ttu/cf-datascience-fs"            # client
pip install "cf-datascience-fs[server] @ git+https://github.com/sweeden-ttu/cf-datascience-fs"  # server with sign-in
```

Python 3.9 or newer.

## Use the hosted service

```bash
cfds login https://storage.cloudfronts.group     # opens the sign-in page; sign-up is free
cfds whoami                                      # who you are, your quota, what you can see
```

```python
import fsspec
import pandas as pd

fs = fsspec.filesystem("cfdss", host="storage.cloudfronts.group")

fs.ls("/")                                  # ['/home', '/kaggle-datasets']
fs.ls("/kaggle-datasets")                   # shared datasets, read-only
fs.me()["quota"]                            # {'limit': 107374182400, 'used': ..., 'available': ...}

df = pd.read_csv("cfdss://storage.cloudfronts.group/kaggle-datasets/titanic/train.csv")
df.to_parquet("cfdss://storage.cloudfronts.group/home/titanic/train.parquet")

with fs.open("/home/models/model.pkl", "wb") as f:   # uploaded when the file is closed
    f.write(blob)
fs.put("results/", "/home/results/", recursive=True)
fs.cp_file("/kaggle-datasets/titanic/train.csv", "/home/titanic/train.csv")  # copied on the server
fs.mv("/home/titanic", "/home/titanic-2024")
fs.rm("/home/scratch", recursive=True)
```

Every user sees the same layout:

| Path | What | Access |
| --- | --- | --- |
| `/kaggle-datasets` | `/mnt/TMP/ai-rig/kaggle-datasets` on the server, shared | read-only |
| `/home` | your own space, 100 GiB on the free tier | read-write |

Writing past the quota raises `QuotaExceededError` (`errno.EDQUOT`); writing under
`/kaggle-datasets` raises `PermissionError` (`errno.EROFS`).

### From async code

```python
import asyncio
from cf_datascience_fs import CFDataScienceAsyncFileSystem

async def main():
    fs = CFDataScienceAsyncFileSystem(host="storage.cloudfronts.group", use_ssl=True,
                                      asynchronous=True)
    session = await fs.set_session()
    try:
        names = await fs._ls("/kaggle-datasets", detail=False)
        blobs = await asyncio.gather(*(fs._cat_file(n, start=0, end=1024) for n in names))
        await fs._pipe_file("/home/heads.bin", b"".join(blobs))
    finally:
        await session.close()

asyncio.run(main())
```

### Client options

Pass them to `fsspec.filesystem`, or as `storage_options=` in pandas.

| Option | Default | Meaning |
| --- | --- | --- |
| `host`, `port` | `localhost`; 443 with TLS, else 8765 | Server address. Taken from the URL when you use one. |
| `url` | none | Instead of host/port: `https://storage.cloudfronts.group`. |
| `token` | see below | Bearer token, or a function returning one. |
| `use_ssl` | `False` | HTTPS. The `cfdss://` scheme sets this. |
| `verify` | `True` | `False`, or the path of a CA bundle, for HTTPS. |
| `timeout` | `60` | Seconds to wait to connect, or between bytes received. |
| `retries` | `3` | Retries after connection failures and 502/503/504 (requests that are safe to repeat). |
| `block_size` | fsspec default (5 MiB) | Read-ahead size for `open()` in read mode. |
| `auto_mkdir` | `True` | Create missing parent directories when writing or copying a file. |
| `use_listings_cache` | `False` | Cache directory listings. Off because the drive can change. |

The token is, in order: the `token` argument; `$CFDS_TOKEN` (read on every request);
the token saved by `cfds login` for that server (refreshed when it expires, if the
provider gave a refresh token). A rejected token is refreshed and the request retried
once. `cfds token` prints the current token, for `curl` and other tools; `cfds logout`
forgets it. Tokens are stored in `~/.config/cfds/credentials.json` (mode 600;
`$CFDS_CONFIG_DIR` overrides the directory).

`CFDataScienceFileSystem` is the original synchronous client built on `requests`. It is
read-only and still works; `cfds://` now resolves to `CFDataScienceAsyncFileSystem`.

## Serve a directory yourself

```bash
cfds-server --root /mnt/TMP --host 0.0.0.0 --port 8765              # read-only
cfds-server --root /srv/scratch --writable --token-file token.txt   # read-write, shared token
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--root` | `$CFDS_ROOT` or `/mnt/TMP` | Directory to serve at `/`. Nothing outside it is reachable. |
| `--writable` | off | Allow uploads, deletes, renames and copies under `--root`. |
| `--host` | `127.0.0.1` | Address to bind. Use `0.0.0.0` to accept other machines. |
| `--port` | `8765` | TCP port. |
| `--token-file` | none | File holding a bearer token clients must send. `$CFDS_TOKEN` works too. |
| `--certfile`, `--keyfile` | none | PEM certificate and key. Turns on HTTPS (TLS 1.2 or newer). |
| `--allowed-host` | none | Extra DNS name clients use for this server, when no token is set. Repeatable. |
| `--cors-origin` | vscode.dev when sign-in is on, else none | Browser origin allowed to call the API. Repeatable; `https://*.example.com` matches subdomains. |
| `--max-connections` | `256` | Connections served at once. More are dropped. |
| `--log-level` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`. |

Multi-user mode (what the hosted service runs) adds:

| Option | Default | Meaning |
| --- | --- | --- |
| `--users-root` | none | Holds one private folder per user. Turns on multi-user mode (`--root` is ignored). |
| `--datasets-root` | `/mnt/TMP/ai-rig/kaggle-datasets` | Shown read-only to everyone at `/kaggle-datasets`. |
| `--oidc-issuer` | none | Require access tokens from this OpenID Connect provider. |
| `--audience` | `cfds-vscode`, `cfds-ios`, `cfds-cli` | Accepted token audiences (client IDs). Repeatable. |
| `--default-quota` | `100G` | Storage per user. |
| `--datasets-writer-role` | none | Users whose token carries this role may write to `/kaggle-datasets`. |
| `--public-url` | from each request | Address clients use, for the OAuth metadata the server publishes. |

The default bind address is loopback on purpose: with `--host 0.0.0.0` and no token,
anyone who can reach the port can read (and with `--writable`, change) everything under
the root. For anything beyond a trusted network, require a token or sign-in **and** use
TLS. Tokens may use letters, digits and `-._~+/=`; generate one with
`python -c "import secrets; print(secrets.token_urlsafe(32))"`.

Without a token, the server also checks the `Host` header, so that a web page on another
site cannot reach the drive through a colleague's browser (DNS rebinding). IP addresses,
single-label names (`drive-host`) and the machine's own host names always work. If people
reach the server through some other DNS name, add `--allowed-host drive.example.org`.

## Behaviour worth knowing

- **Writes are atomic.** An upload goes to a hidden temporary file next to the target and
  is renamed into place once every byte has arrived, so readers see the old file or the
  new one, never half of one. A file opened for writing is uploaded when it is closed.
  `open(path, "xb")` and `pipe_file(..., mode="create")` never replace an existing file.
  Appending (`"ab"`) is not supported.
- **Quota is checked before the upload starts** when the size is known, so a too-large
  upload fails at once instead of after sending everything.
- **Confined to the root.** `..` in a path is rejected. Symlinks are followed for reading,
  but a link whose target is outside the root is refused and left out of listings.
  Writes and deletes act on a link itself, never on what it points to.
- **Files that change mid-read are detected.** An open file is pinned to the version that
  was current when it was opened. If the file is modified, truncated or removed on the
  server, the read raises `FileChangedError` instead of returning a mix of old and new
  bytes. Detection uses the file's modification time, size and inode, so a rewrite that
  keeps the same size *and* lands within the same timestamp tick cannot be told apart.
- **Downloads are all or nothing.** `get` and `get_file` write to a temporary `.part` file
  and rename it into place only once every byte has arrived.
- **Errors are not mistaken for missing files.** A wrong token or an unreachable server
  raises `PermissionError` or `ConnectionError` from `exists`, `isdir`, `isfile`, `find`,
  `glob`, `du` and `walk`, instead of reporting an empty drive. Pass `on_error="omit"` to
  `walk` for fsspec's usual skip-on-error behaviour.
- **Symlinked directories are listed but not entered by recursive operations** (`find`,
  `glob("**")`, `walk`, `du`, `get(recursive=True)`), as `os.walk` does.
- **Only regular files are served.** Devices, FIFOs and sockets are listed as `other`;
  opening or reading one raises `PermissionError`.

## Develop

```bash
pip install -e ".[dev]"
pytest          # starts real servers on temporary directories, with a stand-in sign-in provider
ruff check . && ruff format --check .
```
