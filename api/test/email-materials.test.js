"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const m = require("../shared/email-materials");

test("only five material channels are accepted", () => {
  assert.deepEqual(m.CHANNELS, ["generic", "ubs", "mswm", "ml", "rj"]);
  assert.throws(() => m.channel("wfa"), /Unknown material channel/);
});
test("domain policy normalizes, disables, de-duplicates and rejects conflicts", () => {
  const p = m.validateRoutes({ rules: [
    { domain: "@UBS.COM.", channel: "UBS" }, { domain: "ubs.com", channel: "ubs" },
    { domain: "old.example.com", channel: "rj", disabled: true },
  ] });
  assert.deepEqual(p.rules, [{ domain: "old.example.com", channel: "rj", status: "disabled", source: "Administrator", evidenceCount: 0, disabled: true },
    { domain: "ubs.com", channel: "ubs", status: "active", source: "Administrator", evidenceCount: 0 }]);
  assert.throws(() => m.validateRoutes({ rules: [
    { domain: "same.com", channel: "rj" }, { domain: "same.com", channel: "ml" },
  ] }), (e) => e.code === "material_domain_conflict");
  assert.throws(() => m.validateRoutes({ rules: [{ domain: "person@ubs.com", channel: "ubs" }] }));
});
test("routing uses canonical email domain and longest matching parent", () => {
  const p = { rules: [{ domain: "firm.com", channel: "rj" }, { domain: "wealth.firm.com", channel: "ubs" }] };
  assert.equal(m.resolveChannel("A@sub.wealth.firm.com", p), "ubs");
  assert.equal(m.resolveChannel("a@unknown.com", p), "generic");
});
test("families select channel variant and only explicit generic fallback", () => {
  const docs = [
    { id: "u", familyId: "case", channel: "ubs", periodKey: "2026-07", freshness: "current", approved: true, version: 1 },
    { id: "g", familyId: "case", channel: "generic", periodKey: "2026-07", freshness: "current", approved: true, version: 1, genericFallbackChannels: ["rj"] },
  ], p = { rules: [{ domain: "ubs.com", channel: "ubs" }, { domain: "rjf.com", channel: "rj" }, { domain: "ml.com", channel: "ml" }] };
  assert.equal(m.resolveFamilies(docs, ["case"], "a@ubs.com", p).documents[0].id, "u");
  assert.equal(m.resolveFamilies(docs, ["case"], "a@rjf.com", p).documents[0].id, "g");
  assert.throws(() => m.resolveFamilies(docs, ["case"], "a@ml.com", p), (e) => e.code === "material_variant_unavailable");
});
test("stale variants are never selected and newest current period wins", () => {
  const docs = [
    { id: "old", familyId: "deck", channel: "generic", periodKey: "2026-Q1", freshness: "stale", approved: true, version: 2 },
    { id: "q2", familyId: "deck", channel: "generic", periodKey: "2026-Q2", freshness: "current", approved: true, version: 1 },
  ];
  assert.equal(m.resolveFamilies(docs, ["deck"], "a@example.com", { rules: [] }).documents[0].id, "q2");
});
test("disabled tombstones remain editable but never route recipients", () => {
  const p = m.validateRoutes({ rules: [{ domain: "ubs.com", channel: "ubs", disabled: true }] });
  assert.deepEqual(p.rules, [{ domain: "ubs.com", channel: "ubs", status: "disabled",
    source: "Administrator", evidenceCount: 0, disabled: true }]);
  assert.equal(m.resolveChannel("a@ubs.com", p), "generic");
});

test("replacing PDF bytes inherits categorization when an older client omits metadata", () => {
  const old = { familyId: "case-value", category: "case", channel: "ubs",
    periodKey: "2026-07", periodKind: "month", asOfDate: "2026-07-31",
    freshness: "current", genericFallbackChannels: ["rj"] };
  assert.deepEqual(m.replacementMetadata({}, old), old);
  assert.deepEqual(m.replacementMetadata({ channel: "ml", familyId: "" }, old),
    { ...old, familyId: "", channel: "ml" });
});

test("quarter freshness uses the latest completed calendar quarter", () => {
  const august = Date.parse("2026-08-28T12:00:00Z");
  assert.equal(m.latestCompletedQuarter(august), "2026-Q2");
  assert.equal(m.freshnessOf({ approved: true, freshness: "current", periodKind: "quarter", periodKey: "2026-Q1" }, august), "stale");
  assert.equal(m.freshnessOf({ approved: true, freshness: "current", periodKind: "quarter", periodKey: "2026-Q2" }, august), "current");
  assert.equal(m.freshnessOf({ approved: true, freshness: "current", periodKind: "quarter", periodKey: "2026-Q3" }, august), "future");
  assert.equal(m.freshnessOf({ approved: true, freshness: "withdrawn", periodKind: "quarter", periodKey: "2026-Q2" }, august), "withdrawn");
});

test("all explicit lifecycle states and as-of periods survive normalization", () => {
  for (const freshness of ["current", "stale", "superseded", "expired", "withdrawn", "future"])
    assert.equal(m.normalizeMetadata({ freshness, periodKind: "as_of" }).freshness, freshness);
});

test("route provenance and seed version survive safe normalization", () => {
  const p = m.validateRoutes({ seedVersion: " seed-v1\u0000 ", rules: [{ domain: "rjf.com",
    channel: "rj", source: "Roster\u0000 seed", evidenceCount: 42.9, status: "active" }] });
  assert.equal(p.seedVersion, "seed-v1");
  assert.deepEqual(p.rules[0], { domain: "rjf.com", channel: "rj", status: "active",
    source: "Roster  seed", evidenceCount: 42, seedVersion: "seed-v1" });
});

test("packaged seed exposes active provenance and admin rules are labelled", () => {
  const seed = m.loadSeed();
  assert.ok(seed.seedVersion);
  assert.ok(seed.rules.length > 0);
  assert.equal(seed.rules.every((r) => r.status === "active" && r.source === "Roster seed"), true);
  assert.equal(m.validateRoutes({ rules: [{ domain: "new.example", channel: "ml" }] }).rules[0].source,
    "Administrator");
});

test("bare multi-label public suffixes cannot route entire countries", () => {
  for (const suffix of ["co.uk", "org.uk", "ac.uk", "net.au", "org.au", "co.nz",
    "co.jp", "com.br", "co.za", "com.mx", "org.mx"]) {
    assert.throws(() => m.validateRoutes({ rules: [{ domain: suffix, channel: "rj" }] }),
      /Invalid material-routing domain/, suffix);
  }
  assert.equal(m.validateRoutes({ rules: [{ domain: "wealth.co.uk", channel: "rj" }] })
    .rules[0].domain, "wealth.co.uk", "ordinary firm domains below a public suffix stay valid");
});

test("legacy unperioded documents remain current but every noncurrent state is blocked", () => {
  assert.equal(m.currentDocument({ approved: true, freshness: "current" }), true);
  for (const freshness of ["stale", "superseded", "expired", "withdrawn", "future"])
    assert.equal(m.currentDocument({ approved: true, freshness }), false);
});
