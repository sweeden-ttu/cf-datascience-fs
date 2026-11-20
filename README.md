# cf-datascience-fs

CloudFronts Data Science file system: browse and read a remote storage drive, including
large blobs, from Python through [fsspec](https://filesystem-spec.readthedocs.io/).

There are two parts:

- **`cfds-server`** runs on the machine that has the drive (for example `/mnt/TMP`) and
  serves it **read-only** over HTTP. It needs only the Python standard library.
- **`cfds://`** is an fsspec filesystem that data scientists use from their own machines.
  Anything that understands fsspec URLs (pandas, pyarrow, Dask, xarray, ...) can read from it.
- **[`ios/`](ios/README.md)** is an iPhone app with a File Provider extension that shows the
  same storage in the iOS Files app, with sign-up and sign-in through Cloudfronts OIDC.

## Install

```bash
pip install "git+https://github.com/sweeden-ttu/cf-datascience-fs"
```

Install it on both the server and the client machines. Python 3.9 or newer.

## Serve the drive

```bash
cfds-server --root /mnt/TMP --host 0.0.0.0 --port 8765
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--root` | `$CFDS_ROOT` or `/mnt/TMP` | Directory to serve. Nothing outside it is readable. |
| `--host` | `127.0.0.1` | Address to bind. Use `0.0.0.0` to accept other machines. |
| `--port` | `8765` | TCP port. |
| `--token-file` | none | File holding a bearer token clients must send. `$CFDS_TOKEN` works too. |
| `--certfile`, `--keyfile` | none | PEM certificate and key. Turns on HTTPS. |

The default bind address is loopback on purpose: with `--host 0.0.0.0` and no token, anyone
who can reach the port can read everything under the root. For anything beyond a trusted
network, set a token **and** TLS (a token sent over plain HTTP can be sniffed).

## Read from it

```python
import fsspec
import pandas as pd

fs = fsspec.filesystem("cfds", host="drive-host", port=8765)

fs.ls("/")                        # browse
fs.glob("/datasets/**/*.parquet")
fs.info("/datasets/train.csv")    # size, type, mtime
fs.cat_file("/datasets/big.bin", start=0, end=1024)   # just the first KiB
fs.get("/datasets/train.csv", "train.csv")            # download

with fs.open("/datasets/big.bin", "rb") as f:         # seekable, reads by byte range
    f.seek(10_000_000)
    chunk = f.read(4096)

# Or let the library do it from a URL:
df = pd.read_csv("cfds://drive-host:8765/datasets/train.csv")
df = pd.read_parquet("cfds://drive-host:8765/datasets/table.parquet", columns=["id"])
```

Paths are relative to the served root: `/datasets/train.csv` is
`/mnt/TMP/datasets/train.csv` on the server.

Client options (pass to `fsspec.filesystem`, or as `storage_options=` in pandas):

| Option | Default | Meaning |
| --- | --- | --- |
| `host`, `port` | `localhost`, `8765` | Server address. Taken from the URL when you use one. |
| `token` | `$CFDS_TOKEN` | Bearer token, when the server requires one. |
| `use_ssl` | `False` | Use HTTPS. The `cfdss://` scheme sets this. |
| `verify` | `True` | `False`, or the path of a CA bundle, for HTTPS. |
| `timeout` | `60` | Seconds to wait for the server. |
| `retries` | `3` | Retries after connection failures and 502/503/504. |
| `block_size` | fsspec default (5 MiB) | Read-ahead size for `open()`. |
| `use_listings_cache` | `False` | Cache directory listings. Off because the drive can change. |

## Behaviour worth knowing

- **Read-only.** Every write (`open(..., "wb")`, `rm`, `mkdir`, `put`, `mv`, ...) raises
  `OSError` with `errno.EROFS`. The server refuses every method except `GET` and `HEAD`.
- **Confined to the root.** `..` in a path is rejected. Symlinks are followed, but a link
  whose target is outside the root is refused and is left out of listings.
- **Files that change mid-read are detected.** An open file is pinned to the version that
  was current when it was opened. If the file is modified on the server, the next read
  raises `FileChangedError` instead of mixing old and new bytes.
- **Only regular files are served.** Devices, FIFOs and sockets are listed as `other` and
  cannot be read.

## HTTP API

Both endpoints accept `GET` and `HEAD`. Errors are JSON:
`{"error": {"code": "...", "message": "..."}}`.

`GET /v1/ls?path=/datasets[&list=0]`

```json
{
  "info": {"name": "/datasets", "size": 0, "type": "directory", "mtime": 1700000000.0, "islink": false},
  "entries": [
    {"name": "/datasets/train.csv", "size": 24, "type": "file", "mtime": 1700000000.0,
     "islink": false, "etag": "\"17c2f0a1b2c3d4e5-18\""}
  ]
}
```

`entries` is `null` for a file, or when `list=0` asks for the `info` alone.

`GET /v1/file?path=/datasets/train.csv`

Returns the bytes. Send `Range: bytes=START-END` for part of the file (`206 Partial
Content`), and `If-Match: <etag>` to fail with `412` if the file has changed.

| Status | Meaning |
| --- | --- |
| 400 | Bad path (`..`, NUL), or `/v1/file` on a directory |
| 401 | Missing or wrong token |
| 403 | Outside the root, not a regular file, or no permission |
| 404 | No such file or directory |
| 412 | `If-Match` did not match: the file changed |
| 416 | Range starts at or after the end of the file |

## Develop

```bash
pip install -e ".[dev]"
pytest          # starts a real server on a temporary directory
ruff check . && ruff format --check .
```
