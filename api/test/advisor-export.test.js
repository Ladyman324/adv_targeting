"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const app = fs.readFileSync(path.resolve(__dirname, "../../webapp/app.js"), "utf8").replace(/\r\n/g, "\n");
const code = app.slice(app.indexOf("function csvCell("), app.indexOf("// ---- firm exclusion ----"));
function harness(extra = {}) {
  const downloads = [], notices = [], listeners = [];
  const env = { Blob, scope: "GA", scopeLabel: () => "Georgia", META: { source_date: "2026-11-05" }, DATA_VERSION: "test-build",
    document: { querySelectorAll: () => [], body: { appendChild() {} }, addEventListener: (...x) => listeners.push(x),
      createElement: () => ({ click() { downloads.push(this.download); }, remove() {} }) },
    URL: { createObjectURL: blob => { downloads.push(blob); return "blob:test"; }, revokeObjectURL() {} },
    setTimeout: fn => fn(), showNotice: text => notices.push(text), ALL: [],
    contactFor: () => null, advisorDisplayName: (_id, fallback) => fallback,
    ActAddresses: { current: () => null, isContactKey: () => false },
    ...extra,
  };
  vm.createContext(env); vm.runInContext(code, env);
  return { env, downloads, notices, listeners };
}

test("Excel CSV starts with its header, escapes cells, prevents formulas and retains provenance", async () => {
  const { env, downloads } = harness();
  env.csvDownload("contacts.csv", ["Advisor", "ZIP"], [["Jones, Dana", "01880"], [' =HYPERLINK("bad")', "30309"]]);
  assert.equal(downloads[1], "contacts.csv");
  const csv = await downloads[0].text();
  assert.ok(csv.startsWith("Advisor,ZIP,Export scope,"));
  assert.match(csv, /"Jones, Dana",01880,Georgia/);
  assert.match(csv, /"' =HYPERLINK\(""bad""\)"/);
  assert.match(csv, /2026-11-05,test-build/);
  assert.equal(env.csvCell("a\rb"), '"a\rb"');
  assert.equal(env.csvCell("+1"), "'+1");
  assert.equal(env.csvCell("\t@bad"), "'\t@bad");
});

test("list export retains all members, including contacts with no email, phone or current map pin", async () => {
  const { env, downloads } = harness({ contactFor: id => id === "1" ? { e: "current@example.com", t: "review", sal: "Dana" } : null });
  env.exportContactList("My rotation", [
    { crd: "1", name: "Dana", email: "saved@example.com" },
    { crd: "2", name: "No route", city: "Albany", state: "NY" },
  ]);
  const csv = await downloads[0].text();
  assert.equal(csv.trim().split("\r\n").length, 3);
  assert.match(csv, /current@example.com,saved@example.com/);
  assert.match(csv, /My rotation,No route,2/);
  assert.match(csv, /Contact list: My rotation,Complete membership; map filters and viewport do not apply/);
  assert.match(csv, /Complete list membership; export does not establish email eligibility/);
});

test("map export uses filters and viewport, deduplicates relationships and includes current contacts", async () => {
  const pin = (id, fc, lon, show) => ({ properties: { id, fc, n: `Advisor ${id}`, f: "Firm", _state: "GA", show }, geometry: { coordinates: [lon, 33] } });
  const { env, downloads } = harness({
    ALL: [pin("1", "10", -84, true), pin("1", "10", -84, true), pin("2", "10", -84, false), pin("3", "10", -90, true)],
    map: { getBounds: () => ({ contains: ([lat, lon]) => lon === -84 && lat === 33 }) },
    loadContacts: async () => ({}), passesFilters: p => p.show, advisorRow: () => null,
    ownerRolesFor: () => [], barronsFor: () => [], forbesFor: () => [], forbesTeamAssets: () => null,
    contactFor: () => ({ e: "a@example.com", sal: "A", t: "confirmed" }), STATE_TO_TERRITORY: {}, OWNERSHIP_BANDS: {}, FILTER_DAY_MS: 86400000,
  });
  await env.exportAdvisors();
  const csv = await downloads[0].text();
  assert.equal(csv.trim().split("\r\n").length, 2);
  assert.match(csv, /Advisor 1/); assert.doesNotMatch(csv, /Advisor 2|Advisor 3/);
  assert.match(csv, /a@example.com/);
});

test("a contact-loading failure produces no misleading map export", async () => {
  const { env, downloads, notices } = harness({ loadContacts: async () => null });
  await env.exportAdvisors();
  assert.equal(downloads.length, 0); assert.match(notices[0], /Retry the export/);
});


test("saved-list export reads the selected list without opening it, changing membership or preparing email", async () => {
  const app = fs.readFileSync(path.resolve(__dirname, "../../webapp/app.js"), "utf8").replace(/\r\n/g, "\n");
  const start = app.indexOf('document.addEventListener("click", async e => {\n  const b = e.target.closest("[data-lists]")');
  assert.ok(start > 0);
  const end = app.indexOf('\n});', start) + 4;
  let listener, exported;
  const calls = [], button = { dataset: { lists: "export", id: "rotation" } };
  const state = { listId: "currently-calling", cursor: 9, lists: [{ id: "rotation", name: "Rotation", count: 1 }] };
  const env = { document: { addEventListener: (_type, fn) => { listener = fn; } }, listsBack: {},
    currentWorkspaceLists: () => state.lists, listWorkspace: null,
    Dial: { state, readList: async (id, owner) => {
      calls.push({ id, owner });
      return { name: "Rotation", items: [{ crd: "1" }] };
    } }, loadContacts: async () => ({}),
    ActAddresses: { loadListContacts: async () => [] },
    exportContactList: (name, items) => { exported = { name, items }; },
    showNotice: assert.fail, encodeURIComponent,
  };
  vm.createContext(env); vm.runInContext(app.slice(start, end), env);
  await listener({ target: { closest: () => button } });
  assert.equal(exported.name, "Rotation"); assert.equal(exported.items.length, 1);
  assert.equal(state.listId, "currently-calling"); assert.equal(state.cursor, 9);
  assert.deepEqual(calls, [{ id: "rotation", owner: undefined }]);
  assert.equal(button.disabled, false);
});
