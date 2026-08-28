"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const zlib = require("node:zlib");
const registry = require("../shared/recipient-registry");
const descriptor = require("../shared/approved-recipient-release.json");

function record(email, tier = "confirmed", extra = {}) {
  return { email, tier, name: "Advisor", greetingName: "Advisor",
    lastName: "Person", firm: "Firm", source: "CRM", ...extra };
}

test.afterEach(() => registry.reset());

test("confirmed and high identities resolve; weaker tiers never do", async () => {
  // `high` was admitted deliberately. It is a strong name match with no CRD
  // asserted anywhere -- about 0.989 precision -- which is a decision about
  // acceptable misdirection, not a safety property. `review` means "we are not
  // sure this is the same person" and is never addressable.
  registry.useIndex({ recipients: {
    "100": record("high@example.com", "high"),
    "101": record("confirmed@example.com", "confirmed"),
    "102": record("review@example.com", "review"),
    "103": record("none@example.com", "none"),
  } });
  assert.equal((await registry.resolve("101")).email, "confirmed@example.com");
  assert.equal((await registry.resolve("100")).email, "high@example.com");
  for (const crd of ["102", "103"])
    await assert.rejects(registry.resolve(crd),
      (error) => error.code === "recipient_not_approved");
});

test("APPROVED_RECIPIENT_TIERS can narrow the policy without a rebuild", async () => {
  // The point of the setting: tightening used to require rebuilding the API,
  // re-exporting the blob, and matching the two by content hash.
  const saved = process.env.APPROVED_RECIPIENT_TIERS;
  process.env.APPROVED_RECIPIENT_TIERS = "confirmed";
  const modulePath = require.resolve("../shared/recipient-registry");
  delete require.cache[modulePath];
  const narrowed = require(modulePath);
  try {
    narrowed.useIndex({ recipients: {
      "100": record("high@example.com", "high"),
      "101": record("confirmed@example.com", "confirmed"),
    } });
    assert.equal((await narrowed.resolve("101")).email, "confirmed@example.com");
    await assert.rejects(narrowed.resolve("100"),
      (error) => error.code === "recipient_not_approved");
  } finally {
    narrowed.reset();
    if (saved === undefined) delete process.env.APPROVED_RECIPIENT_TIERS;
    else process.env.APPROVED_RECIPIENT_TIERS = saved;
    delete require.cache[modulePath];
  }
});

test("a blocked source excludes high routes but never confirmed routes", async () => {
  const savedTiers = process.env.APPROVED_RECIPIENT_TIERS;
  const savedSources = process.env.APPROVED_RECIPIENT_BLOCKED_SOURCES;
  const modulePath = require.resolve("../shared/recipient-registry");
  process.env.APPROVED_RECIPIENT_TIERS = "confirmed,high";
  process.env.APPROVED_RECIPIENT_BLOCKED_SOURCES = " uBs , EDWARD JONES ";
  delete require.cache[modulePath];
  const blocked = require(modulePath);
  try {
    blocked.useIndex({ recipients: {
      "110": record("high@example.com", "high", { source: "UBS" }),
      "111": record("confirmed@example.com", "confirmed", { source: "UBS" }),
    } });
    await assert.rejects(blocked.resolve("110"),
      (error) => error.code === "recipient_not_approved"
        && error.detail === "high_tier_source_blocked");
    assert.equal((await blocked.resolve("111")).email, "confirmed@example.com");
    assert.deepEqual(blocked.policy().tiers, ["confirmed", "high"]);
    assert.deepEqual(blocked.policy().blockedHighSources, ["edward jones", "ubs"]);
    assert.notEqual(blocked.policy().version, registry.policy().version);
  } finally {
    blocked.reset();
    if (savedTiers === undefined) delete process.env.APPROVED_RECIPIENT_TIERS;
    else process.env.APPROVED_RECIPIENT_TIERS = savedTiers;
    if (savedSources === undefined) delete process.env.APPROVED_RECIPIENT_BLOCKED_SOURCES;
    else process.env.APPROVED_RECIPIENT_BLOCKED_SOURCES = savedSources;
    delete require.cache[modulePath];
  }
});

