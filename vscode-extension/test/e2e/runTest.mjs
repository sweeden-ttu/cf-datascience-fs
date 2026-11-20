// Launches a real VS Code with this extension and runs suite.cjs inside it,
// against the in-memory cfds server. Usage: npm run test:e2e (needs a display;
// on Linux CI wrap with xvfb-run).
import path from "node:path";
import os from "node:os";
import fs from "node:fs";
import { runTests } from "@vscode/test-electron";
import { spawn } from "node:child_process";
import readline from "node:readline";
import { startFakeServer } from "./fakeServer.mjs";

const root = path.resolve(import.meta.dirname, "..", "..");

// CFDS_SERVER_REPO=<checkout of the Python server with multi-user support> runs
// against the real cfds-server and a stand-in OIDC provider, signing in through
// the extension's real browser flow. Without it, an in-memory server is used.
const serverRepo = process.env.CFDS_SERVER_REPO;
let env, server, py;
if (serverRepo) {
  py = spawn(process.env.PYTHON ?? "python3", [path.join(root, "test", "e2e", "real_server.py")], {
    env: { ...process.env, PYTHONPATH: [path.join(serverRepo, "src"), path.join(serverRepo, "tests")].join(path.delimiter) },
    stdio: ["pipe", "pipe", "inherit"],
  });
  const line = await new Promise((resolve, reject) => {
    readline.createInterface({ input: py.stdout }).once("line", resolve);
    py.once("exit", (code) => reject(new Error(`real_server.py exited with ${code}`)));
  });
  const info = JSON.parse(line);
  console.log(`real cfds-server at ${info.server}, sign-in provider at ${info.issuer}`);
  env = { CFDS_TEST_SERVER: info.server, CFDS_TEST_ISSUER: info.issuer };
} else {
  server = await startFakeServer("e2e-token");
  env = { CFDS_TOKEN: "e2e-token", CFDS_TEST_SERVER: `http://127.0.0.1:${server.address().port}` };
}
const userData = fs.mkdtempSync(path.join(os.tmpdir(), "cfds-vscode-"));
try {
  await runTests({
    version: process.env.VSCODE_VERSION ?? "stable",
    extensionDevelopmentPath: root,
    extensionTestsPath: path.join(root, "test", "e2e", "suite.cjs"),
    launchArgs: ["--disable-extensions", "--user-data-dir", userData, "--no-sandbox", "--disable-gpu"],
    extensionTestsEnv: env,
  });
  if (server) console.log("server saw:\n  " + server.log.join("\n  "));
} catch (err) {
  console.error("e2e failed:", err);
  if (server) console.error("server saw:\n  " + server.log.join("\n  "));
  process.exitCode = 1;
} finally {
  server?.close();
  py?.kill();
}
