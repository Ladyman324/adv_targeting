"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const worker = require("../email-worker/index");

function routedDraft(id = "draft-1", extra = {}) {
  return { id, isDraft: true,
    toRecipients: [{ emailAddress: { address: "safe@example.test" } }],
    ccRecipients: [], bccRecipients: [], ...extra };
}

test("replacement draft and send stop when the original Outlook message was sent manually", async () => {
  for (const kind of ["draft", "send"]) {
    const f = fixture("send", kind === "draft" ? "draft_pending" : "send_scheduled");
    Object.assign(f.message, { retryOfBatchId: "parent", retryOfMessageId: "source", graphMessageId: kind === "send" ? "new-draft" : "" });
    const get = f.store.getMessage;
    f.store.getMessage = async (u, b, id) => b === "parent" ? {
      id: "source", state: "canceled", retryBatchId: "batch-1", graphMessageId: "old-draft", sendAttempts: 0,
    } : get(u, b, id);
    let creates = 0, sends = 0;
    const graph = {
      getMessage: async (_t, id) => id === "old-draft" ? { id, isDraft: false } : routedDraft("new-draft"),
      findByAppId: async () => null,
      createDraft: async () => { creates++; return routedDraft(); },
      sendDraft: async () => { sends++; },
    };
    await worker.processWork({ kind, userId: "user-1", batchId: "batch-1", messageId: "message-1" }, { ...f, graph });
    assert.equal(creates, 0); assert.equal(sends, 0);
    assert.equal(f.message.state, "failed"); assert.equal(f.message.failureCode, "retry_original_not_draft");
  }
});

test("queued hints cannot revive a source already moved to a retry review", async () => {
  const f = fixture("send", "draft_pending");
  f.message.retryBatchId = "replacement";
  await worker.processWork({ kind: "draft", userId: "user-1", batchId: "batch-1", messageId: "message-1" }, {
    ...f, auth: { tokenFor: async () => { throw new Error("must not reach Graph authentication"); } },
  });
  assert.equal(f.message.state, "draft_pending"); assert.equal(f.message.attemptCount, 0);
});

test('ACT email mirror runs only for a confirmed sent item', async () => {
  const f = fixture('send', 'submitted');
  f.batch.senderMail = 'rep@eicatlanta.com';
  f.message.graphMessageId = 'sent-1';
  f.message.teammateCc = ['teammate@example.test'];
  f.message.teammateCcCrds = ['456'];
  const calls = [];
  const graph = { getMessage: async () => ({
    id: 'sent-1', isDraft: false, sentDateTime: '2026-08-15T12:00:00Z',
    subject: 'Confirmed subject',
  }), findByAppId: async () => null };
  await worker.processWork({ kind: 'reconcile', userId: 'user-1',
    batchId: 'batch-1', messageId: 'message-1' }, {
    ...f, graph, actSync: { logEmail: async (...args) => {
      calls.push(args); return 'written';
    } },
  });
  assert.equal(calls.length, 2);
  assert.equal(calls[0][0], 'rep@eicatlanta.com');
  assert.deepEqual(calls[0][1], {
    crd: '123', email: 'safe@example.test', userId: 'user-1',
    messageId: 'batch-1:message-1', sentAt: '2026-08-15T12:00:00Z',
    subject: 'Confirmed subject',
  });
  assert.equal(calls[1][1].crd, '456');
  assert.equal(calls[1][1].email, 'teammate@example.test');
  assert.equal(f.message.state, 'sent');
});

test('ACT email mirror never runs for an uncertain or still-draft send', async () => {
  const f = fixture('send', 'send_ambiguous');
  f.message.graphMessageId = 'draft-1';
  let calls = 0;
  await worker.processWork({ kind: 'reconcile', userId: 'user-1',
    batchId: 'batch-1', messageId: 'message-1' }, {
    ...f, graph: { getMessage: async () => routedDraft('draft-1') },
    actSync: { logEmail: async () => { calls++; return 'written'; } },
  });
  assert.equal(calls, 0);
});

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
      message.workerLeaseId = "lease-" + (++version);
      message.leaseUntilUtc = new Date(Date.now() + 300000).toISOString();
      if (phase) message[`${phase}Attempts`] = (message[`${phase}Attempts`] || 0) + 1;
      message.etag = `m${++version}`; return { ...message };
    },
    patchMessage: async (_u, _b, _m, patch, etag) => {
      if (etag && etag !== message.etag) throw Object.assign(new Error("Stale message write"), { statusCode: 412 });
      Object.assign(message, patch); message.etag = `m${++version}`; return { ...message };
    },
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

