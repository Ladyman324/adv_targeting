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

function load(messages, batchOverrides) {
  const sent = [];
  const servicePath = require.resolve("../shared/email-service.js");
  const batch = { id: "b1", userId: "u1", status: "drafting", mode: "drafts",
                  etag: "be", recipientCount: messages.length, attachmentIds: [],
                  name: "Batch", senderMail: "rep@eicatlanta.com",
                  ...(batchOverrides || {}) };
  const storeStub = {
    getBatch: async () => batch,
    listMessages: async () => messages,
    patchMessage: async () => {},
    patchBatch: async () => {},
    getDocuments: async () => [],
    audit: async () => {},
  };
  // retry refuses to run on a disconnected mailbox, which is correct and is not
  // what these tests are about.
  const authStub = { status: async () => ({ connected: true }) };
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
      if (request === "@azure/storage-queue") return { QueueClient: QueueClientStub };
    }
    return realLoad.call(this, request, parent, isMain);
  };
  try { return { service: require(servicePath), sent }; }
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