test("internal colleagues resolve only when their domain or address is allowlisted", async () => {
  // Excluding them outright removed the only safe rehearsal path: every test
  // batch is addressed to this firm, and the exclusion blocked the account
  // doing the testing. They are exported and gated instead.
  const saved = process.env.EMAIL_INTERNAL_RECIPIENT_ALLOWLIST;
  try {
    registry.useIndex({ recipients: {
      "200": record("colleague@eicatlanta.com", "confirmed", { internal: true }),
    } });
    await assert.rejects(registry.resolve("200"),
      (error) => error.code === "recipient_not_approved",
      "with no allowlist, internal stays unaddressable");

    // The DOMAIN form, because that is what a firm sets: one entry that also
    // covers the colleague who joins next month.
    process.env.EMAIL_INTERNAL_RECIPIENT_ALLOWLIST = "eicatlanta.com";
    registry.reset();
    registry.useIndex({ recipients: {
      "200": record("colleague@eicatlanta.com", "confirmed", { internal: true }),
    } });
    assert.equal((await registry.resolve("200")).email, "colleague@eicatlanta.com");
  } finally {
    if (saved === undefined) delete process.env.EMAIL_INTERNAL_RECIPIENT_ALLOWLIST;
    else process.env.EMAIL_INTERNAL_RECIPIENT_ALLOWLIST = saved;
  }
});

test("one email claimed by two CRDs excludes both", async () => {
  registry.useIndex({ recipients: {
    "100": record("shared@example.com"),
    "101": record("shared@example.com"),
  } });
  await assert.rejects(registry.resolve("100"),
    (error) => error.code === "recipient_not_approved" && error.detail === "duplicate_email");
  await assert.rejects(registry.resolve("101"),
    (error) => error.code === "recipient_not_approved" && error.detail === "duplicate_email");
});

test("a bad top-level content hash is rejected", () => {
  const payload = { schemaVersion: "1.0", recipients: {
    "100": record("safe@example.com"),
  }, ineligible: {}, contentHash: "0".repeat(64) };
  assert.throws(() => registry.hydrate(payload),
    (error) => error.code === "recipient_registry_incompatible");
});

test("a future registry schema is rejected rather than guessed compatible", () => {
  const payload = { schemaVersion: 2, recipients: {}, ineligible: {},
    provenance: {} };
  payload.contentHash = registry.expectedContentHash(payload);
  assert.throws(() => registry.hydrate(payload),
    (error) => error.code === "recipient_registry_incompatible");
});

test("a validly self-hashed registry from another release is rejected", () => {
  const payload = { schemaVersion: 1, recipients: {
    "100": record("safe@example.com"),
  }, ineligible: {}, provenance: { actSource: "older-snapshot.json" } };
  payload.contentHash = registry.expectedContentHash(payload);
  assert.throws(() => registry.hydrate(payload),
    (error) => error.code === "recipient_registry_release_mismatch");
});

test("the packaged PII-free descriptor accepts the exact local registry", () => {
  assert.deepEqual(Object.keys(descriptor).sort(), ["descriptorHash",
    "ineligibleCount", "provenance", "recipientCount",
    "registryContentHash", "registrySchemaVersion", "schemaVersion"]);
  assert.equal(JSON.stringify(descriptor).includes("@"), false);
  const root = process.env.REPOSITORY_TEST_ROOT
    || path.resolve(__dirname, "../..");
  const payload = JSON.parse(zlib.gunzipSync(fs.readFileSync(path.join(root,
    "data", "identity", "approved_recipients.json.gz"))).toString("utf8"));
  const hydrated = registry.hydrate(payload);
  assert.equal(hydrated.contentHash, descriptor.registryContentHash);
  assert.deepEqual(hydrated.provenance, descriptor.provenance);
});

test("a duplicated Act GUID cannot receive an Act write", async () => {
  registry.useIndex({ recipients: {
    "100": record("one@example.com", "confirmed", { actContactId: "act-same" }),
    "101": record("two@example.com", "confirmed", { actContactId: "act-same" }),
  } });
  await assert.rejects(registry.verifyActPair("100", "one@example.com"),
    (error) => error.code === "recipient_has_no_act_contact");
  await assert.rejects(registry.verifyActPair("101", "two@example.com"),
    (error) => error.code === "recipient_has_no_act_contact");
});

test("teammates are server-authoritative CRD and email pairs", async () => {
  registry.useIndex({ recipients: {
    "100": record("primary@example.com", "confirmed", { teammates: [
      { crd: "101", email: "mate@example.com", name: "Mate" },
    ] }),
    "101": record("mate@example.com"),
    "102": record("stranger@example.com"),
  } });
  assert.deepEqual((await registry.verifyTeammates("100",
    [{ crd: "101", email: "mate@example.com" }])).map((row) => row.crd), ["101"]);
  await assert.rejects(registry.verifyTeammates("100",
    [{ crd: "102", email: "stranger@example.com" }]),
    (error) => error.code === "teammate_not_approved");
});
