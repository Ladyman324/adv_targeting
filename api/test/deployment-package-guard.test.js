"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const crypto = require("node:crypto");

const ROOT = path.resolve(__dirname, "..", "..");
const SOURCE_ROOT = process.env.API_PACKAGE_SOURCE_ROOT || ROOT;
const SCRIPT = path.join(SOURCE_ROOT, "src", "build_api.sh");
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
    cwd: SOURCE_ROOT, encoding: "utf8",
  });
}

function archiveFixture(t) {
  const dir = fixture(t);
  const stamp = "2026-10-23T00:00:00Z";
  const contacts = Object.fromEntries(Array.from({ length: 1000 }, (_, i) => [String(i + 1), "fixture-" + i]));
  const descriptor = {
    schemaVersion: 1, registrySchemaVersion: 1,
    registryContentHash: "a".repeat(64), shardManifestHash: "b".repeat(64),
    recipientCount: 1000, ineligibleCount: 0,
    provenance: { identityManifestHash: "c".repeat(64), identityLinksSha256: "d".repeat(64),
      contactsSha256: "e".repeat(64), actSource: "fixture.json", actSourceSha256: "f".repeat(64) },
  };
  const canonical = (value) => Array.isArray(value)
    ? "[" + value.map(canonical).join(",") + "]"
    : value && typeof value === "object"
      ? "{" + Object.keys(value).sort().map((key) => JSON.stringify(key) + ":" + canonical(value[key])).join(",") + "}"
      : JSON.stringify(value);
  descriptor.descriptorHash = crypto.createHash("sha256").update(canonical(descriptor)).digest("hex");
  const files = {
    "host.json": {}, "package.json": {}, "package-lock.json": {},
    "release.json": { id: "fixture", commit: "a".repeat(40), builtUtc: stamp },
    "local.settings.json": { IsEncrypted: false, Values: { AzureWebJobsStorage: "", FUNCTIONS_WORKER_RUNTIME: "node" } },
    "shared/act_contacts.json": { built_utc: stamp, contacts },
    "shared/act_mail_codes.json": { built_utc: stamp, addresses: contacts,
      crds: Object.fromEntries(Object.entries(contacts).slice(0, 100)) },
    "shared/approved-recipient-release.json": descriptor,
  };
  for (const [name, value] of Object.entries(files))
    fs.writeFileSync(path.join(dir, name), JSON.stringify(value));
  // Keep the archive outside the package tree so it cannot include itself.
  const archive = dir + ".tgz";
  t.after(() => fs.rmSync(archive, { force: true }));
  const packed = spawnSync(BASH, ["-c", 'tar -czf "$1" .', "archive-fixture", "../" + path.basename(archive)],
    { cwd: dir, encoding: "utf8" });
  assert.equal(packed.status, 0, packed.stderr);
  return { archive, digest: crypto.createHash("sha256").update(fs.readFileSync(archive)).digest("hex") };
}

test("archive hashes accept native Windows paths without filename escape prefixes", (t) => {
  const { archive, digest } = archiveFixture(t);
  if (process.platform === "win32") assert.ok(archive.includes("\\"));
  const result = spawnSync(BASH, [SCRIPT, "--verify", archive, digest],
    { cwd: SOURCE_ROOT, encoding: "utf8" });
  assert.equal(result.status, 0, result.stderr);
  assert.ok(result.stdout.includes("[*] sha256 " + digest));
});

test("archive verification still rejects the wrong digest before extraction", (t) => {
  const { archive } = archiveFixture(t);
  const result = spawnSync(BASH, [SCRIPT, "--verify", archive, "0".repeat(64)],
    { cwd: SOURCE_ROOT, encoding: "utf8" });
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /SHA-256 mismatch/);
  assert.doesNotMatch(result.stdout, /\[\*\] verified/);
});

