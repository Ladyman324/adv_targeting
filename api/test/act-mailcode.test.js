"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const act = require("../shared/act");

test("Mail Code ranking orders the picklist from least to most restrictive", () => {
  const R = act.MAIL_CODE_RANK;
  // Straight from the EIC picklist. U is the strongest value in the list, which
  // is why an unsubscribe targets it.
  assert.ok(R["1"] < R["3"], "'Email and hard copy' is looser than 'Hard Copy Only'");
  assert.ok(R["3"] < R["N"], "'Hard Copy Only' is looser than 'No mail'");
  assert.ok(R["N"] <= R["NC"], "'No mail' is not stronger than 'No mail by request'");
  assert.equal(R["U"], Math.max(...Object.values(R)), "U must be the strongest");
});

test("never-downgrade: an unsubscribe only ever tightens the Mail Code", () => {
  const R = act.MAIL_CODE_RANK, target = R["U"];
  const wouldWrite = (current) => (R[String(current || "").trim().toUpperCase()] || 0) < target;
  // Everything looser than U gets written...
  for (const c of ["", "1", "2", "3", "C", "P", "N", "NC", "  1  ", "u nknown"])
    assert.equal(wouldWrite(c), true, `expected a write over ${JSON.stringify(c)}`);
  // ...and an existing opt-out is left exactly as it is, in either case.
  for (const c of ["U", "u", " U "]) assert.equal(wouldWrite(c), false, `must not rewrite ${JSON.stringify(c)}`);
});

test("nothing is written to a contact field unless the property name is configured", () => {
  // The API property behind the "Mail Code" LABEL is not knowable from the Act!
  // UI, and an earlier version of this integration shipped an invented field
  // name. Unconfigured must mean "write no field", not "guess".
  const saved = process.env.ACT_MAIL_CODE_FIELD;
  delete process.env.ACT_MAIL_CODE_FIELD;
  try {
    const src = require("fs").readFileSync(require.resolve("../shared/act.js"), "utf8");
    const fn = src.split("async function setMailCode")[1].split(String.fromCharCode(10) + "}")[0];
    assert.match(fn, /if \(!field\) return \{ attempted: false/);
    // And it must send the WHOLE entity back, not a two-field patch, because
    // Act!'s PATCH semantics are unconfirmed. Cloned rather than spread, so the
    // nested customFields object is copied instead of shared.
    assert.match(fn, /JSON\.parse\(JSON\.stringify\(contact\)\)/);
    assert.match(fn, /"PUT"/);
    assert.doesNotMatch(fn, /"PATCH"/);
  } finally { if (saved !== undefined) process.env.ACT_MAIL_CODE_FIELD = saved; }
});

test("the opt-out writes history before it touches any field", () => {
  const src = require("fs").readFileSync(require.resolve("../shared/act.js"), "utf8");
  const fn = src.split("async function markDoNotEmail")[1];
  const history = fn.indexOf("api/tasks/");
  const field = fn.indexOf("setMailCode(");
  assert.ok(history > -1 && field > -1, "both writes should be present");
  assert.ok(history < field, "history must be written before the Mail Code field");
});

test("the invented field names are gone for good", () => {
  const src = require("fs").readFileSync(require.resolve("../shared/act.js"), "utf8");
  const code = src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*/g, "");
  assert.doesNotMatch(code, /doNotEmail/);
  assert.doesNotMatch(code, /contactNotes/);
});
