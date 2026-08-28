"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = process.env.REPOSITORY_TEST_ROOT
  ? path.resolve(process.env.REPOSITORY_TEST_ROOT)
  : path.resolve(__dirname, "..", "..");
const source = (name) => fs.readFileSync(path.join(ROOT, "webapp", name), "utf8");

function loadDial(){
  const window = {};
  const context = vm.createContext({
    window, localStorage: { getItem(){ return null; }, setItem(){}, removeItem(){} },
    fetch: async () => { throw new Error("unexpected fetch"); },
    setInterval, clearInterval, setTimeout, clearTimeout,
  });
  vm.runInContext(source("dial.js"), context);
  return window.Dial;
}

test("saved telephone routes fail closed and reconcile only to current approved data", async () => {
  const Dial = loadDial();
  Dial.setContactRouteVersion("build-new");

  const legacy = { crd: "101", name: "Legacy Person", phone: "+12025550101" };
  const stale = { crd: "102", name: "Stale Person", phone: "+12025550102",
    identityApproved: true, contactRouteVersion: "build-old" };
  Dial.state.items = [legacy, stale];

  assert.equal(Dial.telHref(legacy.crd, legacy.phone, legacy), "");
  assert.equal(Dial.telHref(stale.crd, stale.phone, stale), "");
  Dial.state.auto.on = true;
  Dial.armAuto(legacy, () => assert.fail("legacy route auto-dialled"));
  assert.equal(Dial.state.pending, null);

  await Dial.reconcileRoute("102", {
    crd: "102", name: "Current Person", phone: "+12025550999",
    phoneKind: "direct", identityApproved: true,
    contactRouteVersion: "build-new", unconfirmed: false,
  }, { save: false });
  const current = Dial.state.items[1];
  assert.equal(current.phone, "+12025550999");
  assert.equal(Dial.telHref("102", "+12025550102", current), "");
  assert.equal(Dial.telHref("102", current.phone, current), "tel:+12025550999");

  await Dial.reconcileRoute("102", {
    ...current, unconfirmed: true, identityApproved: false,
  }, { save: false });
  assert.equal(Dial.telHref("102", Dial.state.items[1].phone, Dial.state.items[1]), "");

  await Dial.reconcileRoute("101", null, { save: false });
  assert.equal(Dial.state.items[0].routeIssue, "missing");
  assert.equal(Dial.routeStatus(Dial.state.items[0]).reason, "identity");
  assert.equal(Dial.telHref("101", legacy.phone, Dial.state.items[0]), "");
});

test("a saved list is re-decided when server email policy changes", () => {
  const desk = source("app.js");
  const dial = source("dial.js");
  assert.match(dial, /EMAIL_POLICY_VERSION/);
  assert.match(dial, /blockedHighSources/);
  assert.match(dial, /const emailTierKey = \(\) => EMAIL_POLICY_VERSION/);
  assert.equal((dial.match(/emailTierKey: emailTierKey\(\)/g) || []).length, 2);
  assert.match(desk, /item\.emailTierKey !== Dial\.emailTierKey\(\)/);
});

test("an item without email proof cannot email, whatever its tier", async () => {
  const Dial = loadDial();
  Dial.setContactRouteVersion("build-new");

  const confirmed = { crd: "201", phone: "+12025550201", email: "confirmed@example.com",
    unconfirmed: false, identityApproved: true, emailConfirmed: true,
    emailEligibilityKnown: true,
    contactRouteVersion: "build-new" };
  const high = { ...confirmed, crd: "202", email: "research@example.com",
    emailConfirmed: false };
  const review = { ...confirmed, crd: "203", unconfirmed: true,
    identityApproved: false, emailConfirmed: false };
  const legacy = { crd: "204", phone: "+12025550204", email: "legacy@example.com" };
  const stale = { ...confirmed, crd: "205", contactRouteVersion: "build-old" };

  assert.equal(Dial.emailRouteStatus(confirmed).ok, true);
  assert.equal(Dial.emailRouteStatus(high).ok, false);
  assert.equal(Dial.emailRouteStatus(review).ok, false);
  assert.equal(Dial.emailRouteStatus(legacy).ok, false);
  assert.equal(Dial.emailRouteStatus(stale).ok, false);
  assert.equal(Dial.emailRouteStatus(confirmed, "changed@example.com").reason, "email-changed");
  assert.equal(Dial.routeStatus(high).ok, true);
  assert.equal(Dial.telHref(high.crd, high.phone, high), "tel:+12025550201");

  Dial.state.items = [legacy];
  await Dial.reconcileRoute(legacy.crd, confirmed, { save: false });
  assert.equal(Dial.emailRouteStatus(Dial.state.items[0]).ok, false,
    "a mismatched CRD cannot inherit another person's confirmed address");
  await Dial.reconcileRoute(legacy.crd, { ...confirmed, crd: legacy.crd }, { save: false });
  assert.equal(Dial.emailRouteStatus(Dial.state.items[0]).ok, true);
  assert.equal(Dial.state.items[0].emailEligibilityKnown, true);
});

