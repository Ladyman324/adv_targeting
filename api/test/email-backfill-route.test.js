"use strict";

/* The backfill and status routes.
 *
 * Driven through the HTTP entry point rather than a service function, because
 * the thing that went wrong the first time was the ROUTE's idea of who it acts
 * on: it used the caller and nobody else, which is unusable when an
 * administrator cannot sign in as a rep -- and correctly cannot.
 */

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("path");

function load({ connections = [], state = {} } = {}) {
  const saved = {};
  const modules = {
    "../shared/store": {
      identity: (req) => req.__who,
    },
    "../shared/email-store": {
      listConnections: async () => connections,
      getSweepState: async (userId) => state[userId] || null,
      putSweepState: async (userId, sweep, value) => { saved[userId] = value; },
    },
  };
  const routePath = require.resolve("../email/index.js");
  delete require.cache[routePath];
  const Module = require("module");
  const realResolve = Module._resolveFilename;
  const stubs = new Map();
  for (const [name, exports] of Object.entries(modules)) {
    const resolved = require.resolve(path.join(__dirname, "..", "email", name));
    stubs.set(resolved, exports);
  }
  const realLoad = Module._load;
  Module._load = function (request, parent, isMain) {
    if (parent && parent.filename === routePath) {
      try {
        const resolved = realResolve.call(Module, request, parent, isMain);
        if (stubs.has(resolved)) return stubs.get(resolved);
      } catch { /* fall through */ }
    }
    return realLoad.apply(this, arguments);
  };
  const route = require(routePath);
  Module._load = realLoad;
  delete require.cache[routePath];
  return { route, saved };
}

const context = () => ({ log: Object.assign(() => {}, { error: () => {}, warn: () => {} }) });
const REP = { id: "user-bo", name: "Bo", roles: [] };
const ADMIN = { id: "user-kate", name: "Kate", roles: ["EmailAdministrator"] };
const CONNECTED = [
  { userId: "user-bo", mailbox: "bo@eicatlanta.com", needsReconnect: false },
  { userId: "user-kate", mailbox: "kate@eicatlanta.com", needsReconnect: false },
];

async function post(route, who, body) {
  const ctx = context();
  await route(ctx, { method: "POST", __who: who, body, query: {} });
  return { status: ctx.res.status, body: JSON.parse(ctx.res.body) };
}
async function get(route, who, query) {
  const ctx = context();
  await route(ctx, { method: "GET", __who: who, query, body: {} });
  return { status: ctx.res.status, body: JSON.parse(ctx.res.body) };
}

test("a rep can backfill their own mailbox", async () => {
  const { route, saved } = load({ connections: CONNECTED });
  const r = await post(route, REP, { op: "backfill", days: 365 });
  assert.equal(r.status, 200);
  assert.equal(r.body.userId, "user-bo");
  assert.ok(saved["user-bo"].watermarkUtc < saved["user-bo"].backfillUntilUtc);
});

test("an ADMIN can backfill for another rep, which is the whole point", async () => {
  // The first version acted only on the caller -- unusable, because an admin
  // cannot sign in as a rep and should not be able to.
  const { route, saved } = load({ connections: CONNECTED });
  const r = await post(route, ADMIN, { op: "backfill", userId: "user-bo", days: 365 });
  assert.equal(r.status, 200);
  assert.equal(r.body.mailbox, "bo@eicatlanta.com");
  assert.ok(saved["user-bo"], "the TARGET's watermark moves, not the admin's");
  assert.ok(!saved["user-kate"]);
});

test("a rep cannot backfill somebody else", async () => {
  const { route, saved } = load({ connections: CONNECTED });
  const r = await post(route, REP, { op: "backfill", userId: "user-kate", days: 365 });
  assert.equal(r.status, 403);
  assert.deepEqual(saved, {});
});

test("a mailbox that is not connected is refused, not silently queued", async () => {
  const { route, saved } = load({ connections: CONNECTED });
  const r = await post(route, ADMIN, { op: "backfill", userId: "user-nobody" });
  assert.equal(r.status, 404);
  assert.deepEqual(saved, {}, "a watermark for a mailbox we cannot read is a lie");
});

test("a rep who must reconnect is refused with the reason", async () => {
  const { route } = load({ connections: [
    { userId: "user-bo", mailbox: "bo@eicatlanta.com", needsReconnect: true }] });
  const r = await post(route, ADMIN, { op: "backfill", userId: "user-bo" });
  assert.equal(r.status, 409);
  assert.equal(r.body.code, "graph_reconnect_required");
});

test("the window is clamped rather than trusted", async () => {
  const { route, saved } = load({ connections: CONNECTED });
  await post(route, REP, { op: "backfill", days: 99999 });
  const span = new Date(saved["user-bo"].backfillUntilUtc) - new Date(saved["user-bo"].watermarkUtc);
  assert.ok(span / 86400000 <= 3650 + 1, "ten years is the ceiling");
});

/* ---- status -------------------------------------------------------------- */

test("status reports how far behind each rep is, which is the silent failure", async () => {
  const behind = new Date(Date.now() - 5 * 3600000).toISOString();
  const { route } = load({ connections: CONNECTED,
    state: { "user-bo": { watermarkUtc: behind, lastOkUtc: behind } } });
  const r = await get(route, ADMIN, { op: "sweep_status" });
  const bo = r.body.reps.find((x) => x.userId === "user-bo");
  assert.equal(bo.behindHours, 5, "a watermark that stops moving is the thing to watch");
});

test("status shows a backfill's remaining days", async () => {
  const until = new Date().toISOString();
  const watermark = new Date(Date.now() - 200 * 86400000).toISOString();
  const { route } = load({ connections: CONNECTED,
    state: { "user-bo": { watermarkUtc: watermark, backfillUntilUtc: until } } });
  const r = await get(route, ADMIN, { op: "sweep_status" });
  const bo = r.body.reps.find((x) => x.userId === "user-bo");
  assert.equal(bo.backfill.daysRemaining, 200);
});

test("a rep sees only themselves in status", async () => {
  const { route } = load({ connections: CONNECTED });
  const r = await get(route, REP, { op: "sweep_status" });
  assert.equal(r.body.count, 1);
  assert.equal(r.body.reps[0].userId, "user-bo");
});
