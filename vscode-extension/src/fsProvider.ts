// vscode.FileSystemProvider for the cfds: scheme. A URI's path is the path the
// storage server shows the signed-in user, e.g. cfds:/kaggle-datasets/titanic/train.csv
// (shared, read-only; /mnt/TMP/ai-rig/kaggle-datasets on the server) or
// cfds:/home/notebooks/eda.ipynb (the user's own space).

import * as vscode from "vscode";
import { CfdsClient, CfdsError, Entry, formatBytes } from "./client";

export const SCHEME = "cfds";

function parentOf(uri: vscode.Uri): vscode.Uri {
  const p = uri.path.replace(/\/+$/, "");
  const i = p.lastIndexOf("/");
  return uri.with({ path: i <= 0 ? "/" : p.slice(0, i) });
}

function fileType(e: Entry): vscode.FileType {
  let t =
    e.type === "directory"
      ? vscode.FileType.Directory
      : e.type === "file"
        ? vscode.FileType.File
        : vscode.FileType.Unknown;
  if (e.islink) t |= vscode.FileType.SymbolicLink;
  return t;
}

/** Converts server and network errors into the FileSystemError kinds VS Code understands. */
export function toFsError(err: unknown, uri: vscode.Uri): Error {
  if (err instanceof vscode.FileSystemError) return err;
  if (err instanceof CfdsError) {
    switch (err.status) {
      case 404:
        return vscode.FileSystemError.FileNotFound(uri);
      case 409:
        if (err.code === "not_empty") {
          return new vscode.FileSystemError(`${uri.path} is not empty.`);
        }
        return vscode.FileSystemError.FileExists(uri);
      case 400:
        if (err.code === "is_a_directory") return vscode.FileSystemError.FileIsADirectory(uri);
        if (err.code === "not_a_directory") return vscode.FileSystemError.FileNotADirectory(uri);
        if (err.code === "cross_mount") {
          return vscode.FileSystemError.NoPermissions(
            `${uri.path}: files cannot be moved between your storage and the shared datasets. Copy instead.`,
          );
        }
        return new vscode.FileSystemError(`${uri.path}: ${err.message}`);
      case 401:
        return vscode.FileSystemError.NoPermissions(`Sign in to Cloudfronts to access ${uri.path}`);
      case 403:
        if (err.code === "read_only") {
          return vscode.FileSystemError.NoPermissions(`${uri.path} is read-only.`);
        }
        return vscode.FileSystemError.NoPermissions(`${uri.path}: ${err.message}`);
      case 412:
        return new vscode.FileSystemError(
          `${uri.path} was changed on the server since you opened it. Reopen the file to see the new version.`,
        );
      case 413:
      case 507:
        return vscode.FileSystemError.NoPermissions(
          `Storage quota exceeded: ${err.message}. Free up space or upgrade your plan.`,
        );
      case 502:
      case 503:
      case 504:
        return vscode.FileSystemError.Unavailable(uri);
      default:
        return new vscode.FileSystemError(`${uri.path}: ${err.message} (HTTP ${err.status})`);
    }
  }
  if (err instanceof vscode.CancellationError) return err;
  // fetch() network failures surface as TypeError("fetch failed").
  if (err instanceof TypeError) return vscode.FileSystemError.Unavailable(uri);
  return err instanceof Error ? err : new Error(String(err));
}

export class CfdsFileSystemProvider implements vscode.FileSystemProvider {
  private readonly emitter = new vscode.EventEmitter<vscode.FileChangeEvent[]>();
  readonly onDidChangeFile = this.emitter.event;
  /** ETag of the version each open file was read at, so saves cannot clobber newer server edits. */
  private readonly etags = new Map<string, string>();

  constructor(
    private readonly client: () => CfdsClient,
    private readonly maxFileBytes: () => number,
  ) {}

  private async run<T>(uri: vscode.Uri, fn: () => Promise<T>): Promise<T> {
    try {
      return await fn();
    } catch (err) {
      throw toFsError(err, uri);
    }
  }

  // No server push yet; VS Code still sees our own changes via the events below.
  watch(): vscode.Disposable {
    return new vscode.Disposable(() => undefined);
  }

