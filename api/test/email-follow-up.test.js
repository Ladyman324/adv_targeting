"use strict";

/* The bulk follow-up: who is left after a campaign, and who must not be.
 *
 * The rep's rule is "everyone who did not reply". These pin the three
 * exclusions that rule does not cover, each of which is a different failure:
 *
 *   an OUT-OF-OFFICE is not a reply     following up is exactly right
 *   a BOUNCE is not a candidate         a second send buys a second bounce
 *   an OPT-OUT since the send           compliance, not preference
 */

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("path");
const Module = require("module");

function load(stubs) {
  const target = require.resolve("../shared/email-service.js");
  delete require.cache[target];
  const realLoad = Module._load;
  Module._load = function (request, parent) {
    if (parent && parent.filename === target) {
      const key = path.basename(String(request));
      if (stubs[key]) return stubs[key];
    }
    return realLoad.apply(this, arguments);
  };
  let mod;
  try { mod = require(target); } finally { Module._load = realLoad; }
  return mod;
}

const WHO = { id: "u-1", name: "bo@eicatlanta.com" };

function build({ messages, activity = {}, blocked = [], batch = {} }) {
  const store = {
    getBatch: async () => ({ id: "B1", name: "Intro", status: "completed", mode: "send",
                             parentBatchId: "", followUpSentUtc: "",
                             attachmentIds: [], etag: "e", ...batch }),
    listMessages: async () => messages,
    listActivity: async (crd) => activity[crd] || [],
    id: () => "new-id",
  };
  const suppress = {
    blockedAmong: async (rs) => new Set(rs.map(r => r.email).filter(e => blocked.includes(e))),
    manageUrl: () => "https://x/manage",
  };
  return load({ "email-store": store, "email-suppress": suppress });
}

const msg = (over) => ({ id: "m", state: "sent", contactId: "1", recipientEmail: "a@x.com",
  recipientName: "A", graphConversationId: "conv", graphMessageId: "g", subject: "Intro",
  bounceKind: "", ...over });

test("somebody who replied comes off the list", async () => {
  const svc = build({
    messages: [msg({ id: "m1", contactId: "1", recipientEmail: "one@x.com" })],
    activity: { "1": [{ direction: "inbound", classification: "reply", conversationId: "conv" }] },
  });
  const r = await svc.followUpCandidates(WHO, "B1");
  assert.equal(r.counts.replied, 1);
  assert.equal(r.counts.remaining, 0);
});

test("an OUT-OF-OFFICE is not a reply, so they stay on the list", async () => {
  const svc = build({
    messages: [msg({ id: "m1", contactId: "1", recipientEmail: "one@x.com" })],
    activity: { "1": [{ direction: "inbound", classification: "auto_reply", conversationId: "conv" }] },
  });
  const r = await svc.followUpCandidates(WHO, "B1");
  assert.equal(r.counts.replied, 0);
  assert.equal(r.counts.remaining, 1, "an auto-responder says nothing about whether they read it");
});

test("a hard bounce is excluded rather than mailed again", async () => {
  const svc = build({ messages: [msg({ bounceKind: "hard" })] });
  const r = await svc.followUpCandidates(WHO, "B1");
  assert.equal(r.counts.bounced, 1);
  assert.equal(r.counts.remaining, 0);
});

test("somebody who opted out after the send is excluded", async () => {
  const svc = build({
    messages: [msg({ recipientEmail: "gone@x.com" })],
    blocked: ["gone@x.com"],
  });
  const r = await svc.followUpCandidates(WHO, "B1");
  assert.equal(r.counts.suppressed, 1);
  assert.equal(r.counts.remaining, 0);
});

test("a message that never sent is not a first touch", async () => {
  const svc = build({ messages: [msg({ state: "failed" }), msg({ id: "m2", state: "sent",
    contactId: "2", recipientEmail: "two@x.com" })] });
  const r = await svc.followUpCandidates(WHO, "B1");
  assert.equal(r.counts.notSent, 1);
  assert.equal(r.counts.remaining, 1);
});

