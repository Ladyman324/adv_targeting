"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const worker = require("../email-worker/index");

function routedDraft(id = "draft-1", extra = {}) {
  return { id, isDraft: true,
    toRecipients: [{ emailAddress: { address: "safe@example.test" } }],
    ccRecipients: [], bccRecipients: [], ...extra };
}

function fixture(mode, state) {
  let version = 1;
  const batch = { id: "batch-1", userId: "user-1", status: mode === "send" ? "sending" : "drafting",
    mode, graphMailboxId: "user-1", sendNotBeforeUtc: new Date(0).toISOString(), etag: `v${version}` };
  const message = { id: "message-1", batchId: batch.id, userId: batch.userId, state,
    ordinal: 0, contactId: "123", recipientEmail: "safe@example.test", recipientName: "Safe User",
    subject: "Subject", bodyHtml: "<p>Body</p>", signatureHtml: "<div>Signature</div>",
    attachments: [], graphMessageId: "", recipientRoutingHash: "route", attemptCount: 0,
    draftAttempts: 0, sendAttempts: 0, reconcileAttempts: 0, etag: `m${version}` };
  const audits = [], enqueued = [];
  const policy = { killed: false, reason: "" };
  // Nobody is suppressed unless a test says so. Stubbed rather than left to the
  // real module, which would reach for Azure storage.
  const suppressed = new Map();
  const store = {
    getBatch: async () => ({ ...batch }),
    listMessages: async () => [{ ...message }],
    patchBatch: async (_u, _b, patch) => { Object.assign(batch, patch); batch.etag = `v${++version}`; return { ...batch }; },
    getMessage: async () => ({ ...message }),
    claimMessage: async (_u, _b, _m, allowed, next, _lease, phase) => {
      if (!allowed.includes(message.state)) return null;
      message.state = next; message.attemptCount++;
      if (phase) message[`${phase}Attempts`] = (message[`${phase}Attempts`] || 0) + 1;
      message.etag = `m${++version}`; return { ...message };
    },
    patchMessage: async (_u, _b, _m, patch) => { Object.assign(message, patch); message.etag = `m${++version}`; return { ...message }; },
    audit: async (...args) => audits.push(args),
    // The kill switch. Defaults to off; the fault-injection tests flip it.
    policy: async () => ({ ...policy }),
  };
  return { batch, message, audits, enqueued, store, policy, suppressed,
    suppress: { blockedAmong: async () => new Map(suppressed) },
    auth: { tokenFor: async () => ({ accessToken: "mock-token", mailboxId: "user-1" }) },
    recipientRegistry: {
      verify: async (crd, email) => ({ crd, email, registryHash: "registry",
        routingHash: "route", teammates: [] }),
      verifyTeammates: async () => [],
    },
    enqueue: async (work, delay) => enqueued.push({ work, delay }),
    // campaignHealth is the real one -- stubbing the brake would let these tests
    // pass while it was broken.
    core: { config: () => ({ mailboxIntervalSeconds: 5 }),
            extraRecipients: () => ({ cc: [], bcc: [] }),
            campaignHealth: require("../shared/email-core").campaignHealth },
    mailboxGate: { acquire: async () => 0 } };
}

test("draft retry reconciles the application property before creating anything", async () => {
  const f = fixture("drafts", "draft_pending");
  let creates = 0, attachments = 0;
  const graph = {
    findByAppId: async () => ({ id: "immutable-1", isDraft: true, internetMessageId: "<one@example>" }),
    createDraft: async () => { creates++; throw new Error("must not create a duplicate"); },
    attachDocuments: async () => { attachments++; },
    getMessage: async () => routedDraft("immutable-1"),
  };
  await worker.processWork({ kind: "draft", userId: "user-1", batchId: "batch-1", messageId: "message-1" }, { ...f, graph });
  assert.equal(creates, 0);
  assert.equal(attachments, 1);
  assert.equal(f.message.graphMessageId, "immutable-1");
  assert.equal(f.message.state, "draft_ready");
  assert.equal(f.batch.status, "drafts_ready");
});

