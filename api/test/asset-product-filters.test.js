"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const products = require("../../webapp/asset-product-filters");

test("Any EIC and specific products are mutually exclusive; last selection turns off", () => {
  assert.deepEqual(products.transition(false, [], "any"), {assetsOnly:true,assetProducts:[]});
  assert.deepEqual(products.transition(true, [], "any"), {assetsOnly:false,assetProducts:[]});
  assert.deepEqual(products.transition(true, [], "acv"), {assetsOnly:true,assetProducts:["acv"]});
  assert.deepEqual(products.transition(true, ["acv"], "lcv"), {assetsOnly:true,assetProducts:["acv","lcv"]});
  assert.deepEqual(products.transition(true, ["acv","lcv"], "any"), {assetsOnly:true,assetProducts:[]});
  assert.deepEqual(products.transition(true, ["acv"], "acv"), {assetsOnly:false,assetProducts:[]});
});
test("canonical products match positive holdings with OR semantics, not strategy conflation", () => {
  assert.equal(products.matches({acv:20}, ["acv","lcv"]), true);
  assert.equal(products.matches({lcv:20}, ["acv","lcv"]), true);
  assert.equal(products.matches({mf:20}, ["acv"]), false);
  assert.equal(products.matches({acv:20}, ["mf"]), false);
  assert.equal(products.matches({mf:20}, ["mf"]), true);
  assert.equal(products.matches({midcap:20}, ["midcap"]), true);
  for (const amount of [undefined, null, 0, -1, "10", Infinity, NaN, true])
    assert.equal(products.matches({acv:amount}, ["acv"]), false);
});
test("Any EIC keeps canonical book existence semantics; prospects do not match", () => {
  assert.equal(products.matches({n:1,ix:[0]}, []), true);
  for (const book of [null, undefined, "book", []]) assert.equal(products.matches(book, []), false);
});
test("normalization is canonical, strict and does not mutate the input", () => {
  const input = ["mf","acv","acv"];
  assert.deepEqual(products.normalize(input), ["acv","mf"]);
  assert.deepEqual(input, ["mf","acv","acv"]);
  assert.equal(products.label(["mf","acv"]), "ACV / EICIX");
  assert.equal(products.label([]), "Any EIC");
  for (const value of [null, "acv", {}, ["ACV"], [" acv"], ["any"], ["unknown"], [null],
      ["acv","acv","acv","acv","acv"]])
    assert.throws(() => products.normalize(value));
});
