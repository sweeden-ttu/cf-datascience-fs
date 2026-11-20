// Runs inside the VS Code extension host. Exercises the cfds: FileSystemProvider
// through vscode.workspace.fs exactly as the Explorer and editors do.
const vscode = require("vscode");
const assert = require("assert");

const u = (p) => vscode.Uri.from({ scheme: "cfds", path: p });
const enc = (s) => new TextEncoder().encode(s);
const dec = (b) => new TextDecoder().decode(b);

async function expectFsError(promise, code) {
  try {
    await promise;
  } catch (e) {
    assert.ok(e instanceof vscode.FileSystemError, `expected FileSystemError, got ${e}`);
    assert.strictEqual(e.code, code, `expected ${code}, got ${e.code}: ${e.message}`);
    return;
  }
  assert.fail(`expected ${code}`);
}

async function run() {
  await vscode.workspace.getConfiguration("cfds").update("serverUrl", process.env.CFDS_TEST_SERVER, vscode.ConfigurationTarget.Global);
  const ext = vscode.extensions.getExtension("cloudfronts.cfds");
  assert.ok(ext, "extension cloudfronts.cfds is installed");
  await ext.activate();
  const fs = vscode.workspace.fs;
  const steps = [];
  const step = async (name, fn) => { await fn(); steps.push(name); console.log(`  ok - ${name}`); };

  await step("root lists the two mounts", async () => {
    const names = (await fs.readDirectory(u("/"))).map(([n, t]) => `${n}:${t}`).sort();
    assert.deepStrictEqual(names, ["home:2", "kaggle-datasets:2"]);
  });
  await step("shared dataset is readable and marked read-only", async () => {
    const st = await fs.stat(u("/kaggle-datasets/titanic/train.csv"));
    assert.strictEqual(st.type, vscode.FileType.File);
    assert.strictEqual(st.size, 20);
    assert.strictEqual(st.permissions, vscode.FilePermission.Readonly);
    assert.strictEqual(dec(await fs.readFile(u("/kaggle-datasets/titanic/train.csv"))), "id,survived\n1,0\n2,1\n");
  });
  await step("writing to the datasets is refused as NoPermissions", () =>
    expectFsError(fs.writeFile(u("/kaggle-datasets/titanic/x.csv"), enc("x")), "NoPermissions"));
  await step("missing file is FileNotFound", () => expectFsError(fs.stat(u("/home/nope.txt")), "FileNotFound"));
  await step("mkdir, write, read back, overwrite in /home", async () => {
    await fs.createDirectory(u("/home/notebooks"));
    await fs.writeFile(u("/home/notebooks/eda.py"), enc("print('hi')\n"));
    assert.strictEqual(dec(await fs.readFile(u("/home/notebooks/eda.py"))), "print('hi')\n");
    await fs.writeFile(u("/home/notebooks/eda.py"), enc("print('v2')\n"));
    assert.strictEqual(dec(await fs.readFile(u("/home/notebooks/eda.py"))), "print('v2')\n");
  });
  await step("open in an editor, edit and save", async () => {
    const doc = await vscode.workspace.openTextDocument(u("/home/notebooks/eda.py"));
    const editor = await vscode.window.showTextDocument(doc);
    await editor.edit((e) => e.insert(new vscode.Position(0, 0), "# saved from VS Code\n"));
    assert.ok(await doc.save(), "save succeeded");
    assert.strictEqual(dec(await fs.readFile(u("/home/notebooks/eda.py"))), "# saved from VS Code\nprint('v2')\n");
  });
  await step("copy a dataset into /home (server-side cp)", async () => {
    await fs.copy(u("/kaggle-datasets/titanic"), u("/home/titanic"));
    assert.strictEqual(dec(await fs.readFile(u("/home/titanic/train.csv"))), "id,survived\n1,0\n2,1\n");
    assert.strictEqual((await fs.stat(u("/home/titanic/train.csv"))).permissions, undefined);
  });
  await step("rename and refuse overwrite", async () => {
    await fs.rename(u("/home/notebooks/eda.py"), u("/home/notebooks/eda2.py"));
    await expectFsError(fs.stat(u("/home/notebooks/eda.py")), "FileNotFound");
    await fs.writeFile(u("/home/notebooks/other.py"), enc("1"));
    await expectFsError(fs.rename(u("/home/notebooks/other.py"), u("/home/notebooks/eda2.py"), { overwrite: false }), "FileExists");
  });
  await step("recursive delete", async () => {
    await fs.delete(u("/home/notebooks"), { recursive: true, useTrash: false });
    await expectFsError(fs.stat(u("/home/notebooks")), "FileNotFound");
  });
  await step("status bar quota command runs", async () => {
    await vscode.commands.executeCommand("cfds.showQuota");
  });
  console.log(`e2e: ${steps.length} steps passed`);
}

module.exports = { run };
