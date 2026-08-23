"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { releaseMetadata } = require("../health");

function fixture(value) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "advisor-release-"));
  const file = path.join(dir, "release.json");
  fs.writeFileSync(file, JSON.stringify(value), "utf8");
  return { dir, file };
}

test("release provenance exposes only the approved fields", (t) => {
  const f = fixture({
    id: "0222568-123-1",
    commit: "02225680e29e3e271312e9d189b42e3fab2b0f4d",
    builtUtc: "2026-08-22T23:10:00Z",
    workflowRun: "manual-123",
    dirty: false,
    AZURE_STORAGE_CONNECTION_STRING: "must-not-escape",
  });
  t.after(() => fs.rmSync(f.dir, { recursive: true, force: true }));

  assert.deepEqual(releaseMetadata(f.file), {
    available: true,
    id: "0222568-123-1",
    commit: "02225680e29e3e271312e9d189b42e3fab2b0f4d",
    builtUtc: "2026-08-22T23:10:00Z",
    workflowRun: "manual-123",
    dirty: false,
  });
});

test("missing or malformed provenance fails closed", (t) => {
  const missing = releaseMetadata(path.join(os.tmpdir(), "not-a-release.json"));
  assert.equal(missing.available, false);
  assert.equal(missing.id, "development");

  const f = fixture({
    id: "bad id with spaces",
    commit: "not-a-commit",
    builtUtc: "yesterday",
  });
  t.after(() => fs.rmSync(f.dir, { recursive: true, force: true }));
  assert.equal(releaseMetadata(f.file).available, false);
});
