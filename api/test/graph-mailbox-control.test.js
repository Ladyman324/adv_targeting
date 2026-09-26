"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const controlModule = require("../shared/graph-mailbox-control");
const graph = require("../shared/graph-mail");
const TOKEN = "x." + Buffer.from(JSON.stringify({ tid: "tenant", oid: "mailbox" })).toString("base64url") + ".x";

function memoryTable() {
  const rows = new Map();
  let version = 0;
  return {
    async getEntity(pk, rk) {
      const row = rows.get(pk + rk);
      if (!row) throw { statusCode: 404 };
      return { ...row };
    },
    async createEntity(entity) {
      const key = entity.partitionKey + entity.rowKey;
      if (rows.has(key)) throw { statusCode: 409 };
      rows.set(key, { ...entity, etag: String(++version) });
    },
    async updateEntity(entity, _mode, options) {
      const key = entity.partitionKey + entity.rowKey;
      const current = rows.get(key);
      if (!current || options.etag !== current.etag) throw { statusCode: 412 };
      rows.set(key, { ...current, ...entity, etag: String(++version) });
    },
  };
}

test("independent hosts share one mailbox lease; other mailboxes proceed", async () => {
  const table = memoryTable();
  const hosts = Array.from({ length: 25 }, () => controlModule.createControl(table));
  const results = await Promise.allSettled(hosts.map(host => host.acquire("same")));
  assert.equal(results.filter(x => x.status === "fulfilled").length, 1);
  assert.ok(results.filter(x => x.status === "rejected").every(x => x.reason.deferred));
  const other = await hosts[1].acquire("other");
  await hosts[1].release(other);
});

test("a waiting operation takes over after release and records the prior holder", async () => {
  const control = controlModule.createControl(memoryTable());
  const held = await control.acquire("mail", { operation: "attachment_upload" });
  const pending = control.acquire("mail", { operation: "message_read", waitMs: 1000 });
  await new Promise(resolve => setTimeout(resolve, 40));
  await control.release(held);
  const acquired = await pending;
  assert.equal(acquired.operation, "message_read");
  assert.equal(acquired.blockedBy, "attachment_upload");
  assert.ok(acquired.waitedMs >= 40);
  await control.release(acquired);
});

test("an occupied mailbox reports the blocking operation without leaking a mailbox identity", async () => {
  const control = controlModule.createControl(memoryTable());
  const held = await control.acquire("mail", { operation: "draft_create" });
  await assert.rejects(control.acquire("mail", { operation: "send", waitMs: 30 }), error =>
    error.code === "mailbox_busy" && error.blockedBy === "draft_create"
      && error.waitedMs >= 30 && error.leaseRemainingSeconds > 0);
  await control.release(held);
});

test("send spacing is at least ten seconds while reads and drafting remain immediate", async () => {
  let clock = 100000;
  const control = controlModule.createControl(memoryTable(), () => clock);
  const first = await control.acquire("mail", { sending: true, intervalSeconds: 10 });
  await control.release(first);
  const read = await control.acquire("mail");
  await control.release(read);
  await assert.rejects(control.acquire("mail", { sending: true }), e => e.code === "mailbox_send_spacing");
  clock += 10000;
  const second = await control.acquire("mail", { sending: true });
  await control.release(second);
});

test("shared cooldown never shortens and an expired owner cannot release a new lease", async () => {
  let clock = 100000;
  const table = memoryTable(), a = controlModule.createControl(table, () => clock),
    b = controlModule.createControl(table, () => clock);
  const old = await a.acquire("mail", { timeoutMs: 1000 });
  clock += 32000;
  const current = await b.acquire("mail");
  await a.release(old);
  await assert.rejects(a.acquire("mail"), e => e.code === "mailbox_busy");
  await b.cooldown("mail", 60);
  await a.cooldown("mail", 10);
  await b.release(current);
  clock += 11000;
  await assert.rejects(a.acquire("mail"), e => e.code === "mailbox_cooldown" && e.retryAfter === 49);
  clock += 49000;
  await a.acquire("mail");
});

