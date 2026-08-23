"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const ROOT = path.resolve(__dirname, "..", "..");
const SCRIPT = path.join(ROOT, "src", "build_api.sh");
const WINDOWS_BASH = "C:\\Program Files\\Git\\bin\\bash.exe";
const BASH = process.platform === "win32" && fs.existsSync(WINDOWS_BASH)
  ? WINDOWS_BASH : "bash";

function fixture(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "api-boundary-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  fs.mkdirSync(path.join(dir, "shared"));
  fs.writeFileSync(path.join(dir, "index.js"), '"use strict";\n');
  fs.writeFileSync(path.join(dir, "shared", "act_contacts.json"), "{}\n");
  fs.writeFileSync(path.join(dir, "shared", "act_mail_codes.json"), "{}\n");
  return dir;
}

function check(dir) {
  return spawnSync(BASH, [SCRIPT, "--check-source", dir], {
    cwd: ROOT, encoding: "utf8",
  });
}

test("the source boundary permits the two generated ACT JSON files", (t) => {
  const result = check(fixture(t));
  assert.equal(result.status, 0, result.stderr);
});

test("secret, backup, archive, key, and source settings files fail closed", async (t) => {
  const cases = [
    [".env", "CONTENT_SHOULD_NOT_LEAK_01"],
    ["old-api.tgz", "CONTENT_SHOULD_NOT_LEAK_02"],
    ["signing.pfx", "CONTENT_SHOULD_NOT_LEAK_03"],
    ["local.settings.json", "CONTENT_SHOULD_NOT_LEAK_04"],
    [".netrc", "CONTENT_SHOULD_NOT_LEAK_05"],
    ["secrets.yml", "CONTENT_SHOULD_NOT_LEAK_06"],
    ["signing.pkcs12", "CONTENT_SHOULD_NOT_LEAK_07"],
  ];
  for (const [name, secret] of cases) {
    await t.test(name, () => {
      const dir = fixture(t);
      fs.writeFileSync(path.join(dir, name), secret);
      const result = check(dir);
      assert.notEqual(result.status, 0, "guard unexpectedly accepted " + name);
      assert.match(result.stderr, /not allowed/);
      assert.ok(result.stderr.includes(name), "diagnostic should name the rejected file");
      assert.doesNotMatch(result.stderr + result.stdout, new RegExp(secret));
    });
  }
});

test("source symlinks fail closed when the platform permits creating one", (t) => {
  const dir = fixture(t);
  const target = path.join(dir, "index.js");
  try {
    fs.symlinkSync(target, path.join(dir, "linked.js"), "file");
  } catch (error) {
    if (error.code === "EPERM" || error.code === "EACCES")
      return t.skip("Windows policy does not permit a test symlink");
    throw error;
  }
  const result = check(dir);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /symbolic links are not allowed/);
});

test("archive verification rejects link and special entry types before extraction", () => {
  const source = fs.readFileSync(SCRIPT, "utf8");
  assert.match(source, /tar -tvzf/);
  assert.match(source, /archive contains a link or special filesystem entry/);
});

test("the shell guard mirrors every Python deployer forbidden name and suffix", () => {
  const shell = fs.readFileSync(SCRIPT, "utf8");
  const python = fs.readFileSync(path.join(ROOT, "src", "deploy_swa.py"), "utf8");
  const values = (label, opener, closer) => {
    const start = python.indexOf(label + " = " + opener);
    assert.notEqual(start, -1, "missing Python contract " + label);
    const end = python.indexOf(closer, start);
    assert.notEqual(end, -1, "unterminated Python contract " + label);
    return [...python.slice(start, end).matchAll(/"([^"]+)"/g)].map((match) => match[1]);
  };
  const names = values("FORBIDDEN_DEPLOY_NAMES", "{", "}");
  const suffixes = values("FORBIDDEN_DEPLOY_SUFFIXES", "{", "}");
  const endings = values("FORBIDDEN_DEPLOY_ENDINGS", "(", ")");
  for (const name of names)
    assert.ok(shell.includes("-iname '" + name + "'"), "shell guard missing name " + name);
  for (const suffix of [...suffixes, ...endings])
    assert.ok(shell.includes("-iname '*" + suffix + "'"), "shell guard missing suffix " + suffix);
});
