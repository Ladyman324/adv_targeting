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

function load(messages) {
  const sent = [];
  const servicePath = require.resolve("../shared/email-service.js");
  const batch = { id: "b1", userId: "u1", status: "drafting", mode: "drafts",
                  etag: "be", recipientCount: messages.length, attachmentIds: [],
                  name: "Batch", senderMail: "rep@eicatlanta.com" };
  const storeStub = {
    getBatch: async () => batch,
    listMessages: async () => messages,
    patchMessage: async () => {},
    getDocuments: async () => [],
    audit: async () => {},
  };
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
