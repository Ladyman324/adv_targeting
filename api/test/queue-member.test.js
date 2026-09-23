"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const Module = require("module");
const { FakeTableService } = require("./helpers/fake-table");

function loadStore() {
  const service = new FakeTableService();
  const storePath = require.resolve("../shared/store.js");
  delete require.cache[storePath];
  const realLoad = Module._load;
  Module._load = function (request, parent) {
    if (parent && parent.filename === storePath && request === "@azure/data-tables") {
      return {
        TableClient: { fromConnectionString: (_conn, name) => service.table(name) },
        odata: (strings, ...values) => strings.reduce(
          (out, part, i) => out + part + (i < values.length ? `'${values[i]}'` : ""), ""),
      };
    }
    return realLoad.apply(this, arguments);
  };
  process.env.AZURE_STORAGE_CONNECTION_STRING = "UseDevelopmentStorage=true";
  try { return { store: require(storePath), service }; }
  finally { Module._load = realLoad; }
}

const WHO = { id: "rep-one", name: "rep@eicatlanta.com" };
const person = (crd, name) => ({ crd, name, firm: "Firm", phone: "4045550100",
  phoneKind: "direct", city: "Atlanta", state: "GA", unconfirmed: false,
  identityApproved: true });

async function seeded() {
  const env = loadStore();
  await env.store.putQueue(WHO, { id: "open", name: "Open", items: [person("1", "One")] });
  await env.store.putQueue(WHO, { id: "target", name: "Target",
    items: [person("2", "Two"), person("3", "Three")], cursor: 1 });
  return env;
}

test("atomic add targets an arbitrary list and is idempotent", async () => {
  const { store } = await seeded();
  const added = await store.mutateQueueMember(WHO, "target", "add", person("4", "Four"));
  assert.equal(added.added, true);
  assert.deepEqual(added.items.map((x) => x.crd), ["2", "3", "4"]);
  const again = await store.mutateQueueMember(WHO, "target", "add", person("4", "Four"));
  assert.equal(again.added, false);
  assert.equal(again.items.length, 3);
  assert.deepEqual((await store.getQueue(WHO, "open")).items.map((x) => x.crd), ["1"]);
});

test("atomic remove preserves the current-person cursor anchor", async () => {
  const { store } = await seeded();
  const result = await store.mutateQueueMember(WHO, "target", "remove", "2");
  assert.deepEqual(result.items.map((x) => x.crd), ["3"]);
  assert.equal(result.cursor, 0, "the cursor follows advisor 3 after a row above it is removed");
});

test("atomic add enforces firm DNC and requires confirmed identity proof", async () => {
  const { store } = await seeded();
  await store.addDnc(WHO, "9", "asked not to call", "Nine");
  await assert.rejects(() => store.mutateQueueMember(WHO, "target", "add", person("9", "Nine")),
    (e) => e.statusCode === 409 && /do-not-call/.test(e.message));
  await assert.rejects(() => store.mutateQueueMember(WHO, "target", "add",
    { ...person("8", "Eight"), unconfirmed: true }),
    (e) => e.statusCode === 409 && /confirmed contact/.test(e.message));
  await assert.rejects(() => store.mutateQueueMember(WHO, "target", "add",
    { ...person("7", "Seven"), identityApproved: false }),
    (e) => e.statusCode === 409 && /confirmed contact/.test(e.message));
});

test("atomic membership is isolated by signed-in user partition", async () => {
  const { store } = await seeded();
  await assert.rejects(() => store.mutateQueueMember(
    { id: "other", name: "other@eicatlanta.com" }, "target", "add", person("7", "Seven")),
    (e) => e.statusCode === 404);
});

