"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const read = (name) => fs.readFileSync(path.join(__dirname, "..", "..", "webapp", name), "utf8");
const desk = read("app.js");
const field = read("field.js");
const dial = read("dial.js");

test("Analyst uses the same shield-with-check drawing on desk and field", () => {
  for (const source of [desk, field]) {
    assert.match(source, /const SHIELD_PATH =/);
    assert.match(source, /const CHECK_PATH =/);
    assert.match(source, /"Analyst", SHIELD_PATH/);
    assert.doesNotMatch(source, /"Analyst", DOC_PATH/);
  }
});

test("role projections have reserved ids and name aliases are migration-only", () => {
  for (const source of [desk, field]) {
    assert.match(source, /ROLE_LIST_IDS = \{ key: "role-key", dd: "role-analyst", scheduler: "role-scheduler" \}/);
    assert.match(source, /const legacy = LEGACY_ROLE_IDS\[id\]/);
    assert.match(source, /STANDING_NAMES\[String\(l\.name \|\| ""\)/);
    assert.match(source, /filter\(\(list\) => !standingKindOf\(list\.id\)\)/);
  }
});

test("role rebuild refreshes summaries and deletes a zero-eligible projection", () => {
  assert.match(dial, /async function replaceList/);
  assert.match(dial, /opts && opts\.deleteIfEmpty/);
  assert.match(dial, /await deleteList\(state\.listId\)/);
  assert.match(dial, /await loadLists\(\)/);
  for (const source of [desk, field]) {
    assert.match(source, /Dial\.replaceList/);
    assert.match(source, /deleteIfEmpty: true/);
    assert.match(source, /active \? (?:Dial\.state|S)\.items\.length/);
  }
});

test("the active derived role is explicitly selected and an empty active role is retired", () => {
  for (const source of [desk, field]) {
    assert.match(source, /const selected = active \? " selected" : ""/);
    assert.match(source, /standingCleanup = Dial\.deleteList\(S\.listId\)/);
    assert.match(source, /!flagged(?:Advisors|Field)\(activeStanding\)\.length/);
  }
});

test("both views refresh role flags when resuming after work on another device", () => {
  assert.match(desk, /visibilitychange[\s\S]*await Dial\.fetchFlags\(\)[\s\S]*renderDialer\(\)/);
  assert.match(field, /visibilitychange[\s\S]*await Dial\.fetchFlags\(\)[\s\S]*await Dial\.refreshQueue\(\)/);
});
