"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const Module = require("node:module");
const source = require.resolve("../shared/address-change-store");
function fixture() {
  let version = 0;
  const rows = new Map();
  const key = (pk, rk) => pk + "|" + rk;
  const fail = statusCode => Object.assign(new Error("fake table failure"), { statusCode });
  const table = {
    createTable: async () => {},
    createEntity: async e => { const k = key(e.partitionKey, e.rowKey); if (rows.has(k)) throw fail(409); rows.set(k, { ...e, etag: String(++version) }); },
    getEntity: async (pk, rk) => { const e = rows.get(key(pk, rk)); if (!e) throw fail(404); return { ...e }; },
    updateEntity: async (e, mode, options) => {
      const k = key(e.partitionKey, e.rowKey), before = rows.get(k);
      if (!before) throw fail(404);
      if (options && options.etag !== before.etag) throw fail(412);
      rows.set(k, { ...(mode === "Merge" ? before : {}), ...e, etag: String(++version) });
    },
    deleteEntity: async (pk, rk, options) => {
      const k = key(pk, rk), before = rows.get(k);
      if (!before) throw fail(404);
      if (options.etag !== before.etag) throw fail(412);
      rows.delete(k);
    },
  };
  const old = Module._load, previous = process.env.AZURE_STORAGE_CONNECTION_STRING;
  process.env.AZURE_STORAGE_CONNECTION_STRING = "test-only";
  Module._load = function(name, parent) {
    if (parent && parent.filename === source && name === "@azure/data-tables") return { TableClient: { fromConnectionString: () => table } };
    return old.apply(this, arguments);
  };
  delete require.cache[source];
  let journal;
  try { journal = require(source); } finally { Module._load = old; }
  return { journal, rows, table, restore() { delete require.cache[source]; if (previous === undefined) delete process.env.AZURE_STORAGE_CONNECTION_STRING; else process.env.AZURE_STORAGE_CONNECTION_STRING = previous; } };
}
test("journal records intent and readback outcome, and releases its contact lock", async () => {
  const f = fixture();
  try {
    await f.journal.lock("ACT-CHRIS", async () => {
      const id = await f.journal.begin({ contactId: "ACT-CHRIS", actorId: "trusted", action: "add_secondary" });
      await f.journal.finish(id, "verified");
    });
    assert.equal([...f.rows.values()].filter(r => r.rowKey === "lock").length, 0);
    assert.equal([...f.rows.values()][0].state, "verified");
    assert.equal(JSON.parse([...f.rows.values()][0].details).actorId, "trusted");
  } finally { f.restore(); }
});
test("a second worker cannot update the same ACT contact concurrently", async () => {
  const f = fixture();
  try {
    await f.journal.lock("act-chris", async () => {
      await assert.rejects(f.journal.lock("ACT-CHRIS", async () => assert.fail("must not run")), /in progress/);
    });
    assert.equal(f.rows.size, 0);
  } finally { f.restore(); }
});
test("an expired lease can be claimed with its ETag; failures still release it", async () => {
  const f = fixture();
  try {
    await f.table.createEntity({ partitionKey: "act-chris", rowKey: "lock", expires: "2000-01-01T00:00:00Z" });
    await assert.rejects(f.journal.lock("act-chris", async () => { throw new Error("operation failed"); }), /operation failed/);
    assert.equal(f.rows.size, 0);
  } finally { f.restore(); }
});
test("no recording connection prevents an address operation", async () => {
  const f = fixture();
  try {
    delete process.env.AZURE_STORAGE_CONNECTION_STRING;
    await assert.rejects(f.journal.lock("act-chris", async () => assert.fail("must not run")), /Nothing was changed/);
  } finally { f.restore(); }
});