test("five-hundred-person lists round-trip in bounded properties without truncation", async () => {
  const { store, service } = loadStore();
  const people = Array.from({ length:500 }, (_, i) => ({
    ...person(String(1000000 + i), "Advisor " + i),
    email:"advisor" + i + "@example.com",
    contactRouteVersion:"20260831T110925Z-3e65e09d6f4a9d0a",
  }));
  const written = await store.putQueue(WHO, { id:"large", name:"Large", items:people });
  assert.equal(written.count, 500);
  assert.equal(written.dropped, 0);
  const row = await service.table("DialQueue").getEntity(WHO.id, "large");
  assert.ok(row.itemChunks > 1);
  for (let i = 0; i < row.itemChunks; i++)
    assert.ok(row["items" + i].length * 2 <= 60 * 1024);
  assert.ok(Array.from({ length:row.itemChunks }, (_, i) =>
    row["items" + i].length * 2).reduce((a, b) => a + b, 0) <= 850 * 1024);
  assert.equal((await store.getQueue(WHO, "large")).items.length, 500);
  assert.equal((await store.listQueues(WHO)).find(x => x.id === "large").count, 500);
  await store.mutateQueueMember(WHO, "large", "remove", people[300].crd);
  assert.equal((await store.getQueue(WHO, "large")).items.length, 499);
  await assert.rejects(() => store.putQueue(WHO, {
    id:"large", items:[...people, person("9999999", "Overflow")],
  }), error => error.statusCode === 409 && /500/.test(error.message));
});

test("a realistic 228-person territory list never exceeds Azure's UTF-16 property cap", async () => {
  const { store, service } = loadStore();
  const people = Array.from({ length:228 }, (_, i) => ({
    ...person(String(2000000 + i), "Priority Advisor " + i),
    firm:"Edward Jones",
    email:"priority.advisor." + i + "@edwardjones.com",
    city:"Saint Louis",
    phone:"314-555-" + String(i).padStart(4, "0"),
    contactSource:"firm_roster",
    contactRouteVersion:"20260831T110925Z-3e65e09d6f4a9d0a",
  }));
  await store.putQueue(WHO, { id:"priority", name:"Priority", items:people });
  const row = await service.table("DialQueue").getEntity(WHO.id, "priority");
  assert.ok(row.itemChunks > 1);
  for (let i = 0; i < row.itemChunks; i++)
    assert.ok(row["items" + i].length * 2 <= 60 * 1024,
      "Azure Table's 64 KiB property cap is measured in UTF-16 bytes");
  assert.equal((await store.getQueue(WHO, "priority")).items.length, 228);
});

test("legacy single-property lists remain readable and migrate on write", async () => {
  const { store, service } = loadStore();
  const legacy = person("42", "Legacy");
  await service.table("DialQueue").createEntity({
    partitionKey:WHO.id, rowKey:"legacy", name:"Legacy",
    items:JSON.stringify([legacy]), cursor:0, cycle:1,
  });
  assert.equal((await store.getQueue(WHO, "legacy")).items[0].crd, "42");
  await store.mutateQueueMember(WHO, "legacy", "add", person("43", "New"));
  const row = await service.table("DialQueue").getEntity(WHO.id, "legacy");
  assert.equal(row.itemCount, 2);
  assert.ok(row.itemChunks >= 1);
  assert.equal((await store.getQueue(WHO, "legacy")).items.length, 2);
});

test("queue HTTP PATCH routes one member mutation and serializes the result", async () => {
  const queuePath = require.resolve("../queue/index.js");
  delete require.cache[queuePath];
  const realLoad = Module._load;
  const calls = [];
  const fakeStore = {
    identity: () => WHO,
    mutateQueueMember: async (...args) => {
      calls.push(args);
      return { id: "target", name: "Target", count: 3, items: [person("4", "Four")],
        cursor: 0, added: true };
    },
    ok: (context, body, status = 200) => {
      context.res = { status, body };
      return context.res;
    },
    fail: (context, err) => {
      context.res = { status: err.statusCode || 500, body: { error: err.message } };
      return context.res;
    },
    MAX_QUEUE: 500,
  };
  Module._load = function (request, parent) {
    if (parent && parent.filename === queuePath && request === "../shared/store") return fakeStore;
    return realLoad.apply(this, arguments);
  };
  let handler;
  try { handler = require(queuePath); }
  finally { Module._load = realLoad; }
  const context = {};
  await handler(context, { method: "PATCH", query: {}, body: {
    id: "target", operation: "add", item: person("4", "Four"),
  } });
  assert.equal(context.res.status, 200);
  assert.equal(context.res.body.added, true);
  assert.equal(context.res.body.max, 500);
  assert.deepEqual(calls[0], [WHO, "target", "add", person("4", "Four")]);
});
