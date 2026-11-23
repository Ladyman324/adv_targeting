"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const store = require("../shared/store");
const queue = require("../queue");
const settings = require("../settings");
const { delegates } = require("../shared/list-delegation");

const owner = { id:"owner", name:"owner@eicatlanta.com" };
const assistant = { id:"assistant", name:"assistant@eicatlanta.com" };
async function invoke(handler, who, method, query, body) {
  const context = { log:{ error() {}, info() {} } };
  const principal = Buffer.from(JSON.stringify({ userId:who.id, userDetails:who.name })).toString("base64");
  await handler(context, { method, query, body, headers:{ "x-ms-client-principal":principal } });
  return { status:context.res.status, body:JSON.parse(context.res.body) };
}

test("only the signed-in owner's validated EIC list grants can be saved", async (t) => {
  assert.deepEqual(delegates("A@eicatlanta.com; a@eicatlanta.com, B@eicatlanta.com", owner),
    ["a@eicatlanta.com", "b@eicatlanta.com"]);
  for (const email of ["outside@example.test", owner.name])
    assert.throws(() => delegates(email, owner), { statusCode:400 });
  t.mock.method(store, "saveListGrant", async (who, value) => {
    assert.equal(who.id, owner.id);
    return delegates(value, who).join(", ");
  });
  const response = await invoke(settings, owner, "PUT", {}, { listDelegates:assistant.name });
  assert.deepEqual(response, { status:200, body:{ listDelegates:assistant.name } });
});

test("every delegated queue operation uses the owner partition and rechecks revocation", async (t) => {
  let grant = { id:owner.id, email:owner.name, delegates:[assistant.name] };
  t.mock.method(store, "getListGrant", async id => id === owner.id ? grant : null);
  t.mock.method(store, "getQueue", async (who, id) => ({ owner:who.id, id }));
  t.mock.method(store, "listQueues", async () => [{ id:"prospects" }, { id:"role-scheduler" }]);
  t.mock.method(store, "putQueue", async (who, data) => ({ owner:who.id, createOnly:data.createOnly }));
  t.mock.method(store, "mutateQueueMember", async (who, id) => ({ owner:who.id, id }));
  t.mock.method(store, "deleteQueue", async (who, id) => ({ owner:who.id, id }));

  const query = { owner:owner.id, id:"prospects" };
  assert.equal((await invoke(queue, assistant, "GET", query)).body.owner, owner.id);
  assert.deepEqual((await invoke(queue, assistant, "GET", { owner:owner.id })).body.lists, [{ id:"prospects" }]);
  assert.equal((await invoke(queue, assistant, "PUT", query, { items:[] })).body.createOnly, true);
  assert.equal((await invoke(queue, assistant, "PUT", query, { items:[], etag:'"previous"' })).body.createOnly, false);
  assert.equal((await invoke(queue, assistant, "PATCH", query, { operation:"remove", crd:"123" })).body.owner, owner.id);
  assert.equal((await invoke(queue, assistant, "DELETE", query)).body.owner, owner.id);
  assert.equal((await invoke(queue, assistant, "DELETE", { ...query, id:"role-scheduler" })).status, 400);
  assert.equal((await invoke(queue, { ...assistant, name:"other@eicatlanta.com" }, "GET", query)).status, 403);
  grant = { ...grant, delegates:[] };
  for (const method of ["GET", "PUT", "PATCH", "DELETE"])
    assert.equal((await invoke(queue, assistant, method, query)).status, 403);
  assert.equal((await invoke(queue, assistant, "GET", { id:"prospects" })).body.owner, assistant.id);
});
