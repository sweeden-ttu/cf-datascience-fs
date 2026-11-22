# cfds HTTP API, version 1

This is the contract between `cfds-server` and its clients: the Python `cfds://`
filesystem, the VS Code extension and the iOS app. Status: **v1, 2026-11-20** (implemented in `cfds-server` 0.2.0).
Additive changes (new fields, new endpoints) can happen without notice; anything that
changes or removes behaviour below gets a new prefix (`/v2`).

## Base URL and auth

All endpoints live under one origin, for example `https://storage.cloudfronts.group`
(a deployment detail; clients should make it configurable).

Every `/v1/*` request needs `Authorization: Bearer <access token>`.

- Tokens come from the OIDC provider `https://oidc.cloudfronts.group`
  (discovery: `/.well-known/openid-configuration`). Use the authorization code flow
  with PKCE (S256) as a public client (no secret). Request scopes `openid profile email`.
- The server validates the access token's signature against the issuer's JWKS
  (RS256), and checks `iss`, `exp`/`nbf`, and that `aud` is one of the registered
  cfds clients. Tokens that are not JWTs are checked against the issuer's
  `userinfo` endpoint instead (cached for a few minutes).
- Client IDs (to be registered at the provider as public clients, PKCE required):

  | Client | client_id | Redirect URIs |
  | --- | --- | --- |
  | VS Code extension | `cfds-vscode` | `vscode://cloudfronts.cfds/auth`, `vscode-insiders://cloudfronts.cfds/auth` |
  | iOS app | `cfds-ios` | `groups.cloudfronts.datascientist:/oauth2/callback` |
  | Python (`cfds login`) | `cfds-cli` | `http://127.0.0.1/callback` (any port) |

- Users sign up at the provider (free). The first authenticated request creates the
  user's space on the server with the free tier quota (100 GiB = 107374182400 bytes).
- `401` responses carry `WWW-Authenticate: Bearer realm="cfds", error="invalid_token",
  resource_metadata="<origin>/.well-known/oauth-protected-resource"` (`error=` only when
  a token was sent). On a 401 the client should refresh its token (or sign in again)
  and retry once.
- If the server cannot reach the provider to check a token, it answers `503
  auth_unavailable` with `Retry-After`. That is not a reason to sign in again.

`GET /.well-known/oauth-protected-resource` (no auth, RFC 9728) tells a client which
provider to use:

```json
{"resource": "https://storage.cloudfronts.group",
 "authorization_servers": ["https://oidc.cloudfronts.group"],
 "scopes_supported": ["openid", "profile", "email"],
 "bearer_methods_supported": ["header"]}
```

### CORS

Browser clients (the VS Code web build on vscode.dev) need CORS. The server answers
`OPTIONS` preflights on `/v1/*` and `/.well-known/*` without auth, for the configured
origins only (defaults: `https://vscode.dev`, `https://insiders.vscode.dev` and any
`https://*.vscode-cdn.net`):

- `Access-Control-Allow-Origin: <the request's Origin>` and `Vary: Origin`
- `Access-Control-Allow-Methods: GET, HEAD, PUT, POST, DELETE`
- `Access-Control-Allow-Headers: Authorization, Range, If-Match, If-None-Match, If-Range, Content-Type`
- `Access-Control-Expose-Headers: ETag, Content-Length, Content-Range, Last-Modified, Accept-Ranges, WWW-Authenticate`
- `Access-Control-Max-Age: 600`

No cookies are used, so `Access-Control-Allow-Credentials` is never sent.

## What each user sees

Every user gets the same layout. Paths are POSIX style, always absolute, no `..`.

| Path | Backed by | Access |
| --- | --- | --- |
| `/` | virtual, lists the two entries below | read-only |
| `/kaggle-datasets` | `/mnt/TMP/ai-rig/kaggle-datasets` on the server, shared | read-only |
| `/home` | the user's own private space | read-write, counts against quota |

Clients should not hard-code these: read `home`, `default_path` and `mounts` from
`GET /v1/me`. `default_path` is where a client should open by default.

## Entries

Listings and write responses describe files and directories the same way:

```json
{"name": "/home/models/model.pkl", "size": 1048576, "type": "file",
 "mtime": 1763600000.123, "islink": false, "writable": true,
 "etag": "\"18e95...-100000-2a1\""}
```

- `type` is `file`, `directory` or `other` (devices, sockets; never readable).
- `size` is 0 for anything that is not a file. `etag` is present for files only; it
  changes whenever the content changes. Treat it as opaque.
- `writable` tells whether the caller can change this path.

## Errors

Every error is JSON: `{"error": {"code": "<code>", "message": "<human text>"}}`.
Clients should branch on `code` (and status), never on `message`.

