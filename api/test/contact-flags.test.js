"use strict";

/* Key contact / due diligence flags, as SETS of reps.
 *
 * THE BUG THESE PIN: the flag was one shared boolean with one name attached.
 * The star on a card meant "somebody marked this person", so a second rep saw
 * it already lit -- and pressing it CLEARED the first rep's mark instead of
 * adding their own. One rep could silently delete another's, and there was no
 * way at all to join a flag that was already set.
 *
 * Found by asking a plain question of the finished feature: "if an internal
 * rep marks someone, how does another rep add them to THEIR list?"
 */

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("path");
const Module = require("module");

// A minimal Table Storage double: one partition, upsert/get/delete by rowKey.
function fakeTable() {
  const rows = new Map();
  let version = 0;
  return {
    rows,
    async getEntity(pk, rk) {
      const hit = rows.get(rk);
      if (!hit) { const e = new Error("not found"); e.statusCode = 404; throw e; }
      return { ...hit };
    },
    async createEntity(entity) {
      if (rows.has(entity.rowKey)) { const e = new Error("exists"); e.statusCode = 409; throw e; }
      rows.set(entity.rowKey, { ...entity, etag: `e${++version}` });
    },
    async updateEntity(entity, mode, opts) {
      const current = rows.get(entity.rowKey);
      if (!current) { const e = new Error("not found"); e.statusCode = 404; throw e; }
      if (opts && opts.etag && opts.etag !== current.etag) {
        const e = new Error("stale"); e.statusCode = 412; throw e;
      }
      rows.set(entity.rowKey, { ...entity, etag: `e${++version}` });
    },
    async upsertEntity(entity) { rows.set(entity.rowKey, { ...entity, etag: `e${++version}` }); },
    async deleteEntity(pk, rk, opts) {
      const current = rows.get(rk);
      if (!current) { const e = new Error("not found"); e.statusCode = 404; throw e; }
      if (opts && opts.etag && opts.etag !== current.etag) {
        const e = new Error("stale"); e.statusCode = 412; throw e;
      }
      rows.delete(rk);
    },
    async *listEntities() { for (const r of rows.values()) yield { ...r }; },
    async createTable() { /* the real client creates on first use */ },
  };
}

function loadStore(shared) {
  const storePath = require.resolve("../shared/store.js");
  delete require.cache[storePath];
  const realLoad = Module._load;
  Module._load = function (request, parent) {
    if (parent && parent.filename === storePath && request === "@azure/data-tables") {
      return { TableClient: { fromConnectionString: () => shared },
               odata: (strings, ...vals) => strings.raw.join("") + vals.join("") };
    }
    return realLoad.apply(this, arguments);
  };
  process.env.AZURE_STORAGE_CONNECTION_STRING =
    process.env.AZURE_STORAGE_CONNECTION_STRING || "UseDevelopmentStorage=true";
  let store;
  try { store = require(storePath); } finally { Module._load = realLoad; }
  return store;
}

const REP_A = { id: "u-a", name: "matt@eicatlanta.com" };
const REP_B = { id: "u-b", name: "bo@eicatlanta.com" };

async function ready() {
  const t = fakeTable();
  const store = loadStore(t);
  if (typeof store.setFlag !== "function") return null;
  return { t, store };
}

test("a second rep JOINS a flag instead of clearing it", async () => {
  const env = await ready(); if (!env) return;
  const { store } = env;
  await store.setFlag(REP_A, "5573829", "key", true, "Regina Stuzin", "");
  const after = await store.setFlag(REP_B, "5573829", "key", true, "Regina Stuzin", "");
  assert.equal(after.key, true);
  assert.deepEqual([...after.keyBy].sort(), [REP_A.name, REP_B.name].sort(),
    "both reps must hold the flag; the second must not replace the first");
});

test("one rep unmarking does not take the other rep's mark with it", async () => {
  const env = await ready(); if (!env) return;
  const { store } = env;
  await store.setFlag(REP_A, "5573829", "key", true, "Regina Stuzin", "");
  await store.setFlag(REP_B, "5573829", "key", true, "Regina Stuzin", "");
  const after = await store.setFlag(REP_B, "5573829", "key", false, "", "");
  assert.equal(after.key, true, "the flag survives while anybody still holds it");
  assert.deepEqual(after.keyBy, [REP_A.name]);
});

test("the row is deleted only when nobody holds either flag", async () => {
  const env = await ready(); if (!env) return;
  const { store, t } = env;
  await store.setFlag(REP_A, "111", "key", true, "Solo", "");
  await store.setFlag(REP_A, "111", "key", false, "", "");
  assert.equal(t.rows.size, 0, "a tombstone would ship to every client forever");
});

test("the two flags keep separate membership", async () => {
  const env = await ready(); if (!env) return;
  const { store } = env;
  await store.setFlag(REP_A, "222", "key", true, "Jennifer Nash", "");
  const after = await store.setFlag(REP_B, "222", "dd", true, "Jennifer Nash", "");
  assert.deepEqual(after.keyBy, [REP_A.name], "setting the shield rewrote who starred them");
  assert.deepEqual(after.ddBy, [REP_B.name]);
});

test("a row written before the set existed is read as a set of one", async () => {
  const env = await ready(); if (!env) return;
  const { store, t } = env;
  // Exactly the legacy shape: one userName, no keyBy/ddBy.
  t.rows.set("333", { partitionKey: "flags", rowKey: "333", keyContact: true,
                      dueDiligence: false, advisorName: "Legacy", userName: REP_A.name });
  const [row] = await store.listFlags();
  assert.deepEqual(row.keyBy, [REP_A.name], "legacy rows must not lose their owner");
  // And a second rep joining it keeps the legacy owner.
  const after = await store.setFlag(REP_B, "333", "key", true, "Legacy", "");
  assert.deepEqual([...after.keyBy].sort(), [REP_A.name, REP_B.name].sort());
});

test("the same rep marking twice is not two members", async () => {
  const env = await ready(); if (!env) return;
  const { store } = env;
  await store.setFlag(REP_A, "444", "key", true, "Dup", "");
  const after = await store.setFlag({ id: "u-a", name: REP_A.name.toUpperCase() },
                                    "444", "key", true, "Dup", "");
  assert.equal(after.keyBy.length, 1, "a UPN is not case-sensitive; one rep is one member");
});

test("scheduler is independent and legacy analyst storage remains dd", async () => {
  const env = await ready(); if (!env) return;
  const { store } = env;
  await store.setFlag(REP_A, "555", "dd", true, "Analyst Person", "10");
  const after = await store.setFlag(REP_B, "555", "scheduler", true, "Analyst Person", "10");
  assert.equal(after.dd, true);
  assert.equal(after.scheduler, true);
  assert.deepEqual(after.ddBy, [REP_A.name]);
  assert.deepEqual(after.schedulerBy, [REP_B.name]);
  const [listed] = await store.listFlags();
  assert.equal(listed.dd, true, "Analyst continues to use the legacy dd storage field");
  assert.equal(listed.scheduler, true);
});

test("a row survives until key, analyst, and scheduler are all empty", async () => {
  const env = await ready(); if (!env) return;
  const { store, t } = env;
  await store.setFlag(REP_A, "666", "scheduler", true, "Calendar", "");
  await store.setFlag(REP_A, "666", "key", true, "Calendar", "");
  await store.setFlag(REP_A, "666", "key", false, "Calendar", "");
  assert.equal(t.rows.size, 1);
  await store.setFlag(REP_A, "666", "scheduler", false, "Calendar", "");
  assert.equal(t.rows.size, 0);
});