test("a busy shared mailbox defers a campaign without spending its send retry budget", async () => {
  const f = fixture("send", "send_scheduled");
  f.batch.graphMailbox = "self@example.test";
  f.batch.capacityPlan = { mailboxIntervalSeconds: 20 };
  f.message.contactId = "";
  f.message.recipientEmail = "self@example.test";
  f.message.graphMessageId = "draft-self";
  let sends = 0, interval;
  const graph = {
    getMessage: async () => routedDraft("draft-self", {
      toRecipients: [{ emailAddress: { address: "self@example.test" } }],
    }),
    findByAppId: async () => null,
    sendDraft: async () => { sends++; },
  };
  await worker.processWork({ kind: "send", userId: "user-1", batchId: "batch-1",
    messageId: "message-1" }, { ...f, graph,
    store: { ...f.store, getMailboxInterval: async () => 60 },
    mailboxGate: { acquire: async (_user, seconds) => { interval = seconds; return 15; } } });
  assert.equal(interval, 60);
  assert.equal(sends, 0);
  assert.equal(f.message.state, "send_scheduled");
  assert.equal(f.message.sendAttempts, 0);
  assert.equal(f.message.sendOutcome, undefined);
  assert.equal(f.enqueued.at(-1).delay, 15);
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
  assert.equal(f.message.state, "send_scheduled");
  assert.equal(f.enqueued[0].work.kind, "send");
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

test("an uncertain submission is reconciled even when the send retry budget is gone", async () => {
  const f = fixture("send", "send_scheduled");
  f.message.sendAttempts = 6;           // this phase has genuinely run out
  const graph = {
    getMessage: async () => routedDraft(),
    findByAppId: async () => ({ id: "draft-1", isDraft: true }),
    sendDraft: async () => { const e = new Error("Graph is busy"); e.statusCode = 503; throw e; },
  };
  await worker.processWork({ kind: "send", userId: "user-1", batchId: "batch-1", messageId: "message-1" },
    { ...f, graph });
  assert.equal(f.message.state, "send_ambiguous");
  assert.equal(f.enqueued[0].work.kind, "reconcile");
});

test("a throttled send resumes the send queue and eventually sends exactly once", async () => {
  const f = fixture("send", "send_scheduled");
  let accepted = 0, calls = 0;
  const graph = {
    getMessage: async () => routedDraft(),
    findByAppId: async () => routedDraft(),
    sendDraft: async () => {
      assert.equal(f.message.sendOutcome, "started", "intent must be durable before Graph");
      assert.ok(f.message.sendAttemptId);
      if (++calls === 1) throw Object.assign(new Error("Throttled"), {
        statusCode: 429, graphCode: "ApplicationThrottled", retryAfter: 45 });
      accepted++; return { requestId: "accepted" };
    },
  };
  const work = { kind: "send", userId: "user-1", batchId: "batch-1", messageId: "message-1" };
  await worker.processWork(work, { ...f, graph });
  assert.equal(f.message.sendOutcome, "rejected");
  assert.equal(f.message.state, "send_scheduled");
  assert.equal(f.enqueued[0].work.kind, "send");
  assert.ok(f.enqueued[0].delay >= 45);
  assert.equal(f.message.sendAttempts, 0, "cooldown does not spend the failure budget");
  await worker.processWork(work, { ...f, graph });
  assert.equal(calls, 1, "early duplicate queue hints respect Retry-After");
  f.message.retryAfterUtc = "";
  await worker.processWork(work, { ...f, graph });
  assert.equal(accepted, 1);
  assert.equal(f.message.state, "submitted");
});

test("a read timeout retries drafting without a permanent failure", async () => {
  const f = fixture("drafts", "draft_pending");
  const graph = { findByAppId: async () => { throw Object.assign(new Error("Read timeout"), {
    safeToRetry: true, ambiguous: false, method: "GET", retryAfter: 30 }); } };
  await worker.processWork({ kind: "draft", userId: "user-1", batchId: "batch-1", messageId: "message-1" }, { ...f, graph });
  assert.equal(f.message.state, "draft_ambiguous");
  assert.equal(f.enqueued[0].work.kind, "draft");
  assert.equal(f.audits.at(-1)[3].method, "GET");
});

test("accepted send followed by a failed Table write cannot submit twice", async () => {
  const f = fixture("send", "send_scheduled");
  let sends = 0;
  const patch = f.store.patchMessage;
  f.store.patchMessage = async (...args) => {
    if (args[3].state === "submitted") throw new Error("Worker lost storage after Graph accepted");
    return patch(...args);
  };
  const graph = {
    getMessage: async () => sends ? { id: "draft-1", isDraft: false } : routedDraft(),
    findByAppId: async () => sends ? { id: "draft-1", isDraft: false } : routedDraft(),
    sendDraft: async () => { sends++; return { requestId: "sent" }; },
  };
  const work = { kind: "send", userId: "user-1", batchId: "batch-1", messageId: "message-1" };
  await worker.processWork(work, { ...f, graph });
  assert.equal(f.message.state, "send_ambiguous");
  f.message.retryAfterUtc = "";
  await worker.processWork(work, { ...f, graph });
  assert.equal(sends, 1);
  assert.equal(f.message.state, "sent");
});

test("expired worker after send intent reconciles even if Outlook still shows a draft", async () => {
  const f = fixture("send", "sending");
  f.message.sendOutcome = "started";
  f.message.reconcileStartedUtc = new Date().toISOString();
  let sends = 0;
  const graph = { getMessage: async () => routedDraft(), findByAppId: async () => routedDraft(),
    sendDraft: async () => { sends++; } };
  await worker.processWork({ kind: "send", userId: "user-1", batchId: "batch-1", messageId: "message-1" }, { ...f, graph });
  assert.equal(sends, 0);
  assert.equal(f.message.state, "send_ambiguous");
  assert.equal(f.enqueued.at(-1).work.kind, "reconcile");
});

test("late queue hints cannot revive terminal messages with persisted send intent", async () => {
  for (const state of ["sent", "failed", "canceled"]) {
    const f = fixture("send", state);
    f.message.sendOutcome = "started";
    await worker.processWork({ kind: "send", userId: "user-1", batchId: "batch-1", messageId: "message-1" },
      { ...f, graph: new Proxy({}, { get() { throw new Error("Terminal message touched Graph"); } }) });
    assert.equal(f.message.state, state);
    assert.equal(f.enqueued.length, 0);
  }
});

test("lost draft response never creates another draft from an empty reconciliation lookup", async () => {
  const f = fixture("drafts", "draft_pending");
  let creates = 0;
  const graph = { findByAppId: async () => null,
    createDraft: async () => { creates++; throw Object.assign(new Error("Lost response"), { ambiguous: true }); } };
  const work = { kind: "draft", userId: "user-1", batchId: "batch-1", messageId: "message-1" };
  await worker.processWork(work, { ...f, graph });
  assert.ok(f.message.draftCreationStartedUtc);
  f.message.retryAfterUtc = "";
  await worker.processWork(work, { ...f, graph });
  assert.equal(creates, 1);
  assert.equal(f.message.failureCode, "draft_creation_unconfirmed");
});

test("reconciliation survives six transient failures but holds after 24 hours", async () => {
  const f = fixture("send", "send_ambiguous");
  f.message.reconcileAttempts = 10;
  f.message.reconcileStartedUtc = new Date().toISOString();
  const graph = { getMessage: async () => routedDraft(), findByAppId: async () => routedDraft() };
  const work = { kind: "reconcile", userId: "user-1", batchId: "batch-1", messageId: "message-1" };
  await worker.processWork(work, { ...f, graph });
  assert.equal(f.message.state, "send_ambiguous");
  f.message.retryAfterUtc = "";
  f.message.reconcileStartedUtc = new Date(Date.now() - 86400001).toISOString();
  await worker.processWork(work, { ...f, graph });
  assert.equal(f.message.failureCode, "send_outcome_unknown");
});

test("a replaced worker lease stops the stale worker before Graph mutation", async () => {
  const f = fixture("send", "send_scheduled");
  let sends = 0;
  const graph = { getMessage: async () => routedDraft(), findByAppId: async () => routedDraft(),
    sendDraft: async () => { sends++; } };
  f.store.policy = async () => {
    f.message.workerLeaseId = "different-owner";
    f.message.etag = "different-etag";
    return { killed: false };
  };
  await worker.processWork({ kind: "send", userId: "user-1", batchId: "batch-1", messageId: "message-1" }, { ...f, graph });
  assert.equal(sends, 0);
  assert.equal(f.message.workerLeaseId, "different-owner");
});

test("23-message campaign recovers read and throttle failures with one confirmed submission each", async () => {
  const submissions = new Map();
  for (let index = 0; index < 23; index++) {
    const f = fixture("send", "draft_pending");
    f.message.id = "campaign-" + index;
    f.message.attachments = [{ id: "pdf", version: 1, sha256: "hash", size: 493321, approved: true }];
    f.store.getDocuments = async () => f.message.attachments;
    f.core.extraRecipients = require("../shared/email-core").extraRecipients;
    const copies = f.core.extraRecipients(f.message);
    const draftView = () => routedDraft("draft-" + index, {
      ccRecipients: copies.cc.map(address => ({ emailAddress: { address } })),
      bccRecipients: copies.bcc.map(address => ({ emailAddress: { address } })),
    });
    let reads = 0, sendCalls = 0, attached = 0;
    const graph = {
      findByAppId: async () => {
        if (index < 7 && reads++ === 0)
          throw Object.assign(new Error("Read timeout"), { safeToRetry: true });
        return null;
      },
      createDraft: async () => draftView(),
      getMessage: async () => submissions.has(index)
        ? { id: "draft-" + index, isDraft: false }
        : draftView(),
      attachDocuments: async () => { attached++; },
      sendDraft: async () => {
        if (index >= 20 && sendCalls++ === 0)
          throw Object.assign(new Error("Throttled"), { statusCode: 429, retryAfter: 60 });
        submissions.set(index, (submissions.get(index) || 0) + 1);
        return { requestId: "accepted-" + index };
      },
    };
    const work = { kind: "draft", userId: "user-1", batchId: "batch-1", messageId: f.message.id };
    await worker.processWork(work, { ...f, graph });
    for (let round = 0; round < 10 && f.message.state !== "sent"; round++) {
      const queued = f.enqueued.shift();
      assert.ok(queued, JSON.stringify({ index, state: f.message.state, reason: f.message.failureMessage }));
      f.message.retryAfterUtc = ""; // advance to its due time in this fixture
      await worker.processWork(queued.work, { ...f, graph });
    }
    assert.equal(f.message.state, "sent");
    assert.equal(attached, 1);
  }
  assert.equal(submissions.size, 23);
  assert.ok([...submissions.values()].every(count => count === 1));
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

test("after-hours calendar work moves to its newly reserved slot without touching Graph", async () => {
  const f = fixture("send", "send_scheduled");
  f.batch.capacityReservationId = "batch-1";
  f.batch.capacityPlanHash = "approved";
  f.message.capacityDay = "2026-09-04";
  f.message.plannedSendUtc = "2026-09-04T23:29:00.000Z";
  let moved = 0, graphReads = 0;
  await worker.processWork(
    { kind: "send", userId: "user-1", batchId: "batch-1", messageId: "message-1" },
    { ...f, nowMs: () => Date.parse("2026-09-04T23:31:00Z"),
      capacity: {
        easternDay: () => "2026-09-04", withinSendingWindow: () => false,
        rolloverAllocation: async () => {
          moved++;
          return { available: true, moved: true, assignment: {
            day: "2026-09-07", units: 1,
            plannedSendUtc: "2026-09-07T11:30:00.000Z",
            trancheIndex: 3, tranchePosition: 0,
          } };
        },
      },
      graph: new Proxy({}, { get() { graphReads++; throw new Error("Graph touched after hours"); } }) });
  assert.equal(moved, 1);
  assert.equal(graphReads, 0);
  assert.equal(f.message.state, "send_scheduled");
  assert.equal(f.message.capacityDay, "2026-09-07");
  assert.equal(f.enqueued.length, 1);
});
