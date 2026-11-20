# Building, testing and publishing the extension

## Build and test

```bash
cd vscode-extension
npm ci
npm run typecheck
npm test            # unit tests; reads run against the real Python cfds-server in ../src
npm run test:e2e    # launches a real VS Code against an in-memory server
                    # (Linux without a display: xvfb-run -a npm run test:e2e)
CFDS_SERVER_REPO=.. npm run test:e2e
                    # same, against the real multi-user cfds-server, signing in
                    # through the extension's OIDC flow with a stand-in provider
npm run package     # -> cfds-<version>.vsix
```

Try the `.vsix` locally: `code --install-extension cfds-0.1.0.vsix`.

## Before the first release

These need someone with access to the Cloudfronts accounts:

1. **OIDC client.** Register a public client `cfds-vscode` at `oidc.cloudfronts.group`
   with PKCE required, scopes `openid profile email`, and these redirect URIs:
   - `vscode://cloudfronts.cfds/auth` (VS Code stable)
   - `vscode-insiders://cloudfronts.cfds/auth` (Insiders, optional)
   - for vscode.dev, the https callback URL VS Code generates there (check it in a
     test sign-in; desktop always sends exactly `vscode://cloudfronts.cfds/auth`)
   The extension id `cloudfronts.cfds` comes from `publisher` + `name` in
   `package.json`; if either changes, the redirect URIs change with it.
2. **Production server** reachable at `https://storage.cloudfronts.group` (the
   `cfds.serverUrl` default), implementing the v1 API with OIDC token validation.
3. **Marketplace publisher.** Create the publisher `cloudfronts` at
   <https://marketplace.visualstudio.com/manage> (it must match `publisher` in
   `package.json`). Create an Azure DevOps personal access token with the
   **Marketplace › Manage** scope, "All accessible organizations".
4. Optional: publish to [Open VSX](https://open-vsx.org) too, for VSCodium, Gitpod,
   code-server and other Kubernetes-hosted editors (`npx ovsx publish`).

## Publish

```bash
cd vscode-extension
npx vsce login cloudfronts          # paste the PAT
npm run publish                     # or: npx vsce publish minor
```

Or from CI: the `Publish VS Code extension` workflow runs on a `vscode-v*` tag and
needs the repository secret `VSCE_PAT` (and `OVSX_PAT` for Open VSX).