test("raw mailto remains clickable while controlled actions use policy proof", () => {
  const desk = source("app.js");
  const field = source("field.js");
  assert.match(desk, /function mailtoLink[\s\S]{0,900}href="mailto:/);
  assert.doesNotMatch(desk, /function mailtoLink[\s\S]{0,900}contact-mailto blocked/);
  assert.match(desk, /app suppression controls do not apply/);
  assert.match(field, /class="sheet-mailto" href="mailto:/);
  assert.match(field, /app suppression controls do not apply/);
  assert.match(field, /emailConfirmed:\s*Dial\.tierCanEmail\(r\[COL\.tier\],[\s\S]{0,80}COL\.source/);
  assert.match(field, /emailEligibilityKnown:\s*true/);
});

test("review tier is blocked from controlled actions but not raw mailto", () => {
  const desk = source("app.js");
  const field = source("field.js");
  const dial = source("dial.js");
  assert.match(desk, /return !snap\.emailConfirmed \? null/);
  assert.match(desk, /filter\(\(it\) => Dial\.emailRouteStatus\(it\)\.ok\)/);
  assert.match(field, /const unconfirmed = r\[COL\.tier\] === "review"/);
  assert.match(field, /const mail = r\[COL\.email\] && emailConfirmed/);
  assert.match(field, /const queueBtn = dnc \|\| unconfirmed/);
  assert.match(field, /if \(!base \|\| base\.emailConfirmed !== true\) return null/);
  assert.match(dial, /if \(it\.unconfirmed\) \{ unconfirmed\+\+; continue; \}/);
});

test("the email whitelist cannot erase and then use an unconfirmed identity", () => {
  const email = source("email.js");
  assert.match(email, /filter\(\(r\) => r && !r\.unconfirmed\)/);
  assert.match(email, /cannot use the controlled composer until the CRD link is resolved/);
  assert.match(email, /if \(!recipients\.length\) return/);
});

test("teammate hints carry CRDs, tier, and source for server authorization", () => {
  const desk = source("app.js");
  const field = source("field.js");
  const fieldTiles = fs.readFileSync(path.join(ROOT, "src", "build_field_tiles.py"), "utf8");
  assert.match(desk, /out\.push\(\{ crd: String\(id\), name:/);
  assert.match(field, /out\.push\(\{ crd: mateCrd, name:/);
  assert.match(fieldTiles, /get\("t", ""\)/);
  assert.match(fieldTiles, /get\("src", ""\)/);
  assert.match(field, /Dial\.tierCanEmail\(m\[4\], m\[5\] \|\| ""\)/);
});

test("controlled actions log only eligible routes and disclose identity evidence", () => {
  const field = source("field.js");
  const email = source("email.js");
  assert.match(field, /const controlledEmail = r\[COL\.email\][\s\S]{0,120}Dial\.tierCanEmail/);
  assert.match(field, /controlledEmail[\s\S]{0,350}data-mail=[\s\S]{0,350}href="mailto:/);
  assert.match(field, /app suppression controls do not apply/);
  assert.match(email, /function identityLabel\(message\)/);
  assert.match(email, /ACT-confirmed CRD/);
  assert.match(email, /High-confidence roster match/);
  assert.match(email, /identitySummary\(detail\.messages\)/);
  assert.match(email, /identityLabel\(m\)/);
});

test("saved queues preserve the bounded current-route proof fields", () => {
  const store = fs.readFileSync(
    path.join(ROOT, "api", "shared", "store.js"), "utf8");
  assert.match(store, /identityApproved:\s*!!\(it && it\.identityApproved\)/);
  assert.match(store, /emailConfirmed:\s*!!\(it && it\.emailConfirmed\)/);
  assert.match(store, /emailEligibilityKnown:\s*!!\(it && it\.emailEligibilityKnown\)/);
  assert.match(store,
    /contactRouteVersion:\s*clean\(it && it\.contactRouteVersion, 80\)/);
  assert.match(store, /routeIssue:\s*clean\(it && it\.routeIssue, 24\)/);
});
