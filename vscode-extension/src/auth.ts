// "Cloudfronts" authentication provider: OIDC authorization code + PKCE against
// oidc.cloudfronts.group. The browser redirects back into VS Code through the
// extension's URI handler (vscode://cloudfronts.cfds/auth). That also works in
// Remote/Kubernetes windows, because the URI handler runs on the local client.

import * as vscode from "vscode";
import {
  authorizationUrl,
  codeChallenge,
  decodeJwtPayload,
  discover,
  Discovery,
  exchangeCode,
  randomString,
  refresh,
  TokenSet,
} from "./oidc";

export const AUTH_ID = "cloudfronts";
const SECRET_KEY = "cfds.oidc.session";
const CALLBACK_PATH = "/auth";
const LOGIN_TIMEOUT_MS = 5 * 60 * 1000;
/** Refresh this long before the access token expires. */
const EXPIRY_SKEW_MS = 60 * 1000;

interface StoredSession {
  id: string;
  tokens: TokenSet;
  account: { id: string; label: string };
  scopes: string[];
}

class UriEventHandler extends vscode.EventEmitter<vscode.Uri> implements vscode.UriHandler {
  handleUri(uri: vscode.Uri) {
    this.fire(uri);
  }
}

function settings() {
  const c = vscode.workspace.getConfiguration("cfds");
  return {
    issuer: c.get<string>("oidc.issuer", "https://oidc.cloudfronts.group"),
    clientId: c.get<string>("oidc.clientId", "cfds-vscode"),
    scopes: c.get<string[]>("oidc.scopes", ["openid", "profile", "email"]),
  };
}

export class CloudfrontsAuthProvider implements vscode.AuthenticationProvider, vscode.Disposable {
  private readonly changeEmitter =
    new vscode.EventEmitter<vscode.AuthenticationProviderAuthenticationSessionsChangeEvent>();
  readonly onDidChangeSessions = this.changeEmitter.event;

  private readonly uriHandler = new UriEventHandler();
  private readonly disposables: vscode.Disposable[] = [];
  private discovery?: { issuer: string; doc: Discovery };
  private refreshing?: Promise<StoredSession | undefined>;

  constructor(private readonly context: vscode.ExtensionContext) {
    this.disposables.push(
      vscode.authentication.registerAuthenticationProvider(AUTH_ID, "Cloudfronts", this, {
        supportsMultipleAccounts: false,
      }),
      vscode.window.registerUriHandler(this.uriHandler),
      this.uriHandler,
      this.changeEmitter,
      // Another window signed in or out: tell VS Code our sessions changed.
      context.secrets.onDidChange((e) => {
        if (e.key === SECRET_KEY) this.changeEmitter.fire({ added: [], removed: [], changed: [] });
      }),
    );
  }

  dispose() {
    this.disposables.forEach((d) => d.dispose());
  }

  private async loadStored(): Promise<StoredSession | undefined> {
    const raw = await this.context.secrets.get(SECRET_KEY);
    if (!raw) return undefined;
    try {
      return JSON.parse(raw) as StoredSession;
    } catch {
      return undefined;
    }
  }

  private async store(s: StoredSession | undefined) {
    if (s) await this.context.secrets.store(SECRET_KEY, JSON.stringify(s));
    else await this.context.secrets.delete(SECRET_KEY);
  }

  private async getDiscovery(): Promise<Discovery> {
    const { issuer } = settings();
    if (this.discovery?.issuer !== issuer) {
      this.discovery = { issuer, doc: await discover(issuer) };
    }
    return this.discovery.doc;
  }

  private toSession(s: StoredSession): vscode.AuthenticationSession {
    return { id: s.id, accessToken: s.tokens.accessToken, account: s.account, scopes: s.scopes };
  }

  async getSessions(): Promise<vscode.AuthenticationSession[]> {
    const s = await this.validStored(false);
    return s ? [this.toSession(s)] : [];
  }

  /**
   * The stored session with an unexpired access token, refreshing it when it is
   * close to expiry (or when `force` is set, e.g. after the server said 401).
   * Returns undefined when there is no session or the refresh token was rejected.
   */
  async validStored(force: boolean): Promise<StoredSession | undefined> {
    const s = await this.loadStored();
    if (!s) return undefined;
    if (!force && s.tokens.expiresAt - EXPIRY_SKEW_MS > Date.now()) return s;
    if (!s.tokens.refreshToken) return force ? undefined : s;
    this.refreshing ??= (async () => {
      try {
        const doc = await this.getDiscovery();
        const tokens = await refresh(doc, {
          clientId: settings().clientId,
          refreshToken: s.tokens.refreshToken!,
        });
        const next = { ...s, tokens };
        await this.store(next);
        this.changeEmitter.fire({ added: [], removed: [], changed: [this.toSession(next)] });
        return next;
      } catch (err) {
        console.warn("[cfds] token refresh failed", err);
        await this.store(undefined);
        this.changeEmitter.fire({ added: [], removed: [this.toSession(s)], changed: [] });
        return undefined;
      } finally {
        this.refreshing = undefined;
      }
    })();
    return this.refreshing;
  }

