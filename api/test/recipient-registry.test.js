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

test("only direct-confirmed identities resolve", async () => {
  registry.useIndex({ recipients: {
    "100": record("high@example.com", "high"),
    "101": record("confirmed@example.com", "confirmed"),
    "102": record("review@example.com", "review"),
  } });
  assert.equal((await registry.resolve("101")).email, "confirmed@example.com");
  await assert.rejects(registry.resolve("100"),
    (error) => error.code === "recipient_not_approved");
  await assert.rejects(registry.resolve("102"),
    (error) => error.code === "recipient_not_approved");
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
