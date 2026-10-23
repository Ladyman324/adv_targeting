"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const vm = require("node:vm");
const { project, read } = require("../shared/data-freshness");
const { createHandler } = require("../data-freshness");
const now = new Date("2026-10-22T12:00:00Z");
function fixture() {
  return { schemaVersion: 1, candidate: { id: "run-123", createdUtc: "2026-10-22T10:00:00Z",
    codeCommit: "a".repeat(40), staticDataFingerprint: "20261022T100000Z-abc123", recipientRegistryHash: "b".repeat(64) },
    sources: [{ id: "act", label: "ACT!", sourceDate: "2026-10-20", retrievedUtc: "2026-10-21T10:00:00Z",
      lastSuccessUtc: "2026-10-21T10:00:00Z", lastAttemptUtc: "2026-10-21T10:00:00Z",
      attemptStatus: "success", status: "reused", records: 12000, sha256: "c".repeat(64),
      snapshotId: "act-snapshot-1", freshnessDays: 7 }], publication: null };
}
function receipt(raw) {
  return { verified: true, publishedUtc: "2026-10-22T11:00:00Z", candidateId: raw.candidate.id,
    staticDataFingerprint: raw.candidate.staticDataFingerprint, recipientRegistryHash: raw.candidate.recipientRegistryHash,
    apiCommit: "a".repeat(40), sourceHashes: { act: "c".repeat(64) } };
}
test("freshness projects aggregate allowlist without private fields", () => {
  const raw = fixture();
  raw.secret = "secret-root";
  raw.candidate.path = "C:/private";
  Object.assign(raw.sources[0], { contactEmail: "person@example.com", actId: "private-act-guid",
    path: "//server/private", error: "secret-exception", errorCode: "C:/secret-error" });
  const result = project(raw, now), json = JSON.stringify(result);
  assert.equal(result.available, true);
  assert.equal(result.sources[0].freshness, "current");
  assert.equal(result.sources[0].candidateIncluded, true);
  assert.equal(result.sources[0].publishedIncluded, false);
  assert.doesNotMatch(json, /secret|private|person@example|server/);
});
test("publication requires a complete bound verified receipt", () => {
  const raw = fixture();
  raw.publication = receipt(raw);
  assert.equal(project(raw, now).publication.verified, true);
  assert.equal(project(raw, now).sources[0].publishedIncluded, true);
  for (const key of ["verified", "candidateId", "staticDataFingerprint", "recipientRegistryHash", "apiCommit", "sourceHashes", "publishedUtc"]) {
    const changed = structuredClone(raw);
    changed.publication[key] = null;
    assert.equal(project(changed, now).publication, null, key);
  }
  raw.publication.sourceHashes.act = "d".repeat(64);
  assert.equal(project(raw, now).publication, null);
});
test("publication cannot verify a different or unknown candidate code commit", () => {
  const raw = fixture(); raw.publication = receipt(raw);
  raw.publication.apiCommit = "d".repeat(40);
  let result = project(raw, now);
  assert.equal(result.publication, null);
  assert.equal(result.sources[0].publishedIncluded, false);
  raw.publication = receipt(raw);
  delete raw.candidate.codeCommit;
  result = project(raw, now);
  assert.equal(result.candidate.codeCommit, null);
  assert.equal(result.publication, null);
  assert.equal(result.sources[0].publishedIncluded, false);
});
test("old, failed, unknown and never-loaded are distinct", () => {
  const raw = fixture();
  raw.sources[0].sourceDate = "2026-09-01";
  assert.equal(project(raw, now).sources[0].freshness, "old");
  raw.sources[0].attemptStatus = "failed";
  raw.sources[0].errorCode = "ACQUISITION_FAILED";
  assert.equal(project(raw, now).sources[0].freshness, "failed");
  raw.sources[0] = { id: "sec", label: "SEC", status: "never_loaded" };
  assert.equal(project(raw, now).sources[0].freshness, "never_loaded");
  raw.sources[0].status = "unknown";
  assert.equal(project(raw, now).sources[0].freshness, "unknown");
});
test("publication binds included snapshots while never-loaded sources remain excluded", () => {
  const raw = fixture(); raw.publication = receipt(raw);
  raw.sources.push({ id: "roster:morgan_stanley", label: "Morgan Stanley", status: "never_loaded" });
  const result = project(raw, now);
  assert.equal(result.publication.verified, true);
  assert.equal(result.sources[0].publishedIncluded, true);
  assert.equal(result.sources[1].candidateIncluded, false);
  assert.equal(result.sources[1].publishedIncluded, false);
});
test("malformed descriptors fail closed and unknown values are normalized", () => {
  assert.equal(project({ schemaVersion: 2 }, now).available, false);
  for (const mutation of [
    raw => { raw.sources.push(raw.sources[0]); },
    raw => { raw.sources[0].label = "<script>bad</script>"; },
    raw => { raw.sources[0].id = "../private"; },
    raw => { raw.candidate.id = "https://secret"; },
    raw => { raw.candidate.createdUtc = "not-a-date"; },
  ]) {
    const raw = fixture(); mutation(raw);
    assert.equal(project(raw, now).available, false);
  }
  const raw = fixture();
  Object.assign(raw.sources[0], { records: -1, retrievedUtc: "bad", edition: "person@example.com",
    snapshotId: "//private/path", attemptStatus: "secret failure" });
  const row = project(raw, now).sources[0];
  assert.equal(row.records, null); assert.equal(row.retrievedUtc, null); assert.equal(row.edition, null);
  assert.equal(row.snapshotId, null); assert.equal(row.attemptStatus, "unknown");
});
test("missing, invalid, oversized descriptor is unavailable", t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "refresh-metadata-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const file = path.join(dir, "metadata.json");
  assert.equal(read(file, now).available, false);
  fs.writeFileSync(file, "{");
  assert.equal(read(file, now).available, false);
  fs.writeFileSync(file, JSON.stringify(fixture()));
  assert.equal(read(file, now).available, true);
  fs.writeFileSync(file, " ".repeat(512 * 1024 + 1));
  assert.equal(read(file, now).available, false);
});
test("bounded backend receipt overlay verifies without modifying packaged source metadata", t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "refresh-publication-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const file = path.join(dir, "metadata.json"), raw = fixture();
  fs.writeFileSync(file, JSON.stringify(raw));
  const original = fs.readFileSync(file);
  const valid = receipt(raw);
  let result = read(file, now, JSON.stringify(valid));
  assert.equal(result.publication.verified, true);
  assert.equal(result.sources[0].publishedIncluded, true);
  for (const value of ["secret-not-json", "x".repeat(65537), JSON.stringify({ ...valid, candidateId: "old-run" }),
    JSON.stringify({ ...valid, apiCommit: "d".repeat(40) }), JSON.stringify({ ...valid, sourceHashes: {} })]) {
    result = read(file, now, value);
    assert.equal(result.available, true);
    assert.equal(result.publication, null);
    assert.equal(result.sources[0].publishedIncluded, false);
    assert.doesNotMatch(JSON.stringify(result), /secret-not-json/);
  }
  assert.deepEqual(fs.readFileSync(file), original);
  const prior = process.env.DATA_FRESHNESS_PUBLICATION_RECEIPT_JSON;
  try {
    process.env.DATA_FRESHNESS_PUBLICATION_RECEIPT_JSON = JSON.stringify(valid);
    assert.equal(read(file, now).publication.verified, true);
  } finally {
    if (prior === undefined) delete process.env.DATA_FRESHNESS_PUBLICATION_RECEIPT_JSON;
    else process.env.DATA_FRESHNESS_PUBLICATION_RECEIPT_JSON = prior;
  }
});
test("endpoint authorizes before reading and exposes no raw exception", async () => {
  let reads = 0;
  for (const roles of [[], ["authenticated"], ["EmailAdministrator"], ["ADMINISTRATOR"]]) {
    const handler = createHandler({ identity: () => ({ roles }), read: () => { reads++; return project(fixture(), now); } });
    const context = {};
    await handler(context, { method: "GET" });
    assert.equal(context.res.status, roles[0] === "EmailAdministrator" || roles[0] === "ADMINISTRATOR" ? 200 : 403);
    assert.match(context.res.headers["Cache-Control"], /no-store/);
  }
  assert.equal(reads, 2);
  const unauthenticated = createHandler({ identity: () => { const e = new Error("private"); e.statusCode = 401; throw e; },
    read: () => { throw new Error("must-not-read"); } });
  const context = {}; await unauthenticated(context, {});
  assert.equal(context.res.status, 401); assert.doesNotMatch(context.res.body, /private|must-not-read/);
  const bad = createHandler({ identity: () => ({ roles: ["administrator"] }), read: () => { throw new Error("secret-url"); } });
  await bad(context, { method: "GET" });
  assert.equal(context.res.status, 503); assert.doesNotMatch(context.res.body, /secret-url/);
  await bad(context, { method: "POST" });
  assert.equal(context.res.status, 405);
});
test("desktop freshness view escapes metadata and provides no refresh mutation", () => {
  const file = path.join(__dirname, "../../webapp/app.js");
  const source = fs.readFileSync(file, "utf8");
  const view = source.slice(source.indexOf("async function openDataFreshness"), source.indexOf("async function saveSetting"));
  assert.match(source, /ADMIN \?[^;]*data-set="freshness"/);
  assert.match(view, /fetch\("\/api\/data-freshness", \{ credentials: "same-origin", cache: "no-store" \}\)/);
  assert.match(view, /esc\(row.label\)/);
  assert.match(view, /esc\(row.errorCode\)/);
  assert.match(view, /Not verified for this candidate/);
  assert.match(view, /<dt>Run started<\/dt>/);
  assert.doesNotMatch(view, /<dt>Built<\/dt>/);
  assert.doesNotMatch(view, /method: "POST"|innerHTML = error|\.write\s*\(|\.publish\s*\(/);
});

test("desktop modal renders candidate, publication, empty and failure states without mutations", async () => {
  const source = fs.readFileSync(path.join(__dirname, "../../webapp/app.js"), "utf8");
  const view = source.slice(source.indexOf("async function openDataFreshness"), source.indexOf("async function saveSetting"));
  const valid = project(fixture(), now);
  for (const scenario of ["candidate", "published", "unavailable", "denied", "network", "empty"]) {
    let back, reopened = 0;
    const listeners = new Map();
    const host = { innerHTML: "", textContent: "" }, retry = { disabled: false }, closeButton = {};
    const document = {
      activeElement: null,
      body: { appendChild(element) { element.isConnected = true; } },
      addEventListener(type, callback) { listeners.set(type, callback); },
      removeEventListener(type) { listeners.delete(type); },
      createElement() {
        const dialog = { querySelectorAll: () => [retry, closeButton], focus: () => { document.activeElement = dialog; } };
        back = { isConnected: false, innerHTML: "", remove() { back.isConnected = false; },
          querySelector(selector) { return selector === '[role="dialog"]' ? dialog : selector === "[data-freshness-body]" ? host : retry; } };
        return back;
      },
    };
    const raw = fixture(); raw.publication = receipt(raw);
    let payload = scenario === "published" ? project(raw, now) : structuredClone(valid);
    if (scenario === "unavailable") payload = project(null, now);
    if (scenario === "empty") payload.sources = [];
    if (scenario === "candidate") payload.sources[0].label = '<img src=x onerror="bad">';
    const context = vm.createContext({ document, esc: value => String(value).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"),
      openSettings: () => { reopened++; },
      fetch: async (url, options) => {
        assert.equal(url, "/api/data-freshness"); assert.equal(options.cache, "no-store"); assert.equal(options.method, undefined);
        if (scenario === "network") throw new Error("Network unavailable");
        return { ok: scenario !== "denied", status: scenario === "denied" ? 403 : 200, json: async () => payload };
      } });
    await vm.runInContext(view + "\nopenDataFreshness()", context);
    assert.equal(retry.disabled, false);
    if (scenario === "candidate") { assert.match(host.innerHTML, /&lt;img/); assert.doesNotMatch(host.innerHTML, /<img/); assert.match(host.innerHTML, /Not verified for this candidate/); }
    if (scenario === "published") assert.match(host.innerHTML, /2026-10-22 11:00:00 UTC/);
    if (scenario === "unavailable") assert.match(host.innerHTML, /provenance is unavailable/);
    if (scenario === "empty") assert.match(host.innerHTML, /No source snapshots/);
    if (scenario === "denied") assert.match(host.textContent, /administrator access/);
    if (scenario === "network") assert.match(host.textContent, /Network unavailable/);
    listeners.get("keydown")({ key: "Escape", preventDefault() {} });
    assert.equal(back.isConnected, false); assert.equal(reopened, 1); assert.equal(listeners.size, 0);
  }
});
