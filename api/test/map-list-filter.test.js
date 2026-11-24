"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const app = fs.readFileSync(path.join(__dirname, "../../webapp/app.js"), "utf8");
const section = (start, end) => app.slice(app.indexOf(start), app.indexOf(end, app.indexOf(start)));
const feature = (id, longitude) => ({ properties:{ id }, geometry:{ coordinates:[longitude, 33] } });

test("map list scopes count contacts once and keep selected, visible, and matching contacts distinct", async () => {
  const act = { contactKey:"act-00000000000000000000000001", coordinates:[33, -84] };
  const items = [{ crd:"100" }, { crd:"200" }, { crd:act.contactKey }];
  let west = -85;
  const noPin = { contactKey:"act-00000000000000000000000002" };
  items.push({ crd:noPin.contactKey });
  const pass = () => true;
  const context = {
    scope:"GA", listWorkspace:{ owner:{ id:"owner" } }, ALL:[feature("100", -84), feature("100", -84.1), feature("200", -82), feature("300", -84)],
    map:{ getBounds:() => ({ contains:([, lon]) => lon >= west && lon <= -83 }) },
    Dial:{ MAX_QUEUE:1000, readList:async (id, owner) => {
      assert.equal(id, "prospects"); assert.equal(owner, "owner"); return { name:"Prospects", items };
    } },
    ActAddresses:{ loadListContacts:async () => [act, noPin] },
    listActContactPassesFilters:() => true,
    visibleListActContacts:() => west <= -84 ? [act] : [],
    selectsOnly:true, syncTargetingUI(){}, actContactMarker:null, selectedFirms:[],
    excludedFirms:new Set(), reg:"all", closeListManager(){},
    redraw(){}, reportFilterReach(){},
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
  const captured = context.mapListScopes(new Set(["200", noPin.contactKey]));
  assert.equal(captured.selected.join(","), "200," + noPin.contactKey);
  assert.equal(captured.matches.join(","), ["100", "200", act.contactKey, noPin.contactKey].join(","));
  west = -83.5;
  assert.equal(context.visibleAdvisorIds().length, 0);
  assert.equal(captured.inView.join(","), "100," + act.contactKey);
  assert.equal(items.length, 4);
});

test("adding 24 contacts to a list with six already on it appends 18 in order", () => {
  const context = {};
  vm.createContext(context);
  vm.runInContext(section("function mergeListContacts(", "function visibleAdvisorIds("), context);
  const existing = Array.from({ length:6 }, (_, index) => ({ crd:index + 1, name:"Existing" }));
  const proposed = Array.from({ length:24 }, (_, index) => ({ crd:String(index + 1), name:"New" }));
  const result = context.mergeListContacts(existing, proposed);
  assert.equal(result.added, 18);
  assert.equal(result.duplicates, 6);
  assert.equal(existing.length, 6);
  assert.equal(result.items.length, 24);
  assert.equal(result.items[0], existing[0]);
  assert.equal(result.items[6], proposed[6]);
});

test("dynamic lists require regional filter rules that can be reapplied", () => {
  const context = { scope:"US", lassoPolygon:null, mapListFilter:null };
  vm.createContext(context);
  vm.runInContext(section("function dynamicAudienceIssue(", "async function saveDynamicList("), context);
  assert.match(context.dynamicAudienceIssue(), /Choose a state or sales territory/);
  context.scope = "GA";
  assert.equal(context.dynamicAudienceIssue(), "");
  context.lassoPolygon = [[33, -84]];
  assert.match(context.dynamicAudienceIssue(), /Clear the lasso or list filter/);
  context.lassoPolygon = null;
  context.mapListFilter = { id:"list-1" };
  assert.match(context.dynamicAudienceIssue(), /Clear the lasso or list filter/);
});

test("desktop summary offers one map export and the unified Add to List entry", () => {
  const html = fs.readFileSync(path.join(__dirname, "../../webapp/index.html"), "utf8");
  const summary = html.split('id="statsBar"')[1].split('id="mapLists"')[0];
  assert.match(summary, /id="exportAdvisors"[^>]*[\s\S]*?href="#icon-action-csv"/);
  assert.match(summary, /id="addMapToList"[^>]*[\s\S]*?href="#icon-action-add"/);
  assert.ok(summary.indexOf('id="kpiAdvisors"') < summary.indexOf('id="addMapToList"'));
  assert.ok(summary.indexOf('id="addMapToList"') < summary.indexOf('id="exportAdvisors"'));
  assert.match(summary, /Relevant AUM[\s\S]*?id="estimatedAum"[\s\S]*?id="eicAcv"[\s\S]*?id="eicLcv"[\s\S]*?id="eicMf"[\s\S]*?id="eicMidcap"/);
  assert.doesNotMatch(summary, /id="kpiOffices"|id="kpiFirms"/);
  assert.equal((html.match(/id="addMapToList"/g) || []).length, 1);
});

test("Relevant AUM combines the unrounded product estimates and preserves their detail", () => {
  const elements = { estimatedAum:{}, estimatedAumHelp:{} };
  const context = {
    document:{ getElementById:id => elements[id] },
    fmtMoney:value => "$" + Math.round(value / 1e6) + "M",
  };
  vm.createContext(context);
  vm.runInContext(section("function renderEstimatedAum(", "function refreshPanel("), context);
  context.renderEstimatedAum({ aum:90000000, equities:5200000, funds:2400000, placements:false,
    aumCoverage:7, equityCoverage:5, fundCoverage:4, firms:8 });
  assert.equal(elements.estimatedAum.textContent, "$8M");
  assert.match(elements.estimatedAumHelp.textContent, /\$5M in equities and \$2M in funds or ETFs/);
  assert.match(elements.estimatedAumHelp.textContent, /regulatory AUM allocated to this view is \$90M/);
  assert.match(elements.estimatedAumHelp.textContent, /Funds\/ETFs 4 of 8 firms/);
});
