"use strict";

/* Drafts must be PACED, not fired all at once.
 *
 * The draft phase is where the attachment is uploaded, and Graph allows roughly
 * four concurrent operations per mailbox. Enqueuing every draft with no delay
 * let a batch fan out to as many simultaneous Graph calls as the platform had
 * instances: an eleven-recipient batch carrying a PDF sent four and failed
 * seven -- some refused as ApplicationThrottled / MailboxConcurrency, the rest
 * timing out behind the same wall.
 *
 * mailboxIntervalSeconds already spaced the SENDS. It never reached the step
 * doing the expensive work, which is what these tests hold in place.
 */

const test = require("node:test");
const assert = require("node:assert/strict");
const Module = require("module");

process.env.AZURE_STORAGE_CONNECTION_STRING =
  "DefaultEndpointsProtocol=https;AccountName=test;AccountKey=dGVzdA==;EndpointSuffix=core.windows.net";
process.env.EMAIL_MAILBOX_INTERVAL_SECONDS = "10";

// Every message this batch still has to draft.
function pending(n) {
  return Array.from({ length: n }, (_, i) => ({
    id: `m${i}`, state: "draft_pending", etag: `e${i}`,
    recipientEmail: `advisor${i}@example.com`, recipientName: `Advisor ${i}`,
    subject: "s", body: "b", queuedUtc: "2026-08-26T00:00:00.000Z",
  }));
}

function load(messages, batchOverrides, options = {}) {
  const sent = [], audits = [];
  const servicePath = require.resolve("../shared/email-service.js");
  const batch = { id: "b1", userId: "u1", status: "drafting", mode: "drafts",
                  etag: "be", recipientCount: messages.length, attachmentIds: [],
                  name: "Batch", senderMail: "rep@eicatlanta.com",
                  ...(batchOverrides || {}) };
  const storeStub = {
    getBatch: async () => batch,
    listMessages: async () => messages,
    patchMessage: async (_userId, _batchId, messageId, patch) => {
      const message = messages.find((item) => item.id === messageId);
      Object.assign(message, patch);
      return { ...message };
    },
    patchBatch: async (_userId, _batchId, patch) => {
      Object.assign(batch, patch);
      return { ...batch };
    },
    getDocuments: async () => [],
    getTemplate: async () => null,
    getSuppression: async () => null,
    policy: async () => ({ killed: false, reason: "" }),
    audit: async (...args) => audits.push(args),
  };
  // retry refuses to run on a disconnected mailbox, which is correct and is not
  // what these tests are about.
  const authStub = { status: async () => ({ connected: true,
    profile: { id: "mailbox-1", mail: "rep@eicatlanta.com" } }) };
  const registryStub = { load: async () => ({ ready: true }) };
  const suppressStub = { blockedAmong: async () => new Map() };
  class QueueClientStub {
    async createIfNotExists() {}
    async sendMessage(payload, options) {
      sent.push({ work: JSON.parse(Buffer.from(payload, "base64").toString("utf8")),
                  visibilityTimeout: options && options.visibilityTimeout });
    }
  }
  delete require.cache[servicePath];
  const realLoad = Module._load;
  Module._load = function (request, parent, isMain) {
    if (parent && parent.filename === servicePath) {
      if (request === "./email-store") return storeStub;
      if (request === "./email-auth") return authStub;
      if (request === "./recipient-registry") return registryStub;
      if (request === "./email-suppress") return suppressStub;
      if (request === "./email-limit-guard" && options.limitGuard) return options.limitGuard;
      if (request === "@azure/storage-queue") return { QueueClient: QueueClientStub };
    }
    return realLoad.call(this, request, parent, isMain);
  };
  try { return { service: require(servicePath), sent, audits, batch, messages }; }
  finally { Module._load = realLoad; delete require.cache[servicePath]; }
}

test("re-approving an in-flight batch spaces its drafts by the mailbox interval", async () => {
  const { service, sent } = load(pending(5));
  await service.approve({ id: "u1", name: "Rep" }, { batchId: "b1", mode: "drafts" });

  assert.equal(sent.length, 5, "every undrafted message is re-queued");
  assert.deepEqual(sent.map((s) => s.visibilityTimeout), [0, 10, 20, 30, 40],
    "drafts step through at EMAIL_MAILBOX_INTERVAL_SECONDS, rather than all at once");
  assert.ok(sent.every((s) => s.work.kind === "draft"));
});

test("the re-approval path does not read a binding declared later in approve()", async () => {
  // This is the whole reason the test drives the function rather than reading
  // the source: `const cfg` is declared further down in approve(), so touching
  // it from this earlier branch is a temporal dead zone -- a ReferenceError
  // that node --check cannot see and that only appears when a rep re-approves
  // a batch that is already in flight, which is exactly the path taken after a
  // throttled send.
  const { service } = load(pending(2));
  await assert.doesNotReject(
    () => service.approve({ id: "u1", name: "Rep" }, { batchId: "b1", mode: "drafts" }),
    (err) => err instanceof ReferenceError);
});

