import * as esbuild from "esbuild";

const production = process.argv.includes("--production");
const watch = process.argv.includes("--watch");

const common = {
  bundle: true,
  external: ["vscode"],
  sourcemap: !production,
  minify: production,
  logLevel: "info",
};

const targets = [
  // Desktop VS Code, Remote-SSH, Dev Containers and Kubernetes pods (Node extension host).
  { ...common, entryPoints: ["src/extension.ts"], outfile: "dist/node/extension.js", platform: "node", format: "cjs", target: "node18" },
  // vscode.dev / github.dev (web worker extension host).
  { ...common, entryPoints: ["src/extension.ts"], outfile: "dist/web/extension.js", platform: "browser", external: ["vscode", "crypto"], format: "cjs", target: "es2022" },
  // Plain client and OIDC modules so the tests can import it without VS Code.
  { ...common, entryPoints: ["src/client.ts", "src/oidc.ts"], outdir: "dist/test", platform: "node", format: "cjs", target: "node18", sourcemap: false, minify: false },
];

if (watch) {
  for (const t of targets) (await esbuild.context(t)).watch();
} else {
  await Promise.all(targets.map((t) => esbuild.build(t)));
}
