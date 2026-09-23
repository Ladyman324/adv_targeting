"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const fs = require("node:fs");
const Module = require("node:module");
const importer = require("../../webapp/list_import.js");

test("import controls stay themed and the review button cannot collapse", () => {
  const css = fs.readFileSync(path.resolve(__dirname, "../../webapp/style.css"), "utf8");
  const app = fs.readFileSync(path.resolve(__dirname, "../../webapp/app.js"), "utf8");
  assert.match(css, /\.list-import-workspace>\*\{flex-shrink:0\}/);
  assert.match(css, /\.list-import-field select\{[^}]*background:var\(--panel-2\)/);
  assert.match(css, /\.list-import-review-download\{[^}]*min-height:38px/);
  assert.match(app, /class="ask-btn list-import-review-download" data-lists="import-exceptions"/);
});

test("CSV import reads Excel-style quoting, removes duplicates, and rejects malformed rows", () => {
  const parsed = importer.parseEmails(
    '\uFEFFName;Email Address\r\n"A, One";A.One@example.com\r\n'
    + '"B; Two";b.two@example.com\r\nRepeat;a.one@EXAMPLE.com\r\n'
    + 'Bad;not-an-email\r\n');
  assert.deepEqual(parsed.emails, ["a.one@example.com", "b.two@example.com"]);
  assert.equal(parsed.duplicateRows, 1);
  assert.equal(parsed.invalidRows, 1);
  assert.deepEqual(importer.parseEmails("person@example.com\n").emails,
    ["person@example.com"]);
  assert.throws(() => importer.parseEmails('Email\n"unfinished'), /unclosed/);
});

test("email-only match excludes ambiguous, unapproved, and unmapped advisor records", () => {
  const advisors = {
    1: { e:"leader@example.com", t:"high", fc:"250", cn:"Edward Jones" },
    2: { e:"shared@example.com", t:"high", fc:"250" },
    3: { e:"shared@example.com", t:"high", fc:"250" },
    4: { e:"review@example.com", t:"review", fc:"250" },
    5: { e:"offmap@example.com", t:"high", fc:"250" },
  };
  const index = { cities:["Boston"], advisors:[
    ["1","Leader",0,"MA",0,"MA",""], ["2","Shared",0,"MA",0,"MA",""],
    ["3","Shared",0,"MA",0,"MA",""], ["4","Review",0,"MA",0,"MA",""],
    ["5","Off Map",0,"XX",0,"XX",""],
  ] };
  const result = importer.resolve([
    "leader@example.com", "shared@example.com", "review@example.com",
    "offmap@example.com", "missing@example.com",
  ], advisors, index, state => state === "MA" ? "Northeast" : "",
  contact => contact.t === "high");
  assert.deepEqual(result.matched.map(row => row.crd), ["1"]);
  assert.equal(result.matched[0].territory, "Northeast");
  assert.deepEqual(result.ambiguous, ["shared@example.com"]);
  assert.deepEqual(result.ineligible, ["review@example.com"]);
  assert.deepEqual(result.noTerritory, ["offmap@example.com"]);
  assert.deepEqual(result.unmatched, ["missing@example.com"]);
});

test("ranked additions stay at imported firms and dedupe CRD and email", () => {
  const matched = [{ crd:"1", email:"leader@example.com", firmCrd:"250", territory:"Northeast" }];
  const advisors = {
    1:{ e:"leader@example.com", fc:"250", t:"high" },
    2:{ e:"ranked@example.com", fc:"250", t:"high" },
    3:{ e:"outside@example.com", fc:"999", t:"high" },
    4:{ e:"ranked@example.com", fc:"250", t:"high" },
  };
  const index = { cities:["Boston"], advisors:[
    ["1","Leader",0,"MA",0,"MA",""], ["2","Ranked",0,"MA",0,"MA",""],
    ["3","Other Firm",0,"MA",0,"MA",""], ["4","Shared",0,"MA",0,"MA",""],
  ] };
  const result = importer.unionRanked(matched, advisors, index,
    new Set(["1","2","3","4"]), () => "Northeast",
    contact => contact.t === "high");
  assert.equal(result.added, 1);
  assert.deepEqual(result.people.map(row => row.crd), ["1","2"]);
  const national = importer.unionRanked(matched, advisors, index,
    new Set(["1","2","3","4"]), () => "Northeast",
    contact => contact.t === "high", true);
  assert.deepEqual(national.people.map(row => row.crd), ["1","2","3"]);
});

function flagsHandler(fakeStore) {
  const filename = require.resolve("../flags/index.js");
  delete require.cache[filename];
  const original = Module._load;
  Module._load = function (request, parent) {
    if (request === "../shared/store" && parent && path.resolve(parent.filename) === filename)
      return fakeStore;
    return original.apply(this, arguments);
  };
  try { return require(filename); }
  finally { Module._load = original; }
}

test("bulk role labeling validates the whole request and reports partial failures", async () => {
  const calls = [];
  const handler = flagsHandler({
    identity:() => ({ id:"rep", name:"Rep" }),
    setFlag:async (who, crd, kind, on) => {
      calls.push([crd, kind, on]);
      if (crd === "2") throw new Error("storage unavailable");
      return { crd };
    },
    ok:(context, body) => { context.res = { status:200, body }; },
    fail:(context, error) => { context.res = { status:error.statusCode || 500 }; },
  });
  const invalid = {};
  await handler(invalid, { method:"POST", body:{ kind:"key",
    entries:[{ crd:"1" }, { crd:"1" }] } });
  assert.equal(invalid.res.status, 400);
  assert.equal(calls.length, 0);
  const context = {};
  await handler(context, { method:"POST", body:{ kind:"dd",
    entries:[{ crd:"1" }, { crd:"2" }] } });
  assert.equal(context.res.status, 200);
  assert.equal(context.res.body.updated, 1);
  assert.deepEqual(context.res.body.failed, ["2"]);
  assert.deepEqual(calls, [["1","dd",true], ["2","dd",true]]);
});