test("send retry treats a non-draft immutable message as already sent", async () => {
  const f = fixture("send", "send_scheduled");
  f.message.graphMessageId = "immutable-1";
  let sends = 0;
  const graph = {
    getMessage: async () => ({ id: "immutable-1", isDraft: false, sentDateTime: "2026-08-15T12:00:00Z" }),
    findByAppId: async () => null,
    sendDraft: async () => { sends++; },
  };
  await worker.processWork({ kind: "send", userId: "user-1", batchId: "batch-1", messageId: "message-1" }, { ...f, graph });
  assert.equal(sends, 0);
  assert.equal(f.message.state, "sent");
  assert.equal(f.batch.status, "completed");
});

test("accepted send is reconciled and never submitted a second time", async () => {
  const f = fixture("send", "submitted");
  f.message.graphMessageId = "immutable-1";
  let sends = 0;
  const graph = {
    getMessage: async () => ({ id: "immutable-1", isDraft: false, sentDateTime: "2026-08-15T12:00:00Z" }),
    findByAppId: async () => null,
    sendDraft: async () => { sends++; },
  };
  await worker.processWork({ kind: "reconcile", userId: "user-1", batchId: "batch-1", messageId: "message-1" }, { ...f, graph });
  assert.equal(sends, 0);
  assert.equal(f.message.state, "sent");
  assert.ok(f.audits.some((a) => a[2] === "send_reconciled"));
});
test("interactive token expiry pauses work for reconnection instead of failing or sending", async () => {
  const f = fixture("drafts", "draft_pending");
  const reconnect = new Error("Reconnect Microsoft 365.");
  reconnect.code = "graph_reconnect_required";
  f.auth.tokenFor = async () => { throw reconnect; };
  let graphCalls = 0;
  const graph = {
    findByAppId: async () => { graphCalls++; }, createDraft: async () => { graphCalls++; },
    attachDocuments: async () => { graphCalls++; }, getMessage: async () => { graphCalls++; },
  };
  await worker.processWork({ kind: "draft", userId: "user-1", batchId: "batch-1", messageId: "message-1" }, { ...f, graph });
  assert.equal(graphCalls, 0);
  assert.equal(f.message.state, "auth_required");
  assert.equal(f.message.failureCode, "auth_required_draft");
  assert.equal(f.batch.status, "action_required");
});
/* ---------- fault injection between approval and send --------------------
 *
 * These cover the interval the earlier tests did not: a batch is paced apart by
 * mailboxIntervalSeconds, so at the default of five seconds a 250-recipient send
 * is still going out some twenty minutes after approval. Anything checked only
 * at approval is checked against a world that has since moved on.
 */

test("a recipient who opts out AFTER approval is not sent to", async () => {
  const f = fixture("send", "send_scheduled");
  f.suppressed.set("safe@example.test", "asked to unsubscribe");
  let sends = 0;
  const graph = {
    getMessage: async () => routedDraft(),
    findByAppId: async () => ({ id: "draft-1", isDraft: true }),
    sendDraft: async () => { sends++; return { requestId: "r1" }; },
  };
  await worker.processWork({ kind: "send", userId: "user-1", batchId: "batch-1", messageId: "message-1" },
    { ...f, graph });

  assert.equal(sends, 0, "the send must not happen");
  assert.equal(f.message.state, "canceled");
  assert.equal(f.message.failureCode, "recipient_opted_out");
  assert.match(f.message.failureMessage, /asked to unsubscribe/);
  assert.ok(f.audits.some((a) => a.includes("send_blocked_recipient_opted_out")),
    "the block should be auditable");
  // Final: there is no state in which retrying a send to someone who opted out
  // is the right answer.
  assert.equal(f.enqueued.length, 0, "must not be re-queued");
});

