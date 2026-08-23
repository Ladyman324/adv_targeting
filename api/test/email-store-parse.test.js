"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");

// parse() is module-private, so lift it out of the source rather than exporting
// something purely for a test.
const src = fs.readFileSync(require.resolve("../shared/email-store.js"), "utf8");
const parseSrc = src.match(/function parse\(v, fallback\) \{[\s\S]*?\n\}/)[0];
const parse = new Function("v", "fallback", `${parseSrc}; return parse(v, fallback);`);

test("parse() falls back for null, which JSON.parse accepts without throwing", () => {
  // The regression. JSON.parse(null) returns null -- null stringifies to "null",
  // which parses back to null -- so the catch never fired. Saving a NEW template
  // read images off a row that did not exist, received null instead of [], and
  // failed on images.map with "Cannot read properties of null (reading 'map')".
  assert.deepEqual(parse(null, []), []);
  assert.deepEqual(parse("null", []), []);
  assert.deepEqual(parse(undefined, []), []);
  assert.deepEqual(parse("", []), []);
  assert.deepEqual(parse("not json", []), []);
  assert.deepEqual(parse(0, []), []);
  assert.deepEqual(parse({}, []), []);
});

test("parse() still returns real values, and the fallback is not substituted for them", () => {
  assert.deepEqual(parse("[]", ["x"]), []);            // empty array is a real answer
  assert.deepEqual(parse("[1,2]", []), [1, 2]);
  assert.deepEqual(parse('{"a":1}', []), { a: 1 });
  assert.deepEqual(parse('{"errors":[],"warnings":[]}', null), { errors: [], warnings: [] });
  assert.equal(parse("false", []), false);             // falsy but valid
  assert.equal(parse("0", []), 0);
});

test("every parse() call site supplies a container fallback", () => {
  // If one ever passed null as the fallback, returning the fallback for a null
  // parse would reintroduce exactly the crash this fixed. Parens are balanced
  // rather than regexed: most of these calls sit inside object literals, so a
  // naive match to ");" finds fewer than half of them.
  const sites = [];
  for (let i = 0; i < src.length; i++) {
    if (!src.startsWith("parse(", i)) continue;
    const before = src[i - 1] || " ";
    if (/[.\w]/.test(before)) continue;               // skips JSON.parse
    if (src.slice(Math.max(0, i - 9), i) === "function ") continue;   // skips the definition
    if (src.slice(Math.max(0, i - 7), i) === "return ") continue;     // skips the test harness call
    let depth = 0, j = i + 5, args = "";
    for (; j < src.length; j++) {
      const ch = src[j];
      if (ch === "(") depth++;
      else if (ch === ")") { depth--; if (!depth) break; }
      if (depth >= 1 && !(depth === 1 && ch === "(")) args += ch;
    }
    sites.push(args);
  }
  assert.ok(sites.length >= 10, `expected the known call sites, found ${sites.length}`);
  for (const args of sites) {
    let depth = 0, split = -1;
    for (let k = 0; k < args.length; k++) {
      const ch = args[k];
      if ("([{".includes(ch)) depth++;
      else if (")]}".includes(ch)) depth--;
      else if (ch === "," && depth === 0) split = k;    // last top-level comma
    }
    assert.ok(split > -1, `parse() must be given a fallback: ${JSON.stringify(args)}`);
    const fallback = args.slice(split + 1).trim();
    assert.ok(fallback.startsWith("[") || fallback.startsWith("{"),
      `parse() fallback should be a container, got ${JSON.stringify(fallback)}`);
  }
});

test("putTemplate reads images without assuming the row exists", () => {
  const fn = src.split("async function putTemplate")[1].split(String.fromCharCode(10) + "}")[0];
  // `existing && existing.imagesJson` evaluates to null when existing is null,
  // which is what fed the crash. A ternary makes the empty case explicit.
  assert.doesNotMatch(fn, /parse\(existing && existing\.imagesJson/);
  assert.match(fn, /parse\(existing \? existing\.imagesJson : ""/);
});
