"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { FakeTableService } = require("./helpers/fake-table");

function loadStore() {
  const service = new FakeTableService();
  const path = require.resolve("../shared/email-direct-store");
  delete require.cache[path];
  process.env.AZURE_STORAGE_CONNECTION_STRING =
    "DefaultEndpointsProtocol=https;AccountName=test;AccountKey=dGVzdA==;EndpointSuffix=core.windows.net";
  const store = require(path);
  const sdk = require.resolve("@azure/data-tables");
  const real = require.cache[sdk].exports.TableClient.fromConnectionString;
  require.cache[sdk].exports.TableClient.fromConnectionString = (_connection, name) => service.table(name);
  return { store, service, restore() {
    require.cache[sdk].exports.TableClient.fromConnectionString = real;
    delete require.cache[path];
  } };
}

const BASE = { operationId: "11111111-1111-4111-8111-111111111111",
  kind: "reply", intentHash: "a".repeat(64), advisorCrd: "123", attachmentCount: 1 };

test("operation ownership and its durable marker are created atomically", async () => {
  const { store, service, restore } = loadStore();
  try {
    const result = await store.createOperation("u1", BASE, { nowMs: 1000 });
    assert.equal(result.created, true);
    assert.equal(result.operation.state, "preparing");
    const rows = service.all(store.TABLE_NAME);
    assert.deepEqual(rows.map((row) => row.rowKey).sort(),
      [store.opKey(BASE.operationId), store.workKey(BASE.operationId)]);
    assert.ok(rows.every((row) => !Object.hasOwn(row, "body")
      && !Object.hasOwn(row, "recipients") && !Object.hasOwn(row, "attachments")));
  } finally { restore(); }
});

test("concurrent same-intent creates have exactly one winner", async () => {
  const { store, service, restore } = loadStore();
  try {
    const results = await Promise.all([
      store.createOperation("u1", BASE), store.createOperation("u1", BASE),
    ]);
    assert.equal(results.filter((result) => result.created).length, 1);
    assert.equal(service.all(store.TABLE_NAME).filter((row) => row.rowKey.startsWith("op|")).length, 1);
  } finally { restore(); }
});

test("an operation id cannot be replayed with different intent", async () => {
  const { store, restore } = loadStore();
  try {
    await store.createOperation("u1", BASE);
    await assert.rejects(() => store.createOperation("u1", { ...BASE, intentHash: "b".repeat(64) }),
      (err) => err.statusCode === 409 && err.code === "idempotency_conflict");
  } finally { restore(); }
});

test("ETag leases give one worker ownership and stale writers lose", async () => {
  const { store, restore } = loadStore();
  try {
    const created = (await store.createOperation("u1", BASE, { nowMs: 0, leaseSeconds: 1 })).operation;
    const winner = await store.claimOperation("u1", BASE.operationId, ["preparing"],
      { nowMs: 2000, nextState: "preparing", phase: "prepare" });
    assert.ok(winner);
    assert.equal(await store.claimOperation("u1", BASE.operationId, ["preparing"],
      { nowMs: 2001 }), null);
    await assert.rejects(() => store.patchOperation("u1", BASE.operationId,
      { state: "prepared" }, created.etag), (err) => err.statusCode === 412);
  } finally { restore(); }
});

test("scheduling updates operation and marker together; completion removes only the marker", async () => {
  const { store, service, restore } = loadStore();
  try {
    let operation = (await store.createOperation("u1", BASE)).operation;
    operation = await store.scheduleOperation("u1", BASE.operationId,
      { state: "prepared", graphDraftId: "draft-1" }, "2026-08-25T12:00:00Z", operation.etag);
    assert.equal(operation.state, "prepared");
    const marker = service.all(store.TABLE_NAME).find((row) => row.rowKey.startsWith("q|"));
    assert.equal(marker.dueUtc, "2026-08-25T12:00:00Z");
    const done = await store.completeOperation("u1", BASE.operationId,
      { graphMessageId: "sent-1", canonicalSentDateTime: "2026-08-25T12:01:00Z" }, operation.etag);
    assert.equal(done.state, "complete");
    assert.equal(service.all(store.TABLE_NAME).some((row) => row.rowKey.startsWith("q|")), false);
    assert.equal(service.all(store.TABLE_NAME).some((row) => row.rowKey.startsWith("op|")), true);
  } finally { restore(); }
});

test("queue payloads carry identifiers only and accept host-compatible encodings", async () => {
  const queue = require("../shared/email-direct-queue");
  const sent = [];
  const client = { createIfNotExists: async () => {}, sendMessage: async (payload) => sent.push(payload) };
  await queue.enqueue("direct_send", "u1", BASE.operationId, 0, { client });
  const decoded = queue.parse(sent[0]);
  assert.deepEqual(decoded, { v: 1, kind: "direct_send", userId: "u1", operationId: BASE.operationId });
  assert.deepEqual(queue.parse(JSON.stringify(decoded)), decoded);
  assert.deepEqual(queue.parse(decoded), decoded);
  assert.equal(JSON.stringify(decoded).includes("body"), false);
});