test("the kill switch stops a batch that is already mid-flight", async () => {
  const f = fixture("send", "send_scheduled");
  f.policy.killed = true;
  f.policy.reason = "Compliance halted all outbound email.";
  let sends = 0;
  const graph = {
    getMessage: async () => routedDraft(),
    findByAppId: async () => ({ id: "draft-1", isDraft: true }),
    sendDraft: async () => { sends++; return { requestId: "r1" }; },
  };
  await worker.processWork({ kind: "send", userId: "user-1", batchId: "batch-1", messageId: "message-1" },
    { ...f, graph });

  assert.equal(sends, 0, "an emergency switch that only blocks new approvals is not an emergency switch");
  assert.equal(f.batch.status, "paused");
  assert.match(f.batch.warningMessage, /Compliance halted/);
  assert.equal(f.message.state, "send_scheduled", "resumable, not failed");
  assert.ok(f.enqueued.length >= 1, "should come back and look again");
  assert.ok(f.audits.some((a) => a.includes("send_halted_by_kill_switch")));
});

test("a clean recipient still sends once both checks pass", async () => {
  // The counterweight: it would be easy to make the two tests above pass by
  // breaking sending altogether.
  const f = fixture("send", "send_scheduled");
  let sends = 0;
  const graph = {
    getMessage: async () => routedDraft(),
    findByAppId: async () => ({ id: "draft-1", isDraft: true }),
    sendDraft: async () => { sends++; return { requestId: "r1" }; },
  };
  await worker.processWork({ kind: "send", userId: "user-1", batchId: "batch-1", messageId: "message-1" },
    { ...f, graph });
  assert.equal(sends, 1);
  assert.equal(f.message.state, "submitted");
});

test("a stale approved email fails before Graph send", async () => {
  const f = fixture("send", "send_scheduled");
  f.message.graphMessageId = "draft-1";
  f.recipientRegistry.verify = async () => {
    const error = new Error("The approved address changed.");
    error.statusCode = 409; error.code = "recipient_identity_changed"; throw error;
  };
  let sends = 0;
  const graph = { getMessage: async () => routedDraft(), findByAppId: async () => null,
    sendDraft: async () => { sends++; } };
  await worker.processWork({ kind: "send", userId: "user-1", batchId: "batch-1",
    messageId: "message-1" }, { ...f, graph });
  assert.equal(sends, 0);
  assert.equal(f.message.state, "failed");
  assert.equal(f.message.failureCode, "recipient_identity_changed");
});

test("a changed Outlook Cc fails before Graph send", async () => {
  const f = fixture("send", "send_scheduled");
  f.message.graphMessageId = "draft-1";
  let sends = 0;
  const graph = { getMessage: async () => routedDraft("draft-1", {
      ccRecipients: [{ emailAddress: { address: "added@example.test" } }],
    }), findByAppId: async () => null, sendDraft: async () => { sends++; } };
  await worker.processWork({ kind: "send", userId: "user-1", batchId: "batch-1",
    messageId: "message-1" }, { ...f, graph });
  assert.equal(sends, 0);
  assert.equal(f.message.failureCode, "recipient_routing_changed");
});

test("a missing advisor CRD fails closed", async () => {
  const f = fixture("send", "send_scheduled");
  f.message.graphMessageId = "draft-1";
  f.message.contactId = "";
  let sends = 0;
  const graph = { getMessage: async () => routedDraft(), findByAppId: async () => null,
    sendDraft: async () => { sends++; } };
  await worker.processWork({ kind: "send", userId: "user-1", batchId: "batch-1",
    messageId: "message-1" }, { ...f, graph });
  assert.equal(sends, 0);
  assert.equal(f.message.failureCode, "recipient_not_approved");
});

test("connected-mailbox self-test sends without an advisor teammate record", async () => {
  const f = fixture("send", "send_scheduled");
  f.batch.graphMailbox = "self@example.test";
  f.message.contactId = "";
  f.message.recipientEmail = "self@example.test";
  f.message.graphMessageId = "draft-self";
  let sends = 0;
  const graph = {
    getMessage: async () => routedDraft("draft-self", {
      toRecipients: [{ emailAddress: { address: "self@example.test" } }],
    }),
    findByAppId: async () => null,
    sendDraft: async () => { sends++; return { requestId: "self-request" }; },
  };
  await worker.processWork({ kind: "send", userId: "user-1", batchId: "batch-1",
    messageId: "message-1" }, { ...f, graph });
  assert.equal(sends, 1);
  assert.equal(f.message.state, "submitted");
});