  stat(uri: vscode.Uri): Promise<vscode.FileStat> {
    return this.run(uri, async () => {
      const e = await this.client().info(uri.path);
      const stat: vscode.FileStat = {
        type: fileType(e),
        size: e.size,
        mtime: Math.round(e.mtime * 1000),
        ctime: Math.round((e.ctime ?? e.mtime) * 1000),
      };
      if (e.writable === false) stat.permissions = vscode.FilePermission.Readonly;
      return stat;
    });
  }

  readDirectory(uri: vscode.Uri): Promise<[string, vscode.FileType][]> {
    return this.run(uri, async () => {
      const listing = await this.client().list(uri.path);
      if (!listing.entries) throw vscode.FileSystemError.FileNotADirectory(uri);
      return listing.entries.map((e): [string, vscode.FileType] => [
        e.name.slice(e.name.lastIndexOf("/") + 1),
        fileType(e),
      ]);
    });
  }

  readFile(uri: vscode.Uri): Promise<Uint8Array> {
    return this.run(uri, async () => {
      const client = this.client();
      const e = await client.info(uri.path);
      if (e.type === "directory") throw vscode.FileSystemError.FileIsADirectory(uri);
      const max = this.maxFileBytes();
      if (e.size > max) {
        throw new vscode.FileSystemError(
          `${uri.path} is ${formatBytes(e.size)}, over the ${formatBytes(max)} editor limit ` +
            `(setting cfds.maxFileSizeMB). Read it with the Python cfds client instead.`,
        );
      }
      const data = await client.read(uri.path, { ifMatch: e.etag });
      if (e.etag) this.etags.set(uri.path, e.etag);
      return data;
    });
  }

  writeFile(
    uri: vscode.Uri,
    content: Uint8Array,
    options: { readonly create: boolean; readonly overwrite: boolean },
  ): Promise<void> {
    return this.run(uri, async () => {
      const entry = await this.client().write(uri.path, content, {
        create: options.create,
        overwrite: options.overwrite,
        ifMatch: options.overwrite ? this.etags.get(uri.path) : undefined,
      });
      if (entry?.etag) this.etags.set(uri.path, entry.etag);
      else this.etags.delete(uri.path);
      this.emitter.fire([{ type: vscode.FileChangeType.Changed, uri }]);
    });
  }

  createDirectory(uri: vscode.Uri): Promise<void> {
    return this.run(uri, async () => {
      await this.client().mkdir(uri.path);
      this.emitter.fire([
        { type: vscode.FileChangeType.Changed, uri: parentOf(uri) },
        { type: vscode.FileChangeType.Created, uri },
      ]);
    });
  }

  delete(uri: vscode.Uri, options: { readonly recursive: boolean }): Promise<void> {
    return this.run(uri, async () => {
      await this.client().delete(uri.path, options.recursive);
      this.etags.delete(uri.path);
      this.emitter.fire([
        { type: vscode.FileChangeType.Changed, uri: parentOf(uri) },
        { type: vscode.FileChangeType.Deleted, uri },
      ]);
    });
  }

  copy(source: vscode.Uri, destination: vscode.Uri, options: { readonly overwrite: boolean }): Promise<void> {
    return this.run(source, async () => {
      await this.copyTree(source.path, destination.path, options.overwrite);
      this.emitter.fire([
        { type: vscode.FileChangeType.Changed, uri: parentOf(destination) },
        { type: vscode.FileChangeType.Created, uri: destination },
      ]);
    });
  }

  /** /v1/cp copies single files, so walk directories here. */
  private async copyTree(src: string, dst: string, overwrite: boolean): Promise<void> {
    const client = this.client();
    const listing = await client.list(src);
    if (listing.info.type !== "directory") {
      await client.copy(src, dst, overwrite);
      return;
    }
    await client.mkdir(dst, overwrite);
    for (const e of listing.entries ?? []) {
      const base = e.name.slice(e.name.lastIndexOf("/") + 1);
      await this.copyTree(`${src.replace(/\/+$/, "")}/${base}`, `${dst.replace(/\/+$/, "")}/${base}`, overwrite);
    }
  }

  rename(oldUri: vscode.Uri, newUri: vscode.Uri, options: { readonly overwrite: boolean }): Promise<void> {
    return this.run(oldUri, async () => {
      await this.client().move(oldUri.path, newUri.path, options.overwrite);
      this.etags.delete(oldUri.path);
      this.etags.delete(newUri.path);
      this.emitter.fire([
        { type: vscode.FileChangeType.Deleted, uri: oldUri },
        { type: vscode.FileChangeType.Created, uri: newUri },
      ]);
    });
  }
}