function runnerFixture(t) {
  // The test tree is local like the build's staged workspace. SOURCE_ROOT and
  // the spawned shell cwd remain UNC, reproducing the npm.cmd failure boundary.
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "api-test-runner-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const tests = path.join(dir, "test");
  fs.mkdirSync(tests);
  const body = (name) => `
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
test(${JSON.stringify(name)}, () => {
  assert.equal(process.env.API_PACKAGE_TEST_ROOT, ${JSON.stringify(dir)});
  assert.notEqual(process.cwd(), process.env.API_PACKAGE_TEST_ROOT);
});
`;
  fs.writeFileSync(path.join(tests, "deployment-package-guard.test.js"), body("UNC sentinel executed"));
  fs.writeFileSync(path.join(tests, "second.test.js"), body("second explicit test executed"));
  return dir;
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

test("the package test runner executes the explicit local suite from a UNC source cwd", (t) => {
  const dir = runnerFixture(t);
  const result = spawnSync(BASH, [SCRIPT, "--run-tests", dir, "2"], {
    cwd: SOURCE_ROOT, encoding: "utf8",
  });
  assert.equal(result.status, 0, result.stderr);
  const output = result.stdout + result.stderr;
  assert.match(output, /running 2 explicit API test files/);
  assert.match(output, /UNC sentinel executed/);
  assert.match(output, /second explicit test executed/);
  assert.match(output, /tests 2/);

  const nested = path.join(dir, "test", "nested");
  fs.mkdirSync(nested);
  fs.writeFileSync(path.join(nested, "nested.test.js"), `
"use strict";
const test = require("node:test");
test("NESTED_TEST_SENTINEL", () => {});
`);
  const nestedResult = spawnSync(BASH, [SCRIPT, "--run-tests", dir, "3"], {
    cwd: SOURCE_ROOT, encoding: "utf8",
  });
  assert.equal(nestedResult.status, 0, nestedResult.stderr);
  const nestedOutput = nestedResult.stdout + nestedResult.stderr;
  assert.match(nestedOutput, /running 3 explicit API test files/);
  assert.match(nestedOutput, /NESTED_TEST_SENTINEL/);
  assert.match(nestedOutput, /tests 3/);
});

test("the package test runner rejects an unexpected explicit file count", (t) => {
  const dir = runnerFixture(t);
  const result = spawnSync(BASH, [SCRIPT, "--run-tests", dir, "3"], {
    cwd: SOURCE_ROOT, encoding: "utf8",
  });
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /found 2 API test files; expected 3/);
  assert.doesNotMatch(result.stdout + result.stderr, /UNC sentinel executed/);
});

test("an explicit child test failure propagates out of the package runner", (t) => {
  const dir = runnerFixture(t);
  fs.writeFileSync(path.join(dir, "test", "second.test.js"), `
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
test("intentional fixture failure", () => assert.fail("RUNNER_FAILURE_SENTINEL"));
`);
  const result = spawnSync(BASH, [SCRIPT, "--run-tests", dir, "2"], {
    cwd: SOURCE_ROOT, encoding: "utf8",
  });
  assert.notEqual(result.status, 0);
  assert.match(result.stdout + result.stderr, /RUNNER_FAILURE_SENTINEL/);
});
test("the package test runner fails closed when its sentinel is absent", (t) => {
  const dir = runnerFixture(t);
  fs.rmSync(path.join(dir, "test", "deployment-package-guard.test.js"));
  const result = spawnSync(BASH, [SCRIPT, "--run-tests", dir, "1"], {
    cwd: SOURCE_ROOT, encoding: "utf8",
  });
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /API test sentinel is missing/);
  assert.doesNotMatch(result.stdout, /second explicit test executed/);
});

test("the shell guard mirrors every Python deployer forbidden name and suffix", () => {
  const shell = fs.readFileSync(SCRIPT, "utf8");
  const python = fs.readFileSync(path.join(SOURCE_ROOT, "src", "deploy_swa.py"), "utf8");
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
