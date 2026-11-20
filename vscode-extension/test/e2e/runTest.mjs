// Launches a real VS Code with this extension and runs suite.cjs inside it,
// against the in-memory cfds server. Usage: npm run test:e2e (needs a display;
// on Linux CI wrap with xvfb-run).
import path from "node:path";
import os from "node:os";
import fs from "node:fs";
import { runTests } from "@vscode/test-electron";
import { startFakeServer } from "./fakeServer.mjs";

const root = path.resolve(import.meta.dirname, "..", "..");
const server = await startFakeServer("e2e-token");
const userData = fs.mkdtempSync(path.join(os.tmpdir(), "cfds-vscode-"));
try {
  await runTests({
    version: process.env.VSCODE_VERSION ?? "stable",
    extensionDevelopmentPath: root,
    extensionTestsPath: path.join(root, "test", "e2e", "suite.cjs"),
    launchArgs: ["--disable-extensions", "--user-data-dir", userData, "--no-sandbox", "--disable-gpu"],
    extensionTestsEnv: {
      CFDS_TOKEN: "e2e-token",
      CFDS_TEST_SERVER: `http://127.0.0.1:${server.address().port}`,
    },
  });
  console.log("server saw:\n  " + server.log.join("\n  "));
} catch (err) {
  console.error("e2e failed:", err);
  console.error("server saw:\n  " + server.log.join("\n  "));
  process.exitCode = 1;
} finally {
  server.close();
}