test("a single-recipient batch is not delayed at all", async () => {
  const { service, sent } = load(pending(1));
  await service.approve({ id: "u1", name: "Rep" }, { batchId: "b1", mode: "drafts" });
  assert.deepEqual(sent.map((s) => s.visibilityTimeout), [0],
    "pacing must not make the common one-off send wait for a slot it is first in");
});

test("scheduled approval publishes its preflight for the exact scheduled instant", async () => {
  const prior = {
    NODE_ENV: process.env.NODE_ENV,
    EMAIL_DIRECT_SEND_ENABLED: process.env.EMAIL_DIRECT_SEND_ENABLED,
    EMAIL_DIRECT_SEND_KILL_SWITCH: process.env.EMAIL_DIRECT_SEND_KILL_SWITCH,
  };
  process.env.NODE_ENV = "production";
  process.env.EMAIL_DIRECT_SEND_ENABLED = "1";
  delete process.env.EMAIL_DIRECT_SEND_KILL_SWITCH;
  try {
    const message = pending(1)[0];
    Object.assign(message, { recipientEmail: "rep@eicatlanta.com", reviewed: true,
      bodyText: "b", bodyHtml: "<p>b</p>", attachments: [], contactId: "", teammateCc: [],
      teammateCcCrds: [] });
    const { service, sent } = load([message], { status: "editing", mode: "",
      graphMailbox: "rep@eicatlanta.com", graphMailboxId: "mailbox-1",
      externalCount: 0, templateId: "", commonRevision: 0, scheduleRevision: 0 });
    const scheduledForUtc = new Date(Date.now() + 120000).toISOString();
    const validation = await service.validateBatch({ id: "u1", name: "Rep" }, "b1",
      { reviewed: true, identityForce: true });
    assert.deepEqual(validation.errors, []);
    await service.approve({ id: "u1", name: "Rep" }, { batchId: "b1", mode: "send",
      reviewed: true, scheduledForUtc,
      confirmation: { recipientCount: 1, attachmentIds: [], scheduledForUtc } });
    assert.equal(sent.length, 1);
    assert.deepEqual(sent[0].work, { kind: "preflight", userId: "u1", batchId: "b1",
      scheduleRevision: 1 });
    assert.ok(sent[0].visibilityTimeout >= 118 && sent[0].visibilityTimeout <= 120,
      String(sent[0].visibilityTimeout));
  } finally {
    for (const [key, value] of Object.entries(prior)) {
      if (value === undefined) delete process.env[key]; else process.env[key] = value;
    }
  }
});

test("calendar approval durably binds the reservation and every message before queueing", async () => {
  const prior = {
    NODE_ENV: process.env.NODE_ENV,
    EMAIL_DIRECT_SEND_ENABLED: process.env.EMAIL_DIRECT_SEND_ENABLED,
    EMAIL_DIRECT_SEND_KILL_SWITCH: process.env.EMAIL_DIRECT_SEND_KILL_SWITCH,
    EMAIL_CALENDAR_CAPACITY_ENABLED: process.env.EMAIL_CALENDAR_CAPACITY_ENABLED,
  };
  process.env.NODE_ENV = "production";
  process.env.EMAIL_DIRECT_SEND_ENABLED = "1";
  process.env.EMAIL_CALENDAR_CAPACITY_ENABLED = "1";
  delete process.env.EMAIL_DIRECT_SEND_KILL_SWITCH;
  const plannedSendUtc = new Date(Date.now() + 30000).toISOString();
  const reservations = [];
  try {
    const message = pending(1)[0];
    Object.assign(message, { recipientEmail: "rep@eicatlanta.com", reviewed: true,
      bodyText: "b", bodyHtml: "<p>b</p>", attachments: [], contactId: "", teammateCc: [],
      teammateCcCrds: [] });
    const plan = { schemaVersion: 2, planHash: "plan-hash", timeZone: "America/New_York",
      dailyLimit: 25, recipientCount: 1, externalUnits: 0, scheduledCount: 1,
      excessCount: 0, fit: true, multiDay: false, firstSendUtc: plannedSendUtc,
      lastSendUtc: plannedSendUtc,
      days: [{ day: "2026-08-31", startUtc: plannedSendUtc, messageCount: 1, units: 0 }],
      assignments: [{ key: "m0", units: 0, day: "2026-08-31", plannedSendUtc,
        trancheIndex: 0, tranchePosition: 0 }] };
    const limitGuard = {
      reservePlan: async (...args) => { reservations.push(args); return plan; },
      easternDay: () => "2026-08-31",
      normalizeDailyStartTime: (value) => String(value || "09:00"),
    };
    const h = load([message], { status: "editing", mode: "",
      graphMailbox: "rep@eicatlanta.com", graphMailboxId: "mailbox-1",
      externalCount: 0, templateId: "", commonRevision: 0, capacityPlanVersion: 0 },
    { limitGuard });
    await h.service.approve({ id: "u1", name: "Rep" }, { batchId: "b1", mode: "send",
      reviewed: true, capacityPlanHash: "plan-hash",
      confirmation: { recipientCount: 1, attachmentIds: [], capacityPlanHash: "plan-hash" } });
    assert.equal(reservations.length, 1);
    assert.equal(reservations[0][1], "b1-p1");
    assert.deepEqual(reservations[0][2], [{ key: "m0", units: 0 }]);
    assert.equal(h.batch.capacityReservationId, "b1-p1");
    assert.equal(h.batch.capacityPlanHash, "plan-hash");
    assert.equal(message.plannedSendUtc, plannedSendUtc);
    assert.equal(message.capacityPlanHash, "plan-hash");
    assert.equal(message.state, "draft_pending");
    assert.equal(h.sent.length, 1);
    assert.equal(h.sent[0].work.kind, "draft");
  } finally {
    for (const [key, value] of Object.entries(prior)) {
      if (value === undefined) delete process.env[key]; else process.env[key] = value;
    }
  }
});