test("a reply on a DIFFERENT conversation does not silence this one", async () => {
  const svc = build({
    messages: [msg({ contactId: "1", recipientEmail: "one@x.com", graphConversationId: "conv-A" })],
    activity: { "1": [{ direction: "inbound", classification: "reply",
                        conversationId: "conv-B", batchId: "OTHER" }] },
  });
  const r = await svc.followUpCandidates(WHO, "B1");
  assert.equal(r.counts.remaining, 1);
});

test("outbound activity is not mistaken for a reply", async () => {
  const svc = build({
    messages: [msg({ contactId: "1", recipientEmail: "one@x.com" })],
    activity: { "1": [{ direction: "outbound", classification: "sent", conversationId: "conv" }] },
  });
  const r = await svc.followUpCandidates(WHO, "B1");
  assert.equal(r.counts.remaining, 1);
});

test("a message without its Outlook original is excluded rather than creating a broken follow-up", async () => {
  const svc = build({ messages: [msg({ graphMessageId: "" })] });
  const r = await svc.followUpCandidates(WHO, "B1");
  assert.equal(r.counts.unthreadable, 1);
  assert.equal(r.counts.remaining, 0);
});

test("an unfinished or draft-only campaign cannot be followed up", async () => {
  const svc = build({ messages: [msg({})], batch: { status: "sending" } });
  await assert.rejects(() => svc.followUpCandidates(WHO, "B1"),
    (err) => err.code === "batch_not_completed");
});

function creationFixture({ conflict = false, failMessage = false } = {}) {
  const calls = [], batches = new Map(), messages = new Map();
  batches.set("B1", { id: "B1", name: "Intro", status: "completed", mode: "send",
    parentBatchId: "", followUpSentUtc: "", followUpBatchId: "", attachmentIds: [], etag: "e1" });
  messages.set("B1", [msg({ id: "m1", submittedUtc: "2026-08-20T12:00:00Z" })]);
  let ids = 0;
  const store = {
    id: () => (++ids === 1 ? "F1" : `M${ids}`),
    getBatch: async (_u, id) => batches.get(id) || null,
    listMessages: async (_u, id) => messages.get(id) || [],
    listActivity: async () => [], getDocuments: async () => [],
    createBatch: async (_who, row) => {
      calls.push(["create_batch", row.status]);
      const saved = { ...row, userId: WHO.id, mode: "", etag: "c1" };
      batches.set(row.id, saved); messages.set(row.id, []); return saved;
    },
    createMessage: async (_u, id, row) => {
      calls.push(["create_message", id]);
      if (failMessage) throw Object.assign(new Error("write failed"), { statusCode: 503 });
      messages.get(id).push({ ...row, state: "editing", etag: "m1" });
    },
    patchBatch: async (_u, id, patch, etag) => {
      calls.push(["patch_batch", id, { ...patch }, etag]);
      if (conflict && id === "B1" && patch.followUpBatchId)
        throw Object.assign(new Error("etag conflict"), { statusCode: 412 });
      const current = batches.get(id);
      const saved = { ...current, ...patch, etag: id === "B1" ? "e2" : "c2" };
      batches.set(id, saved); return saved;
    },
    audit: async () => {},
  };
  const registry = {
    load: async () => {},
    verify: async (crd, email) => ({ crd, email, name: "Advisor One", firm: "Firm",
      greetingName: "Advisor", lastName: "One", tier: "approved", source: "roster",
      matchScore: 100, matchGap: 100, registryHash: "rh", routingHash: "route" }),
    allowedTeammates: async () => [], policy: () => ({ version: "v1" }),
  };
  const svc = load({
    "email-store": store,
    "email-suppress": { blockedAmong: async () => new Set(), manageUrl: () => "https://x/manage" },
    "recipient-registry": registry,
    "email-auth": { status: async () => ({ connected: true, mailbox: "rep@eicatlanta.com",
      profile: { id: "mailbox", mail: "rep@eicatlanta.com" } }) },
    "email-materials": { currentDocument: () => true },
    "email-core": { config: () => ({ maxBodyChars: 50000 }),
      plainTextToSafeHtml: (value) => value, corporateSignature: () => "<sig>",
      extraRecipients: () => ({ cc: [], bcc: [] }) },
  });
  return { svc, store, calls, batches, messages };
}

