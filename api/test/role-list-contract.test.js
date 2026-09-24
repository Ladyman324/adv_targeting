"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const read = (name) => fs.readFileSync(path.join(__dirname, "..", "..", "webapp", name), "utf8");
const desk = read("app.js");
const index = read("index.html");
const field = read("field.js");
const dial = read("dial.js");

test("Key Person and Research use the same drawings on desk and field", () => {
  for (const source of [desk, field]) {
    assert.match(source, /const KEY_PATH =/);
    assert.match(source, /const SEARCH_PATH =/);
    assert.match(source, /const SEARCH_HANDLE_PATH =/);
    assert.match(source, /"Key Person", KEY_PATH/);
    assert.match(source, /"Research", SEARCH_PATH/);
    assert.doesNotMatch(source, /"Research", SHIELD_PATH/);
  }
});

test("desktop role filters are multi-selectable and persist in dynamic audiences", () => {
  assert.match(index, /id="roleToggle"[\s\S]*data-role="key"[\s\S]*data-role="dd"[\s\S]*data-role="scheduler"/);
  assert.match(desk, /roleSel\.has\("key"\)[\s\S]*\|\|[\s\S]*roleSel\.has\("dd"\)[\s\S]*\|\|[\s\S]*roleSel\.has\("scheduler"\)/);
  assert.match(desk, /roles:\[\.\.\.roleSel\]/);
  assert.match(desk, /refill\(roleSel, f\.roles\)/);
});

test("dynamic audiences distinguish personal ownership from advisor territory coverage", () => {
  assert.match(desk, /kind:"cross_territory", rows:preview\.rows/);
  assert.match(desk, /kind:"unassigned", rows:\[\], outside:preview\.matches/);
  assert.match(desk, /Advisor territory coverage:/);
  assert.match(desk, /crossTerritoryLists === true/);
  assert.doesNotMatch(desk, /kind:"administrator", rows:preview\.rows/);
  assert.doesNotMatch(desk, /Owner distribution:/);
});
test("building rosters can sort by team and show job titles instead of registration badges", () => {
  assert.match(desk, /data-roster-sort="team"/);
  assert.match(desk, /contactFor\(x\.properties\.id\)\?\.tn/);
  assert.match(desk, /contact\.ti/);
  assert.doesNotMatch(desk, /const bits = \[\s*p\.d \? "Dually registered" : "RIA-only"/);
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