test("returning a schedule to review succeeds and defers a failed capacity release", async () => {
  const row = pending(1)[0];
  Object.assign(row, { state: "scheduled_pending", capacityPlanHash: "plan-hash",
    capacityDay: "2026-09-01", plannedSendUtc: "2026-09-01T13:00:00.000Z",
    capacityUnits: 1, etag: "m1" });
  const limitGuard = {
    easternDay: () => "2026-08-31",
    releaseAllocations: async () => {
      throw Object.assign(new Error("storage unavailable"), { code: "storage_unavailable" });
    },
  };
  const h = load([row], { status: "schedule_held", mode: "send", scheduleRevision: 2,
    capacityReservationId: "b1-p1", capacityPlanHash: "plan-hash",
    capacityTimeZone: "America/New_York", capacityExternalCount: 1 }, { limitGuard });
  const result = await h.service.control({ id: "u1", name: "Rep" },
    { batchId: "b1", action: "review_schedule" });
  assert.equal(result.batch.status, "editing");
  assert.equal(row.state, "editing");
  assert.equal(result.batch.capacityReservationId, "b1-p1",
    "the durable id remains available for the next plan request to finish cleanup");
  assert.ok(h.audits.some((args) => args[2] === "capacity_release_deferred"));
  assert.ok(h.audits.some((args) => args[2] === "scheduled_batch_returned_to_review"));
});

test("retry re-queues genuinely failed messages, paced, but never an unknown send", async () => {
  /* THE BUG THIS ENCODES. "Retry failed" counted `failed` and re-queued only
   * `auth_required`, so a batch that lost recipients to a transient Graph
   * failure offered a button that did nothing. The rep's only recovery was to
   * rebuild the batch by hand.
   *
   * A send whose outcome is UNKNOWN stays terminal: "Graph accepted it but the
   * Sent Items copy could not be confirmed" means it may already have arrived,
   * and replaying it would be a second copy to a real advisor.
   */
  const messages = [
    { id: "m0", state: "failed", failureCode: "draft_retryable_exhausted", etag: "e0" },
    { id: "m1", state: "auth_required", failureCode: "auth_required_draft", etag: "e1" },
    { id: "m2", state: "failed", failureCode: "sent_item_not_confirmed",
      graphMessageId: "AAA", etag: "e2" },
    { id: "m3", state: "failed", failureCode: "send_retryable_exhausted",
      graphMessageId: "BBB", etag: "e3" },
    { id: "m4", state: "sent", etag: "e4" },
    { id: "m5", state: "failed", failureCode: "send_outcome_unknown",
      graphMessageId: "CCC", etag: "e5" },
  ];
  const { service, sent } = load(messages, { status: "paused", mode: "send" });
  await service.control({ id: "u1", name: "Rep" }, { batchId: "b1", action: "retry" });

  const requeued = sent.map((s) => s.work.messageId);
  assert.deepEqual(requeued, ["m0", "m1", "m3"],
    "the ambiguous send is not replayed, and a sent message is left alone");
  assert.deepEqual(sent.map((s) => s.visibilityTimeout), [0, 10, 20],
    "a retry is paced too -- replaying failures at once rebuilds the wall");
  // A message Graph already holds resumes at the send phase, not the draft one.
  assert.deepEqual(sent.map((s) => s.work.kind), ["draft", "draft", "send"]);
});
