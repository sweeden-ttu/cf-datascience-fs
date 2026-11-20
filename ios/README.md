# Cloudfronts for iPhone

A SwiftUI iPhone app plus a **File Provider extension** that puts Cloudfronts storage in
the iOS Files app.

- **Sign up and sign in** at `datascientist.cloudfronts.group` through the OIDC provider
  `oidc.cloudfronts.group` (authorization code + PKCE, in `ASWebAuthenticationSession`).
  "Create free account" sends `prompt=create` so the provider opens its sign-up page.
- **Free tier:** every account gets 100 GiB. The app shows the quota from `GET /v1/me`.
- **Files app:** Browse → *Cloudfronts* shows
  - **Kaggle Datasets** (`/kaggle-datasets`, which is `/mnt/TMP/ai-rig/kaggle-datasets`
    on the drive), read-only, and
  - **My Files** (`/home`), read-write: open, edit, save, create folders, rename, move,
    delete, from any app.
- **In-app browser:** look through both folders and swipe a dataset file to
  *Copy to My Files* (a server-side copy, nothing is downloaded).

It talks to `cfds-server` using the v1 HTTP API in `cfds-api/API.md` (shared with the
Python client and the VS Code extension).

## Layout

| Path | What |
| --- | --- |
| `project.yml` | XcodeGen spec for the Xcode project (the `.xcodeproj` is generated) |
| `Config/Base.xcconfig` | Hosts, client id, bundle ids, app group. Override in `Config/Local.xcconfig` |
| `CFDSKit/` | Swift package used by both targets: API client, OIDC/PKCE, Keychain token store. Has unit tests |
| `CloudfrontsFS/` | The app (SwiftUI): sign-in, account and quota, folder browser |
| `FileProvider/` | The `NSFileProviderReplicatedExtension` behind the Files app location |

## Build on a Mac

Requirements: Xcode 15.3 or newer, iOS 16 or newer on the device.

```bash
brew install xcodegen
cd ios
cp Config/Local.xcconfig.example Config/Local.xcconfig   # set DEVELOPMENT_TEAM
xcodegen generate
open CloudfrontsFS.xcodeproj
```

Then pick the *CloudfrontsFS* scheme and run it on a device or simulator.

To ship it, the Apple developer account needs (Certificates, Identifiers & Profiles):

1. App IDs `com.cloudfronts.datascientist` and `com.cloudfronts.datascientist.FileProvider`.
2. The App Group `group.com.cloudfronts.datascientist`, enabled on both App IDs.
3. Keychain Sharing on both (the group `com.cloudfronts.datascientist.shared` is in the
   entitlements; Xcode adds the team prefix).

Change any of these names in `Config/Base.xcconfig`; nothing in the Swift code hard-codes them.

Run the package tests without Xcode's UI:

```bash
cd ios/CFDSKit && swift test
```

## What the identity provider needs

Register a **public** client (no secret, PKCE S256 required):

| Setting | Value |
| --- | --- |
| client_id | `cfds-ios` |
| Redirect URI | `com.cloudfronts.datascientist:/oauth2/callback` |
| Scopes | `openid profile email` |
| Grant types | `authorization_code` (plus `refresh_token` if you want users to stay signed in) |

The server must accept this client's access tokens (`aud`), see `API.md`.

If the provider issues no refresh tokens, the user signs in again when the access token
expires. The Files app then shows a *Sign in* prompt that opens the app; because the
browser session is shared with Safari, this is usually one tap.

## How the File Provider maps the API

| Files app action | API call |
| --- | --- |
| Open a folder | `GET /v1/ls?path=` |
| Open a file | `GET /v1/ls?path=&list=0`, then `GET /v1/file` with `If-Match: <etag>` |
| New file / save a new file | `PUT /v1/file?path=&overwrite=0` |
| Save changes | `PUT /v1/file?path=&create=0` with `If-Match: <etag the edit started from>` |
| New folder | `POST /v1/mkdir?path=` |
| Rename / move | `POST /v1/mv?src=&dst=&overwrite=0` |
| Delete | `DELETE /v1/file?path=[&recursive=1]` |

- Item identifiers are server paths; the item version is the server's ETag, so the
  system re-downloads a file exactly when it changed.
- Errors become the Files app's own: 401 → *Sign in*, 507 → *storage full*,
  403 `read_only` → no permission, 409 → name already taken, 412 → the system re-fetches
  and retries.
- The server has no change feed, so a folder is re-listed whenever the system asks for
  changes. Edits made elsewhere show up the next time a folder is opened or refreshed.
- Moving between *Kaggle Datasets* and *My Files* is a copy in the Files app (the server
  refuses cross-folder moves with `cross_mount`).

## Not done yet

- No app icon artwork (the asset catalog has an empty 1024 pt slot).
- No background upload/download sessions: large transfers run while the extension is
  alive, which iOS allows for File Provider work, but a very large upload can be
  interrupted and is then retried by the system.
- The access token is stored in the Keychain; the ID token's claims are decoded for
  display only and their signature is not checked on the device (the server validates
  every access token).