function transportFixture(t) {
  const counters = { cooldown: [], released: 0, calls: 0 };
  t.mock.method(controlModule, "control", () => ({
    acquire: async () => ({ expiresAt: Date.now() + 150000, key: "mail" }),
    cooldown: async (_key, seconds) => counters.cooldown.push(seconds),
    release: async () => { counters.released++; },
  }));
  return counters;
}

test("a Graph GET timeout is retryable; a POST timeout remains ambiguous", async t => {
  const counts = transportFixture(t);
  t.mock.method(globalThis, "fetch", async () => { throw new TypeError("network reset"); });
  await assert.rejects(graph.getMessage(TOKEN, "draft"), e =>
    e.safeToRetry === true && e.ambiguous === false && e.method === "GET");
  await assert.rejects(graph.sendDraft(TOKEN, "draft"), e =>
    e.safeToRetry === false && e.ambiguous === true && e.method === "POST");
  assert.equal(counts.released, 2);
  assert.deepEqual(counts.cooldown, [30, 30]);
});

test("a definitive 429 carries Retry-After and never becomes an unknown send", async t => {
  const counts = transportFixture(t);
  t.mock.method(globalThis, "fetch", async () => new Response(
    JSON.stringify({ error: { code: "ApplicationThrottled", message: "Mailbox is busy" } }), {
      status: 429, headers: { "retry-after": "75", "request-id": "graph-request" } }));
  await assert.rejects(graph.sendDraft(TOKEN, "draft"), e =>
    e.statusCode === 429 && e.safeToRetry && !e.ambiguous && e.retryAfter === 75
    && e.requestId === "graph-request" && !!e.clientRequestId && e.operation === "/me/messages/{id}/send");
  assert.deepEqual(counts.cooldown, [75]);
});

test("failed reads with permanent HTTP status do not loop forever", async t => {
  transportFixture(t);
  t.mock.method(globalThis, "fetch", async () => new Response("{}", { status: 400 }));
  await assert.rejects(graph.getMessage(TOKEN, "draft"), e => e.statusCode === 400 && !e.safeToRetry);
});

test("mailbox busy or unavailable never dispatches a Graph request", async t => {
  let calls = 0;
  t.mock.method(globalThis, "fetch", async () => { calls++; });
  t.mock.method(controlModule, "control", () => ({
    acquire: async () => { throw new Error("Table unavailable"); },
  }));
  await assert.rejects(graph.sendDraft(TOKEN, "draft"), e => e.deferred && e.safeToRetry && !e.ambiguous);
  assert.equal(calls, 0);
});

test("follow-up guard reads the whole Outlook conversation and ignores automatic replies", async t => {
  transportFixture(t);
  let pages = 0;
  t.mock.method(globalThis, "fetch", async url => {
    const parsed = new URL(url);
    assert.match(parsed.searchParams.get("$filter") || "", /conversationId eq 'thread-1'/);
    pages++;
    return new Response(JSON.stringify(pages === 1 ? {
      value: [{ conversationId: "thread-1", from: { emailAddress: { address: "advisor@example.test" } },
        subject: "Automatic reply", internetMessageHeaders: [] }],
      "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/messages?$filter=conversationId%20eq%20%27thread-1%27&$skip=1",
    } : { value: [{ conversationId: "thread-1",
      from: { emailAddress: { address: "advisor@example.test" } },
      subject: "Re: Hello", internetMessageHeaders: [] }] }), { status: 200 });
  });
  assert.equal(await graph.hasHumanReplyInConversation(TOKEN, "thread-1", "advisor@example.test"), true);
  assert.equal(pages, 2);
});

