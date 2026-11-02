"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const Module = require("node:module");
const { normalizeDefinition } = require("../shared/audience-definition");

const raw = (filters = {}) => ({ version: 1,
  scope: { kind: "state", value: "NY", states: ["NY"] }, filters });
const normalize = (filters) => normalizeDefinition(raw(filters)).definition.filters;

test("legacy and empty product selections preserve Any EIC or unrestricted semantics", () => {
  for (const assetProducts of [undefined, []]) {
    for (const assetsOnly of [false, true]) {
      const filters = normalize({ assetsOnly, assetProducts });
      assert.equal(filters.assetsOnly, assetsOnly);
      assert.deepEqual(filters.assetProducts, []);
    }
  }
  assert.equal(normalize({}).assetsOnly, false);
});

test("specific product selections are canonical, bounded, and imply assetsOnly", () => {
  for (const product of ["acv", "lcv", "midcap", "mf"]) {
    const filters = normalize({ assetProducts: [product] });
    assert.equal(filters.assetsOnly, true);
    assert.deepEqual(filters.assetProducts, [product]);
  }
  const filters = normalize({ assetsOnly: false,
    assetProducts: ["mf", "midcap", "acv", "lcv"] });
  assert.equal(filters.assetsOnly, true);
  assert.deepEqual(filters.assetProducts, ["acv", "lcv", "midcap", "mf"]);
  assert.deepEqual(normalize({ assetProducts: ["lcv", "acv", "lcv"] }).assetProducts,
    ["acv", "lcv"]);
  const once = normalizeDefinition(raw(filters));
  assert.deepEqual(normalizeDefinition(JSON.parse(once.json)), once);
});

test("version 2 protects product rules from legacy clients and empty rules remain v1", () => {
  for (const version of [1, 2]) {
    for (const assetProducts of [undefined, []]) {
      for (const assetsOnly of [false, true]) {
        const normalized = normalizeDefinition({ ...raw({ assetsOnly, assetProducts }), version });
        assert.equal(normalized.definition.version, 1);
        assert.equal(normalized.definition.filters.assetsOnly, assetsOnly);
        assert.deepEqual(normalizeDefinition(JSON.parse(normalized.json)), normalized);
      }
    }
    const normalized = normalizeDefinition({ ...raw({ assetProducts: ["mf", "acv"] }), version });
    assert.equal(normalized.definition.version, 2);
    assert.deepEqual(normalized.definition.filters.assetProducts, ["acv", "mf"]);
    assert.deepEqual(normalizeDefinition(JSON.parse(normalized.json)), normalized);
  }
});

test("unknown audience definition versions are refused", () => {
  for (const version of [undefined, null, 0, 3, -1, "future", 1.5]) {
    assert.throws(() => normalizeDefinition({ ...raw(), version }),
      (error) => error.statusCode === 400 && /definition\.version/.test(error.message));
  }
});

test("invalid product selections cannot silently become broader audiences", () => {
  for (const assetProducts of [null, false, "acv", {}, ["any"], ["MCV"], ["eicix"],
    ["ACV"], ["acv "], [""], [null], [123], [{}], [["acv"]],
    ["acv", "unknown"], ["acv", "lcv", "midcap", "mf", "acv"]]) {
    assert.throws(() => normalize({ assetsOnly: true, assetProducts }),
      (error) => error.statusCode === 400 && /filters\.assetProducts/.test(error.message),
      JSON.stringify(assetProducts));
  }
});

test("product criteria do not change eligibility or unrelated audience rules", () => {
  const existing = { contactableOnly: true, ownerOnly: true, rankedOnly: true,
    roles: ["dd"], excluded: ["123"], emailScope: "team", callScope: "me" };
  const unrestricted = normalize(existing);
  const restricted = normalize({ ...existing, assetProducts: ["acv", "mf"] });
  for (const key of Object.keys(unrestricted)) {
    if (key !== "assetProducts" && key !== "assetsOnly")
      assert.deepEqual(restricted[key], unrestricted[key], key);
  }
});

test("audience PUT preserves product rules and rejects invalid products before storage", async () => {
  const target = require.resolve("../audiences/index.js");
  const real = Module._load;
  const calls = [];
  const fakeStore = {
    identity: () => ({ id: "rep-one", name: "rep@example.com" }),
    putAudience: async (who, value) => { calls.push({ who, value }); return value; },
    ok: (context, body) => { context.res = { status: 200, body }; },
    fail: (context, error) => { context.res = {
      status: error.statusCode || 500, body: { error: error.message } }; },
  };
  delete require.cache[target];
  Module._load = function (request, parent) {
    if (parent && parent.filename === target && request === "../shared/store")
      return fakeStore;
    return real.apply(this, arguments);
  };
  let handler;
  try { handler = require(target); }
  finally { Module._load = real; delete require.cache[target]; }

  const context = {};
  await handler(context, { method: "PUT", query: {}, body: { name: "ACV or EICIX",
    definition: raw({ assetsOnly: false, assetProducts: ["mf", "acv"] }) } });
  assert.equal(context.res.status, 200);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].who.id, "rep-one");
  assert.equal(calls[0].value.definition.version, 2);
  assert.deepEqual(calls[0].value.definition.filters.assetProducts, ["acv", "mf"]);
  assert.equal(calls[0].value.definition.filters.assetsOnly, true);
  assert.deepEqual(JSON.parse(calls[0].value.definitionJson), calls[0].value.definition);

  const invalid = {};
  await handler(invalid, { method: "PUT", query: {}, body: {
    definition: raw({ assetProducts: ["unknown"] }) } });
  assert.equal(invalid.res.status, 400);
  assert.equal(calls.length, 1, "invalid products must never reach storage");
});
