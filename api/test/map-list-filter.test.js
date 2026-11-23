"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const app = fs.readFileSync(path.join(__dirname, "../../webapp/app.js"), "utf8");
const section = (start, end) => app.slice(app.indexOf(start), app.indexOf(end, app.indexOf(start)));
const feature = (id, longitude) => ({ properties:{ id }, geometry:{ coordinates:[longitude, 33] } });

test("a saved list filters advisor pins and saves only its visible members to a new list", async () => {
  const act = { contactKey:"act-00000000000000000000000001", coordinates:[33, -84] };
  const items = [{ crd:"100" }, { crd:"200" }, { crd:act.contactKey }];
  let west = -85;
  const writes = [];
  const pass = () => true;
  const context = {
    scope:"GA", listWorkspace:{ owner:{ id:"owner" } }, ALL:[feature("100", -84), feature("100", -84.1), feature("200", -82), feature("300", -84)],
    map:{ getBounds:() => ({ contains:([, lon]) => lon >= west && lon <= -83 }) },
    Dial:{ MAX_QUEUE:1000, readList:async (id, owner) => {
      assert.equal(id, "prospects"); assert.equal(owner, "owner"); return { name:"Prospects", items };
    }, saveNewList:async (name, rows, owner) => {
      writes.push({ name, ids:rows.map(row => row.crd).join(","), owner });
      return { name, count:rows.length };
    }, isDnc:() => false },
    ActAddresses:{ loadListContacts:async () => [act] },
    visibleListActContacts:() => west <= -84 ? [act] : [],
    selectsOnly:true, syncTargetingUI(){}, actContactMarker:null, selectedFirms:[],
    excludedFirms:new Set(), reg:"all", closeListManager(){}, openListManager(){},
    redraw(){}, reportFilterReach(){}, showNotice(){},
    prompt:() => "Atlanta visit", loadContacts:async () => true,
    dialSnapshot:crd => ({ crd, phone:"", email:"recipient@example.test" }),
    passesContinental:pass, passesTargeting:pass, passesLastEmailed:pass, passesLastCalled:pass,
    passesJoinedFirm:pass, passesOwner:pass, passesRanked:pass, passesRole:pass,
    passesGeography:pass, passesContactable:pass, passesHasAssets:pass,
  };
  vm.createContext(context);
  vm.runInContext(section("let mapListFilter = null;", "/* Where should a bulk add land?")
    + section("function passesBase(", "function colorFor("), context);
  await context.filterMapByList("prospects", "owner");
  assert.equal(context.selectsOnly, false);
  assert.equal(context.passesFilters({ id:"300" }), false);
  assert.equal(context.passesFilters({ id:"200" }), true);
  assert.equal(context.visibleAdvisorIds().join(","), "100," + act.contactKey);
  await context.saveListInView();
  assert.deepEqual(writes, [{ name:"Atlanta visit", ids:"100," + act.contactKey, owner:"owner" }]);
  assert.equal(items.length, 3);
  west = -83.5;
  assert.equal(context.visibleAdvisorIds().length, 0);
  await context.saveListInView();
  assert.equal(writes.length, 1);
});
