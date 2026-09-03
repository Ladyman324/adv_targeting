"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const m = require("../shared/email-materials");

test("only five material channels are accepted", () => {
  assert.deepEqual(m.CHANNELS, ["generic", "ubs", "mswm", "ml", "rj"]);
  assert.throws(() => m.channel("wfa"), /Unknown material channel/);
});
test("material audience and strategy vocabulary is bounded", () => {
  assert.deepEqual(m.AUDIENCES, ["advisor_only", "client"]);
  assert.deepEqual(m.STRATEGIES, ["general", "acv", "lcv", "combined"]);
  assert.equal(m.audience("Advisor Only"), "advisor_only");
  assert.equal(m.strategy("mutual fund"), "acv");
  assert.equal(m.strategy("all-cap"), "acv");
  assert.throws(() => m.audience("public"), /Unknown material audience/);
  assert.throws(() => m.strategy("midcap"), /Unknown material strategy/);
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
test("Client material wins within the same document slot", () => {
  const docs = [
    { id: "advisor", familyId: "commentary", channel: "generic", strategy: "general",
      audience: "advisor_only", periodKey: "2026-Q2", asOfDate: "2026-07-10", freshness: "current", approved: true, version: 9 },
    { id: "client", familyId: "commentary", channel: "generic", strategy: "general",
      audience: "client", periodKey: "2026-Q2", asOfDate: "2026-07-28", freshness: "current", approved: true, version: 1 },
  ];
  assert.equal(m.resolveFamilies(docs, ["commentary"], "a@example.com", { rules: [] })
    .documents[0].id, "client");
  assert.equal(m.materialSlotKey(docs[0]), m.materialSlotKey(docs[1]),
    "audience and differing approval dates do not split one quarterly slot");
  assert.notEqual(m.materialSlotKey(docs[0]), m.materialSlotKey({ ...docs[0], strategy: "lcv" }));
});

test("Raymond James commentary follows holdings and defaults only no-holding RJ recipients to LCV", () => {
  const docs = [
    { id: "rj-acv", familyId: "commentary", channel: "rj", strategy: "acv", audience: "client", freshness: "current", approved: true },
    { id: "rj-lcv", familyId: "commentary", channel: "rj", strategy: "lcv", audience: "client", freshness: "current", approved: true },
  ], policy = { rules: [{ domain: "rjf.com", channel: "rj" }] };
  const ids = (strategies) => m.resolveFamilies(docs, ["commentary"], "advisor@rjf.com", policy, { strategies })
    .documents.map((doc) => doc.id);
  assert.deepEqual(ids([]), ["rj-lcv"]);
  assert.deepEqual(ids(["acv"]), ["rj-acv"]);
  assert.deepEqual(ids(["lcv"]), ["rj-lcv"]);
  assert.deepEqual(ids(["acv", "lcv"]), ["rj-acv", "rj-lcv"]);
});

test("Merrill commentary uses the All-Cap client version", () => {
  const docs = [
    { id: "ml-acv", familyId: "commentary", channel: "ml", strategy: "acv", audience: "client", freshness: "current", approved: true },
    { id: "ml-lcv", familyId: "commentary", channel: "ml", strategy: "lcv", audience: "client", freshness: "current", approved: true },
  ], policy = { rules: [{ domain: "ml.com", channel: "ml" }] };
  assert.deepEqual(m.resolveFamilies(docs, ["commentary"], "advisor@ml.com", policy).documents.map((d) => d.id),
    ["ml-acv"]);
});

test("UBS commentary requires combined material and Large-Cap is not a global default", () => {
  const docs = [
    { id: "ubs-acv", familyId: "commentary", channel: "ubs", strategy: "acv", freshness: "current", approved: true },
    { id: "ubs-lcv", familyId: "commentary", channel: "ubs", strategy: "lcv", freshness: "current", approved: true },
    { id: "ubs-combined", familyId: "commentary", channel: "ubs", strategy: "combined", freshness: "current", approved: true },
    { id: "generic", familyId: "commentary", channel: "generic", strategy: "general", freshness: "current", approved: true },
    { id: "generic-lcv", familyId: "commentary", channel: "generic", strategy: "lcv", freshness: "current", approved: true },
  ], policy = { rules: [{ domain: "ubs.com", channel: "ubs" }] };
  assert.deepEqual(m.resolveFamilies(docs, ["commentary"], "advisor@ubs.com", policy).documents.map((d) => d.id),
    ["ubs-combined"]);
  assert.deepEqual(m.resolveFamilies(docs, ["commentary"], "advisor@independent.com", policy).documents.map((d) => d.id),
    ["generic"]);
});
test("stale variants are never selected and newest current period wins", () => {
  const docs = [
    { id: "old", familyId: "deck", channel: "generic", periodKey: "2026-Q1", freshness: "stale", approved: true, version: 2 },
    { id: "q2", familyId: "deck", channel: "generic", periodKey: "2026-Q2", freshness: "current", approved: true, version: 1 },
  ];
  assert.equal(m.resolveFamilies(docs, ["deck"], "a@example.com", { rules: [] }).documents[0].id, "q2");
});

test("template requirements migrate family documents but keep standalone PDFs exact", () => {
  const docs = [
    { id: "case-ubs-q2", familyId: "case-value", channel: "ubs" },
    { id: "tax-policy", familyId: "", channel: "generic" },
  ];
  assert.deepEqual(m.templateRequirements({
    requiredMaterialFamilyIds: ["quarterly-update"],
    requiredDocumentIds: ["case-ubs-q2", "tax-policy"],
  }, docs), {
    familyIds: ["quarterly-update", "case-value"],
    documentIds: ["tax-policy"],
    missingDocumentIds: [],
  });
});

test("template requirement migration reports removed legacy documents", () => {
  assert.deepEqual(m.templateRequirements({ requiredDocumentIds: ["gone"] }, []), {
    familyIds: [], documentIds: [], missingDocumentIds: ["gone"],
  });
});

test("exact template requirements prevent their document from being removed", () => {
  const templates = [
    { name: "Series based", requiredMaterialFamilyIds: ["commentary"], requiredDocumentIds: [] },
    { name: "Exact PDF", requiredDocumentIds: ["standard-deviation"] },
    { name: "Legacy exact PDF", requiredDocumentIds: [], defaultAttachmentIds: ["legacy-deck"] },
  ];
  assert.deepEqual(m.templatesRequiringDocument(templates, "standard-deviation").map((t) => t.name), ["Exact PDF"]);
  assert.deepEqual(m.templatesRequiringDocument(templates, "legacy-deck").map((t) => t.name), ["Legacy exact PDF"]);
  assert.deepEqual(m.templatesRequiringDocument(templates, "commentary"), []);
});

test("legacy default attachments are used when the newer exact list is empty", () => {
  assert.deepEqual(m.templateRequirements({
    requiredDocumentIds: [], defaultAttachmentIds: ["case-generic"],
  }, [{ id: "case-generic", familyId: "case-value" }]), {
    familyIds: ["case-value"], documentIds: [], missingDocumentIds: [],
  });
});
test("disabled tombstones remain editable but never route recipients", () => {
  const p = m.validateRoutes({ rules: [{ domain: "ubs.com", channel: "ubs", disabled: true }] });
  assert.deepEqual(p.rules, [{ domain: "ubs.com", channel: "ubs", status: "disabled",
    source: "Administrator", evidenceCount: 0, disabled: true }]);
  assert.equal(m.resolveChannel("a@ubs.com", p), "generic");
});

test("replacing PDF bytes inherits categorization when an older client omits metadata", () => {
  const old = { familyId: "case-value", familyName: "Case for Value", category: "case", channel: "ubs",
    audience: "advisor_only", strategy: "general", periodKey: "2026-07", periodKind: "month", asOfDate: "2026-07-31",
    freshness: "current", genericFallbackChannels: ["rj"] };
  assert.deepEqual(m.replacementMetadata({}, old), old);
  assert.deepEqual(m.replacementMetadata({ channel: "ml", familyId: "" }, old),
    { ...old, familyId: "", channel: "ml" });
});

test("family display names are safe metadata and inherit across replacement uploads", () => {
  assert.equal(m.normalizeMetadata({ familyName: "  Case for Value\u0000  " }).familyName, "Case for Value");
  const existing = { familyId: "case-value", familyName: "Case for Value" };
  assert.equal(m.replacementMetadata({}, existing).familyName, "Case for Value");
  assert.equal(m.replacementMetadata({ familyName: "Value vs. Growth" }, existing).familyName, "Value vs. Growth");
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