  async createSession(scopes: readonly string[]): Promise<vscode.AuthenticationSession> {
    return this.login(scopes, false);
  }

  /** Interactive sign-in. `signUp` asks the issuer to show its registration page first. */
  async login(scopes: readonly string[], signUp: boolean): Promise<vscode.AuthenticationSession> {
    const cfg = settings();
    const wanted = Array.from(new Set([...cfg.scopes, ...scopes]));
    const doc = await this.getDiscovery();

    const callback = vscode.Uri.parse(
      `${vscode.env.uriScheme}://${this.context.extension.id}${CALLBACK_PATH}`,
    );
    // Desktop (also Remote-SSH, Dev Containers, Kubernetes): send the plain
    // vscode://cloudfronts.cfds/auth, because providers match redirect URIs exactly and
    // asExternalUri would append "?windowId=N". The OS routes it to VS Code, which
    // hands it to this extension. On vscode.dev it must become an https URL.
    const redirectUri =
      vscode.env.uiKind === vscode.UIKind.Web
        ? (await vscode.env.asExternalUri(callback)).toString(true)
        : callback.toString(true);
    const state = randomString();
    const nonce = randomString();
    const verifier = randomString(48);
    const url = authorizationUrl(doc, {
      clientId: cfg.clientId,
      redirectUri,
      scopes: wanted,
      state,
      nonce,
      challenge: await codeChallenge(verifier),
      signUp,
    });

    const code = await vscode.window.withProgress(
      {
        location: vscode.ProgressLocation.Notification,
        title: signUp ? "Creating your Cloudfronts account in the browser..." : "Signing in to Cloudfronts in the browser...",
        cancellable: true,
      },
      async (_p, cancel) => {
        const wait = this.waitForCallback(state, cancel);
        if (!(await vscode.env.openExternal(vscode.Uri.parse(url, true)))) {
          throw new Error("Could not open the browser to sign in.");
        }
        return wait;
      },
    );

    const tokens = await exchangeCode(doc, { clientId: cfg.clientId, code, redirectUri, verifier });
    const claims = tokens.idToken ? decodeJwtPayload(tokens.idToken) : {};
    if (claims.nonce !== undefined && claims.nonce !== nonce) {
      throw new Error("Sign-in failed: the ID token nonce did not match.");
    }
    const accessClaims = decodeJwtPayload(tokens.accessToken);
    const sub = String(claims.sub ?? accessClaims.sub ?? "unknown");
    const label = String(claims.email ?? claims.preferred_username ?? claims.name ?? sub);
    const stored: StoredSession = {
      id: randomString(16),
      tokens,
      account: { id: sub, label },
      scopes: wanted,
    };
    await this.store(stored);
    const session = this.toSession(stored);
    this.changeEmitter.fire({ added: [session], removed: [], changed: [] });
    return session;
  }

  private waitForCallback(state: string, cancel: vscode.CancellationToken): Promise<string> {
    return new Promise<string>((resolve, reject) => {
      const disposables: vscode.Disposable[] = [];
      const done = (fn: () => void) => {
        clearTimeout(timer);
        disposables.forEach((d) => d.dispose());
        fn();
      };
      const timer = setTimeout(
        () => done(() => reject(new Error("Sign-in timed out."))),
        LOGIN_TIMEOUT_MS,
      );
      disposables.push(
        cancel.onCancellationRequested(() => done(() => reject(new vscode.CancellationError()))),
        this.uriHandler.event((uri) => {
          if (uri.path !== CALLBACK_PATH) return;
          const q = new URLSearchParams(uri.query);
          if (q.get("state") !== state) return; // a callback for another attempt
          const err = q.get("error");
          if (err) {
            done(() => reject(new Error(`Sign-in failed: ${q.get("error_description") ?? err}`)));
          } else if (q.get("code")) {
            done(() => resolve(q.get("code")!));
          } else {
            done(() => reject(new Error("Sign-in failed: no authorization code returned.")));
          }
        }),
      );
    });
  }

  async removeSession(_sessionId: string): Promise<void> {
    const s = await this.loadStored();
    await this.store(undefined);
    if (s) this.changeEmitter.fire({ added: [], removed: [this.toSession(s)], changed: [] });
  }
}

/**
 * Supplies bearer tokens to the storage client.
 *
 * Order: a token from the CFDS_TOKEN environment variable (for headless
 * Kubernetes pods and CI, where no browser sign-in is possible), then the
 * signed-in Cloudfronts session, then an interactive sign-in prompt.
 */
export function makeTokenSource(provider: CloudfrontsAuthProvider) {
  const envToken = typeof process !== "undefined" ? process.env?.CFDS_TOKEN : undefined;
  return async ({ forceRefresh }: { forceRefresh: boolean }): Promise<string | undefined> => {
    if (envToken) return envToken;
    const s = await provider.validStored(forceRefresh);
    if (s) return s.tokens.accessToken;
    const session = await vscode.authentication.getSession(AUTH_ID, [], { createIfNone: true });
    return session.accessToken;
  };
}