test("follow-up guard fails closed on incomplete Outlook pagination", async t => {
  transportFixture(t);
  t.mock.method(globalThis, "fetch", async () => new Response(JSON.stringify({
    value: [], "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/messages?$skip=next",
  }), { status: 200 }));
  await assert.rejects(graph.hasHumanReplyInConversation(TOKEN, "thread-1", "advisor@example.test"),
    error => error.code === "follow_up_thread_incomplete");
});

test("follow-up guard permits only a complete thread with no human reply", async t => {
  transportFixture(t);
  t.mock.method(globalThis, "fetch", async () => new Response(JSON.stringify({ value: [
    { conversationId: "thread-1", from: { emailAddress: { address: "rep@eicatlanta.com" } },
      subject: "Hello", internetMessageHeaders: [] },
    { conversationId: "thread-1", from: { emailAddress: { address: "advisor@example.test" } },
      subject: "Automatic reply", internetMessageHeaders: [] },
  ] }), { status: 200 }));
  assert.equal(await graph.hasHumanReplyInConversation(TOKEN, "thread-1", "advisor@example.test"), false);
});

test("self-test's original inbox copy is not mistaken for its reply", async t => {
  transportFixture(t);
  t.mock.method(globalThis, "fetch", async () => new Response(JSON.stringify({ value: [
    { conversationId: "thread-1", internetMessageId: "<original@example.test>",
      from: { emailAddress: { address: "rep@eicatlanta.com" } }, subject: "Hello" },
    { conversationId: "thread-1", internetMessageId: "<reply@example.test>",
      from: { emailAddress: { address: "rep@eicatlanta.com" } }, subject: "Re: Hello" },
  ] }), { status: 200 }));
  assert.equal(await graph.hasHumanReplyInConversation(TOKEN, "thread-1", "rep@eicatlanta.com",
    { excludeInternetMessageId: "<original@example.test>" }), true);
});

test("follow-up guard refuses an unfiltered or malformed Graph response", async t => {
  transportFixture(t);
  t.mock.method(globalThis, "fetch", async () => new Response(JSON.stringify({ value: [
    { conversationId: "other-thread", from: { emailAddress: { address: "advisor@example.test" } } },
  ] }), { status: 200 }));
  await assert.rejects(graph.hasHumanReplyInConversation(TOKEN, "thread-1", "advisor@example.test"),
    error => error.code === "follow_up_thread_incomplete");
});

test("Graph passes only a safe operation label and bounded wait to the mailbox lock", async t => {
  let args;
  t.mock.method(controlModule, "control", () => ({
    acquire: async (_key, options) => {
      args = options;
      throw Object.assign(new Error("busy"), { deferred: true, code: "mailbox_busy" });
    },
  }));
  await assert.rejects(graph.sendDraft(TOKEN, "private-draft-id"), e =>
    e.code === "mailbox_busy" && e.operation === "send");
  assert.equal(args.operation, "send");
  assert.equal(args.waitMs, 8000);
  assert.equal(JSON.stringify(args).includes("private-draft-id"), false);
});

test("lease is held until the response body is consumed", async t => {
  const counts = transportFixture(t);
  t.mock.method(globalThis, "fetch", async () => ({
    ok: true, status: 200, headers: new Headers(),
    text: async () => { assert.equal(counts.released, 0); return '{"id":"draft","isDraft":true}'; },
  }));
  assert.equal((await graph.getMessage(TOKEN, "draft")).id, "draft");
  assert.equal(counts.released, 1);
});

test("a lost release acknowledgement cannot turn an accepted send into a failure", async t => {
  transportFixture(t);
  t.mock.method(controlModule, "control", () => ({
    acquire: async () => ({ expiresAt: Date.now() + 90000 }),
    release: async () => { throw new Error("Table unavailable"); },
  }));
  t.mock.method(globalThis, "fetch", async () => new Response(null, {
    status: 202, headers: { "request-id": "accepted" },
  }));
  assert.equal((await graph.sendDraft(TOKEN, "draft")).requestId, "accepted");
});
