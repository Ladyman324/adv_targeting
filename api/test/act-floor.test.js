"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

process.env.EMAIL_UNSUBSCRIBE_SECRET = "test-secret";
const suppress = require("../shared/email-suppress");
const floor = suppress.mailCodeFloor();

test("the Act! Mail Code floor is loaded and populated", () => {
  // If this is empty the guard is silently absent and 2,383 people who asked not
  // to be emailed become mailable again. Rebuild with src/build_act_mail_codes.py.
  assert.ok(floor.byAddress.size > 2000, `expected thousands of addresses, got ${floor.byAddress.size}`);
  assert.ok(floor.byCrd.size > 2000, `expected thousands of CRDs, got ${floor.byCrd.size}`);
  assert.match(floor.builtUtc, /^\d{4}-\d{2}-\d{2}/);
});

test("an opt-out is caught by address", () => {
  const [address, code] = [...floor.byAddress][0];
  assert.equal(suppress.floorCodeFor({ email: address }), code);
  assert.equal(suppress.floorCodeFor({ email: address.toUpperCase() }), code, "matching is case-insensitive");
});

test("an opt-out is caught by CRM contact id even with a different address", () => {
  // The case address matching cannot see: Act! users overwrite the email field
  // with a note ("unsubscribed 3/27/26"), destroying the address but not the
  // opt-out, while we still hold a working address from SEC data. 823 people on
  // the 2026-08-13 export were reachable only this way.
  const [crd, code] = [...floor.byCrd][0];
  assert.equal(suppress.floorCodeFor({ email: "someone-else@example.com", contactId: crd }), code);
  assert.equal(suppress.floorCodeFor({ email: "someone-else@example.com", crd }), code,
    "accepts either key name");
});

test("an unaffected recipient is not blocked", () => {
  assert.equal(suppress.floorCodeFor({ email: "brand.new@example.com", contactId: "0000000" }), null);
  assert.equal(suppress.floorCodeFor({}), null);
  assert.equal(suppress.floorCodeFor(null), null);
});

test("every floor code maps to a stated reason, not a shrug", () => {
  const codes = new Set([...floor.byAddress.values(), ...floor.byCrd.values()]);
  assert.deepEqual([...codes].sort(), ["BB", "N", "NC", "U"]);
});

test("the Mail Code write walks a dotted path and never downgrades", () => {
  const src = require("fs").readFileSync(require.resolve("../shared/act.js"), "utf8");
  const fn = src.split("async function setMailCode")[1].split(String.fromCharCode(10) + "}")[0];
  // The real field is nested, so a flat contact[field] lookup would find nothing
  // and report "no such property" forever without ever writing.
  assert.match(fn, /field\.split\("\."\)/);
  assert.match(fn, /rankNow >= MAIL_CODE_RANK/);
  assert.match(fn, /"PUT"/);
  assert.doesNotMatch(fn, /"PATCH"/);
});
