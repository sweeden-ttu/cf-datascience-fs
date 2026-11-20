# Cloudfronts Data Science Storage for VS Code

Browse, open and edit your Cloudfronts storage right in the VS Code Explorer, next to
the shared Kaggle datasets. Files open in normal editors and notebooks, and saving
writes straight back to the server. No sync folder, no copy on your disk.

- **Free Tier: 100 GB** of private storage for every account. Sign up for free at
  [datascientist.cloudfronts.group](https://datascientist.cloudfronts.group).
- **Kaggle datasets** are mounted read-only at `cfds:/kaggle-datasets` for everyone.
- **Works where you work:** desktop VS Code, Remote-SSH, Dev Containers and
  Kubernetes pods (the extension runs on your machine, so the pod needs no network
  route or credentials of its own), and vscode.dev.

## Get started

1. Run **Cloudfronts: Sign Up (Free 100 GB)** or **Cloudfronts: Sign In** from the
   Command Palette. Your browser opens the Cloudfronts sign-in page
   (`oidc.cloudfronts.group`); when you finish, you land back in VS Code.
2. Two folders appear in the Explorer: **Kaggle datasets** and **My storage**.
3. Open, edit, save, drag and drop, rename, copy and delete as with local files. Copying
   a dataset into **My storage** happens on the server, so nothing is downloaded.

The status bar shows how much of your quota you have used. Click it for details.

## Paths

A `cfds:` URI's path is what the storage server shows you:

| URI | What it is | Access |
| --- | --- | --- |
| `cfds:/kaggle-datasets/...` | Shared Kaggle datasets | read-only |
| `cfds:/home/...` | Your own files | read-write, counts toward your quota |

The same paths work from Python with the `cfds://` fsspec filesystem
(`pip install cf-datascience-fs`), so a notebook can read what you see in VS Code.

## Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `cfds.serverUrl` | `https://storage.cloudfronts.group` | Storage server |
| `cfds.oidc.issuer` | `https://oidc.cloudfronts.group` | Sign-in provider |
| `cfds.oidc.clientId` | `cfds-vscode` | OAuth client id |
| `cfds.oidc.scopes` | `openid profile email` | Scopes requested at sign-in |
| `cfds.signUpUrl` | `https://datascientist.cloudfronts.group/signup` | Fallback sign-up page |
| `cfds.defaultFolders` | `[]` (server's mounts) | Folders added by **Open Cloudfronts Storage** |
| `cfds.maxFileSizeMB` | `512` | Larger files are listed but not opened in an editor |

**Headless use (CI, Kubernetes jobs, code-server):** set the `CFDS_TOKEN` environment
variable to an access token and the extension uses it instead of the browser sign-in.

## Behaviour worth knowing

- **Saves cannot overwrite someone else's newer change.** Each save sends the version
  you opened (`If-Match`); if the file changed on the server since, the save fails and
  asks you to reopen.
- **Quota:** a write that would take you over your limit is refused with a clear
  message, and the old file stays as it was. Writes are atomic.
- **Moving between `/home` and `/kaggle-datasets`** is not possible; copy instead.
- Very large files: VS Code reads a whole file into memory to open it, so files over
  `cfds.maxFileSizeMB` are refused in the editor. Use the Python client for those.
- There is no live push of changes made elsewhere yet: use **Refresh Explorer** to
  see them.

## Privacy

The extension talks only to the storage server and sign-in provider you configure.
Tokens are kept in VS Code's secret storage (your OS keychain).