test("a changed teammate routing hash fails before Graph send", async () => {
  const f = fixture("send", "send_scheduled");
  f.message.graphMessageId = "draft-1";
  f.recipientRegistry.verify = async (crd, email) => ({
    crd, email, routingHash: "new-route", registryHash: "registry",
  });
  let sends = 0;
  const graph = { getMessage: async () => routedDraft(), findByAppId: async () => null,
    sendDraft: async () => { sends++; } };
  await worker.processWork({ kind: "send", userId: "user-1", batchId: "batch-1",
    messageId: "message-1" }, { ...f, graph });
  assert.equal(sends, 0);
  assert.equal(f.message.failureCode, "recipient_routing_changed");
});

test("an unavailable registry retries and never sends open", async () => {
  const f = fixture("send", "send_scheduled");
  f.recipientRegistry.verify = async () => {
    const error = new Error("registry unavailable");
    error.statusCode = 503; error.code = "recipient_registry_unavailable"; throw error;
  };
  let sends = 0;
  const graph = { getMessage: async () => routedDraft(), findByAppId: async () => null,
    sendDraft: async () => { sends++; } };
  await worker.processWork({ kind: "send", userId: "user-1", batchId: "batch-1",
    messageId: "message-1" }, { ...f, graph });
  assert.equal(sends, 0);
  assert.equal(f.message.state, "send_ambiguous");
  assert.ok(f.enqueued.length);
});

test("draft retries do not spend the send phase's retry budget", async () => {
  // One shared counter meant a message that fought through five draft retries
  // reached its first send attempt with nothing left, and failed permanently on
  // a transient error it had never actually hit while sending.
  const f = fixture("send", "send_scheduled");
  f.message.draftAttempts = 5;          // a rough ride getting the draft made
  f.message.attemptCount = 5;
  const graph = {
    getMessage: async () => routedDraft(),
    findByAppId: async () => ({ id: "draft-1", isDraft: true }),
    sendDraft: async () => { const e = new Error("Graph is busy"); e.statusCode = 503; throw e; },
  };
  await worker.processWork({ kind: "send", userId: "user-1", batchId: "batch-1", messageId: "message-1" },
    { ...f, graph });

  assert.equal(f.message.state, "send_ambiguous", "should be retryable, not failed");
  assert.notEqual(f.message.state, "failed");
  assert.ok(f.enqueued.length >= 1, "a retry should have been queued");
});

test("a phase still fails permanently once its own budget is gone", async () => {
  const f = fixture("send", "send_scheduled");
  f.message.sendAttempts = 6;           // this phase has genuinely run out
  const graph = {
    getMessage: async () => routedDraft(),
    findByAppId: async () => ({ id: "draft-1", isDraft: true }),
    sendDraft: async () => { const e = new Error("Graph is busy"); e.statusCode = 503; throw e; },
  };
  await worker.processWork({ kind: "send", userId: "user-1", batchId: "batch-1", messageId: "message-1" },
    { ...f, graph });
  assert.equal(f.message.state, "failed");
});

/* conversationId is what ties a REPLY back to the message it answers. These
 * guard the two places it can be captured, and the one place it must not be
 * clobbered. */

test("the conversation id is captured when the draft is created", async () => {
  const f = fixture("drafts", "draft_pending");
  const graph = {
    findByAppId: async () => null,
    createDraft: async () => routedDraft("immutable-1",
      { internetMessageId: "<one@example>", conversationId: "conv-abc" }),
    attachDocuments: async () => {},
    getMessage: async () => routedDraft("immutable-1"),
  };
  await worker.processWork({ kind: "draft", userId: "user-1", batchId: "batch-1", messageId: "message-1" }, { ...f, graph });
  assert.equal(f.message.graphConversationId, "conv-abc");
});