test("follow-up creation claims the parent before exposing an editable child", async () => {
  const f = creationFixture();
  const result = await f.svc.createFollowUp(WHO, { batchId: "B1", text: "Checking in." }, {
    store: f.store,
    recipientRegistry: {
      load: async () => {}, verify: async (crd, email) => ({ crd, email, name: "Advisor One",
        firm: "Firm", greetingName: "Advisor", lastName: "One", tier: "approved",
        source: "roster", matchScore: 100, matchGap: 100, registryHash: "rh", routingHash: "route" }),
      allowedTeammates: async () => [], policy: () => ({ version: "v1" }),
    },
    auth: { status: async () => ({ connected: true, mailbox: "rep@eicatlanta.com",
      profile: { id: "mailbox", mail: "rep@eicatlanta.com" } }) },
  });
  assert.equal(f.calls[0][0], "patch_batch");
  assert.equal(f.calls[0][1], "B1", "the parent claim must win before a child can be approved");
  assert.equal(result.batch.status, "editing");
  assert.equal(result.batch.parentBatchId, "B1");
  assert.equal(f.batches.get("B1").followUpBatchId, "F1");
});

test("an ETag race refuses a duplicate follow-up before creating a child", async () => {
  const f = creationFixture({ conflict: true });
  await assert.rejects(() => f.svc.createFollowUp(WHO, { batchId: "B1", text: "Checking in." }, {
    store: f.store,
    recipientRegistry: { load: async () => {}, verify: async () => ({}), allowedTeammates: async () => [],
      policy: () => ({ version: "v1" }) },
    auth: { status: async () => ({ connected: true, profile: { id: "mailbox", mail: "rep@eicatlanta.com" } }) },
  }), (err) => err.code === "follow_up_exists");
  assert.equal(f.calls.some((call) => call[0] === "create_batch"), false);
});

test("a partial follow-up build is canceled and releases its parent claim", async () => {
  const f = creationFixture({ failMessage: true });
  await assert.rejects(() => f.svc.createFollowUp(WHO, { batchId: "B1", text: "Checking in." }, {
    store: f.store,
    recipientRegistry: { load: async () => {}, verify: async (crd, email) => ({ crd, email,
      name: "Advisor", firm: "Firm", greetingName: "Advisor", lastName: "One",
      registryHash: "rh", routingHash: "route" }), allowedTeammates: async () => [],
      policy: () => ({ version: "v1" }) },
    auth: { status: async () => ({ connected: true, profile: { id: "mailbox", mail: "rep@eicatlanta.com" } }) },
  }), /write failed/);
  assert.equal(f.batches.get("F1").status, "canceled");
  assert.equal(f.batches.get("B1").followUpBatchId, "");
  assert.equal(f.batches.get("B1").followUpSentUtc, "");
});

test("a stale interrupted build is retired so the rep can prepare the follow-up again", async () => {
  const f = creationFixture();
  f.batches.set("stale-child", { id: "stale-child", status: "building", etag: "stale-etag" });
  f.batches.set("B1", { ...f.batches.get("B1"),
    followUpSentUtc: "2026-08-01T12:00:00Z", followUpBatchId: "stale-child" });
  const registry = { load: async () => {}, verify: async (crd, email) => ({ crd, email,
    name: "Advisor", firm: "Firm", greetingName: "Advisor", lastName: "One",
    registryHash: "rh", routingHash: "route" }), allowedTeammates: async () => [],
    policy: () => ({ version: "v1" }) };
  const result = await f.svc.createFollowUp(WHO, { batchId: "B1", text: "Checking in." }, {
    store: f.store, recipientRegistry: registry,
    auth: { status: async () => ({ connected: true, profile: { id: "mailbox", mail: "rep@eicatlanta.com" } }) },
  });
  assert.equal(f.batches.get("stale-child").status, "canceled");
  assert.equal(result.batch.id, "F1");
  assert.equal(result.batch.status, "editing");
  assert.equal(f.batches.get("B1").followUpBatchId, "F1");
});
