import * as vscode from "vscode";
import { AUTH_ID, CloudfrontsAuthProvider, makeTokenSource } from "./auth";
import { CfdsClient, CfdsError, DEFAULT_DATASETS_PATH, formatBytes, Me } from "./client";
import { CfdsFileSystemProvider, SCHEME } from "./fsProvider";

let cachedClient: { url: string; client: CfdsClient } | undefined;

export function activate(context: vscode.ExtensionContext) {
  const auth = new CloudfrontsAuthProvider(context);
  context.subscriptions.push(auth);
  const tokens = makeTokenSource(auth);
  const version = String(context.extension.packageJSON.version ?? "0");

  const client = (): CfdsClient => {
    const url = vscode.workspace.getConfiguration("cfds").get<string>("serverUrl", "https://storage.cloudfronts.group");
    if (cachedClient?.url !== url) {
      cachedClient = {
        url,
        client: new CfdsClient({ baseUrl: url, getToken: tokens, userAgent: `cfds-vscode/${version}` }),
      };
    }
    return cachedClient.client;
  };
  const maxBytes = () =>
    vscode.workspace.getConfiguration("cfds").get<number>("maxFileSizeMB", 512) * 1024 * 1024;

  context.subscriptions.push(
    vscode.workspace.registerFileSystemProvider(SCHEME, new CfdsFileSystemProvider(client, maxBytes), {
      isCaseSensitive: true,
    }),
  );

  // Status bar: storage used / Free Tier limit.
  const status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 50);
  status.command = "cfds.showQuota";
  context.subscriptions.push(status);
  const refreshStatus = async () => {
    const sessions = await auth.getSessions();
    if (!sessions.length && !envToken()) {
      status.text = "$(cloud) Cloudfronts: Sign in";
      status.command = "cfds.signIn";
      status.tooltip = "Sign in to browse your Cloudfronts storage";
      status.show();
      return;
    }
    status.command = "cfds.showQuota";
    try {
      const me = await client().me();
      status.text = quotaText(me);
      status.tooltip = `Signed in as ${me.user.email ?? me.user.name ?? me.user.sub}`;
    } catch {
      status.text = "$(cloud) Cloudfronts";
      status.tooltip = "Cloudfronts storage";
    }
    status.show();
  };
  context.subscriptions.push(vscode.authentication.onDidChangeSessions((e) => {
    if (e.provider.id === AUTH_ID) void refreshStatus();
  }));
  void refreshStatus();

  context.subscriptions.push(
    vscode.commands.registerCommand("cfds.signIn", async () => {
      await vscode.authentication.getSession(AUTH_ID, [], { createIfNone: true });
      await openStorage(client);
    }),

    vscode.commands.registerCommand("cfds.signUp", async () => {
      const choice = await vscode.window.showInformationMessage(
        "Create a free Cloudfronts account: 100 GB of storage plus the shared Kaggle datasets.",
        "Sign up in browser",
        "I already have an account",
      );
      if (choice === "Sign up in browser") {
        // Ask the issuer for its registration page (OIDC prompt=create). The user
        // lands back in VS Code signed in once the account exists.
        try {
          await auth.login([], true);
        } catch (err) {
          if (err instanceof vscode.CancellationError) return;
          // Issuer does not support prompt=create: open the sign-up site instead.
          const url = vscode.workspace.getConfiguration("cfds").get<string>("signUpUrl")!;
          await vscode.env.openExternal(vscode.Uri.parse(url));
          return;
        }
        await openStorage(client);
      } else if (choice === "I already have an account") {
        await vscode.commands.executeCommand("cfds.signIn");
      }
    }),

    vscode.commands.registerCommand("cfds.signOut", async () => {
      for (const s of await auth.getSessions()) await auth.removeSession(s.id);
      vscode.window.showInformationMessage("Signed out of Cloudfronts.");
    }),

    vscode.commands.registerCommand("cfds.open", () => openStorage(client)),

    vscode.commands.registerCommand("cfds.showQuota", async () => {
      try {
        const me = await client().me();
        const q = me.quota;
        const msg = q
          ? `${me.user.email ?? me.user.sub}: ${formatBytes(q.used)} of ${formatBytes(q.limit)} used (${((q.used / q.limit) * 100).toFixed(1)}%).`
          : `${me.user.email ?? me.user.sub}: no quota reported.`;
        vscode.window.showInformationMessage(msg);
      } catch (err) {
        vscode.window.showErrorMessage(`Could not read storage usage: ${describe(err)}`);
      }
    }),
  );
}

function envToken(): string | undefined {
  return typeof process !== "undefined" ? process.env?.CFDS_TOKEN : undefined;
}

export function deactivate() {
  cachedClient = undefined;
}

function quotaText(me: Me): string {
  if (!me.quota) return "$(cloud) Cloudfronts";
  return `$(cloud) ${formatBytes(me.quota.used)} / ${formatBytes(me.quota.limit)}`;
}

function describe(err: unknown): string {
  if (err instanceof CfdsError) return `${err.message} (HTTP ${err.status})`;
  return err instanceof Error ? err.message : String(err);
}

/**
 * Adds the user's folders to the workspace: the server's mounts from /v1/me
 * (the shared Kaggle datasets, read-only, and the user's own home), unless
 * cfds.defaultFolders overrides it. The default_path mount goes first.
 */
async function openStorage(client: () => CfdsClient) {
  const configured = vscode.workspace.getConfiguration("cfds").get<string[]>("defaultFolders", []);
  let folders: { path: string; name: string }[];
  if (configured.length) {
    folders = configured.map((p) => ({ path: p, name: `Cloudfronts ${p}` }));
  } else {
    let me: Me;
    try {
      me = await client().me();
    } catch (err) {
      if (err instanceof vscode.CancellationError) return;
      vscode.window.showErrorMessage(`Could not reach Cloudfronts storage: ${describe(err)}`);
      return;
    }
    const mounts = me.mounts?.length
      ? me.mounts
      : [
          { path: me.datasets ?? DEFAULT_DATASETS_PATH, writable: false, description: "Kaggle datasets" },
          ...(me.home ? [{ path: me.home, writable: true, description: "Your files" }] : []),
        ];
    const first = me.default_path ?? DEFAULT_DATASETS_PATH;
    mounts.sort((a, b) => Number(b.path === first) - Number(a.path === first));
    folders = mounts.map((m) => ({
      path: m.path,
      name: m.path === me.home ? "My storage" : m.path === (me.datasets ?? DEFAULT_DATASETS_PATH) ? "Kaggle datasets" : (m.description ?? m.path),
    }));
  }

  const existing = new Set(
    (vscode.workspace.workspaceFolders ?? [])
      .filter((f) => f.uri.scheme === SCHEME)
      .map((f) => f.uri.path),
  );
  const toAdd = folders
    .filter((f) => !existing.has(f.path))
    .map((f) => ({ uri: vscode.Uri.from({ scheme: SCHEME, path: f.path }), name: f.name }));
  if (!toAdd.length) {
    await vscode.commands.executeCommand("workbench.view.explorer");
    return;
  }
  const start = vscode.workspace.workspaceFolders?.length ?? 0;
  vscode.workspace.updateWorkspaceFolders(start, 0, ...toAdd);
}