test("a message drafted before capture existed is backfilled on send", async () => {
  const f = fixture("send", "send_scheduled");
  f.message.graphMessageId = "immutable-1";
  f.message.graphConversationId = "";          // drafted before this field existed
  const graph = {
    getMessage: async () => ({ id: "immutable-1", isDraft: false, conversationId: "conv-recovered",
                               sentDateTime: "2026-08-15T12:00:00Z" }),
    sendDraft: async () => { throw new Error("must not re-send an already-sent message"); },
  };
  await worker.processWork({ kind: "send", userId: "user-1", batchId: "batch-1", messageId: "message-1" }, { ...f, graph });
  assert.equal(f.message.state, "sent");
  assert.equal(f.message.graphConversationId, "conv-recovered");
});

test("backfill never overwrites a conversation id the draft already captured", async () => {
  const f = fixture("send", "send_scheduled");
  f.message.graphMessageId = "immutable-1";
  f.message.graphConversationId = "conv-from-draft";
  const graph = {
    // Graph disagreeing here would mean the draft moved threads; the value we
    // stored at draft time is the one our sent record was built against.
    getMessage: async () => ({ id: "immutable-1", isDraft: false, conversationId: "conv-different",
                               sentDateTime: "2026-08-15T12:00:00Z" }),
    sendDraft: async () => { throw new Error("must not re-send an already-sent message"); },
  };
  await worker.processWork({ kind: "send", userId: "user-1", batchId: "batch-1", messageId: "message-1" }, { ...f, graph });
  assert.equal(f.message.graphConversationId, "conv-from-draft");
});

test("a document replaced after approval is refused before the worker attaches it", async () => {
  const f = fixture("drafts", "draft_pending");
  f.batch.graphMailbox = "safe@example.test"; f.message.contactId = "";
  f.message.attachments = [{ id: "deck", name: "Deck", version: 1, sha256: "old", approved: true }];
  f.store.getDocuments = async () => [{ id: "deck", name: "Deck", version: 2, sha256: "new", approved: true }];
  let attached = 0;
  const graph = {
    findByAppId: async () => routedDraft("immutable-1"),
    createDraft: async () => { throw new Error("not expected"); },
    attachDocuments: async () => { attached++; },
    getMessage: async () => routedDraft("immutable-1"),
  };
  await worker.processWork({ kind: "draft", userId: "user-1", batchId: "batch-1", messageId: "message-1" }, { ...f, graph });
  assert.equal(attached, 0);
  assert.equal(f.message.state, "failed");
  assert.match(f.message.failureMessage, /currently approved/);
});

test("future calendar-plan draft work requeues without touching Graph", async () => {
  const f = fixture("send", "draft_pending");
  f.message.plannedSendUtc = new Date(Date.now() + 60000).toISOString();
  f.message.capacityDay = "2026-09-01";
  let graphReads = 0;
  await worker.processWork(
    { kind: "draft", userId: "user-1", batchId: "batch-1", messageId: "message-1" },
    { ...f, capacity: { easternDay: () => "2026-09-01" },
      graph: new Proxy({}, { get() { graphReads++; throw new Error("Graph touched early"); } }) });
  assert.equal(graphReads, 0);
  assert.equal(f.message.state, "draft_pending");
  assert.equal(f.enqueued.length, 1);
  assert.ok(f.enqueued[0].delay >= 59 && f.enqueued[0].delay <= 60);
});

test("work that missed its reserved Eastern day fails closed before Graph", async () => {
  const f = fixture("send", "send_scheduled");
  f.message.plannedSendUtc = "2026-08-31T13:00:00.000Z";
  f.message.capacityDay = "2026-08-31";
  let graphReads = 0;
  await worker.processWork(
    { kind: "send", userId: "user-1", batchId: "batch-1", messageId: "message-1" },
    { ...f, capacity: { easternDay: () => "2026-09-01" },
      graph: new Proxy({}, { get() { graphReads++; throw new Error("Graph touched after expiry"); } }) });
  assert.equal(graphReads, 0);
  assert.equal(f.message.state, "failed");
  assert.equal(f.message.failureCode, "capacity_day_expired");
  assert.ok(f.audits.some((entry) => entry[2] === "capacity_day_expired"));
});