| Status | code | When |
| --- | --- | --- |
| 400 | `bad_request` | malformed path (`..`, NUL), bad parameter, malformed request |
| 400 | `is_a_directory` | file operation on a directory (`GET`/`PUT /v1/file`, `cp` source) |
| 400 | `not_a_directory` | a path component that should be a directory is a file |
| 400 | `cross_mount` | `mv` between `/home` and `/kaggle-datasets` (copy instead) |
| 400 | `invalid_move` | moving a directory into itself |
| 401 | `unauthorized` | missing, expired or invalid token |
| 403 | `read_only` | write to a read-only location (`/`, `/kaggle-datasets/**`, mount roots) |
| 403 | `forbidden` | special file, or the OS denied access |
| 404 | `not_found` | no such file or directory |
| 404 | `parent_not_found` | parent directory of the target does not exist |
| 405 | `method_not_allowed` | wrong method for the endpoint (`Allow` lists the right ones) |
| 409 | `exists` | target exists and `overwrite=0` (or `mkdir` without `parents=1`) |
| 409 | `not_empty` | deleting a non-empty directory without `recursive=1` |
| 409 | `source_changed` | the `cp` source changed while it was being copied |
| 412 | `precondition_failed` | `If-Match` / `If-None-Match` did not hold |
| 416 | `range_not_satisfiable` | read range starts at or after the end of the file |
| 503 | `auth_unavailable` | the sign-in provider cannot be reached to check the token |
| 507 | `quota_exceeded` | the write would take the user over their quota |

A single-tree server started without `--writable` (not the hosted service) answers
every write with `405 method_not_allowed` and `Allow: GET, HEAD`.

## Reading

### `GET /v1/ls?path=P[&list=0]`

`{"info": <entry>, "entries": [<entry>, ...] | null}`. `entries` is `null` for a
file or with `list=0`. Entries are sorted by name.

### `GET /v1/file?path=P` (and `HEAD`)

The bytes, with `Content-Type: application/octet-stream`, `ETag`, `Last-Modified`
and `Accept-Ranges: bytes`.

- `Range: bytes=A-B` / `bytes=A-` / `bytes=-N` gives `206` with `Content-Range`.
- `If-Match: <etag>` gives `412` if the file changed. `If-Range` works per RFC 9110.
- If the file changes while it is being sent, the server closes the connection
  before `Content-Length` is reached: treat a short body as "file changed".

### `GET /v1/me` (and `HEAD`)

```json
{"user": {"sub": "alice", "email": "alice@example.com", "name": "Alice",
          "roles": ["free"]},
 "quota": {"limit": 107374182400, "used": 52428800, "available": 107321753600},
 "home": "/home",
 "datasets": "/kaggle-datasets",
 "default_path": "/kaggle-datasets",
 "mounts": [
   {"path": "/home", "writable": true, "description": "Your files"},
   {"path": "/kaggle-datasets", "writable": false, "description": "Kaggle datasets (shared, read-only)"}]}
```

`email` and `name` may be `null` when the provider does not supply them (the server
asks the provider's `userinfo` endpoint when the token has neither); `roles` comes from
the token's `roles` claim and may be empty. `mounts` is sorted by path.

## Writing

Writes are atomic: a new file appears under its name only once all its bytes have
arrived, and a failed or cancelled upload leaves the old file (if any) unchanged.

### `PUT /v1/file?path=P[&overwrite=0][&create=0]`

Body: the whole file, raw bytes. Send `Content-Length`, or `Transfer-Encoding: chunked`
when the length is unknown. `Expect: 100-continue` is honoured: the server checks
auth, path, preconditions and quota before asking for the body.

- `overwrite=0`: fail with `409 exists` if something is already at `P`.
- `create=0`: fail with `404 not_found` if nothing is at `P` yet.
- `If-Match: <etag>`: replace only if the current file has that ETag (`412` otherwise).
- `If-None-Match: *`: create only (`412` if it exists).
- The parent directory must exist (`404 parent_not_found`); create it with `mkdir`.
- Response: `201 Created` (new file) or `200 OK` (replaced). The body is the new
  `<entry>`, and the `ETag` header is set.

### `POST /v1/mkdir?path=P[&parents=1]`

Creates a directory and returns `201` with its `<entry>`. Without `parents=1`:
`409 exists` if anything is at `P`, `404 parent_not_found` if the parent is missing.
With `parents=1`: creates missing parents, and returns `200` if `P` already is a
directory.

### `DELETE /v1/file?path=P[&recursive=1]`

Deletes a file, or a directory (which needs `recursive=1` unless it is empty,
otherwise `409 not_empty`). Returns `204 No Content`. `If-Match` is honoured for
files. Mount roots and `/` cannot be deleted (`403 read_only`).

### `POST /v1/mv?src=A&dst=B[&overwrite=0|1]`

Renames within one mount (atomic). Default `overwrite=0`: `409 exists` if `B`
exists. With `overwrite=1` a file may replace a file; a directory is never replaced
(`409 exists`). Returns `200` with the `<entry>` for `B`.

### `POST /v1/cp?src=A&dst=B[&overwrite=0|1]`

Server-side copy of one **file**. `A` may be anywhere readable (for example under
`/kaggle-datasets`); `B` must be writable. Default `overwrite=0` (`409 exists` if `B`
exists). Counts against quota. Returns `201`/`200` with the `<entry>` for `B` and its
`ETag`. Directories give `400 is_a_directory` (copy file by file). If `A` changes
during the copy: `409 source_changed`, and `B` is left as it was.

## Notes

- Clients should send `User-Agent: <client>/<version>`.
- Only `PUT /v1/file` takes a request body; send none with other requests.
- Quota counts the apparent size of the files under `/home`.
- Every endpoint answers `OPTIONS` (see CORS); a wrong method gives `405` with `Allow`.
