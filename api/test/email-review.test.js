"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const review = require("../shared/email-review");
const repair = require("../email-campaign-repair");
const worker = require("../email-worker");

function fixture() {
  const now = Date.parse("2026-09-21T14:00:00Z");
  let version = 0;
  const batch = { id: "b", userId: "u", graphMailboxId: "u", mode: "send", status: "partial_failure", approvedUtc: "2026-09-21T13:00:00Z", etag: "b0" };
  const messages = [{ id: "m", state: "failed", failureCode: "draft_retryable_exhausted", sendAttempts: 0,
    recipientEmail: "advisor@example.test", outlookCheckStatus: "draft", outlookCheckedUtc: new Date(now).toISOString(), etag: "m0" }];
  const queued = [], audits = [];
  const store = {
    getBatch: async u => u === "u" ? { ...batch } : null,
    getMessage: async (u, b, id) => u === "u" && b === "b" ? { ...messages.find(m => m.id === id) } : null,
    listMessages: async () => messages.map(m => ({ ...m })),
    patchBatch: async (u, b, patch, etag) => {
      assert.equal(u, "u"); assert.equal(b, "b");
      if (etag !== batch.etag) throw Object.assign(new Error("Conflict"), { statusCode: 412 });
      Object.assign(batch, patch, { etag: "b" + (++version) }); return { ...batch };
    },
    patchMessage: async (u, b, id, patch, etag) => {
      assert.equal(u, "u"); assert.equal(b, "b");
      const m = messages.find(m => m.id === id);
      if (etag !== m.etag) throw Object.assign(new Error("Conflict"), { statusCode: 412 });
      Object.assign(m, patch, { etag: "m" + (++version) }); return { ...m };
    },
    audit: async (...args) => audits.push(args),
  };
  const graph = { getMessage: async () => null, findByAppId: async () => ({ id: "draft", isDraft: true }) };
  return { batch, messages, queued, audits, graph, store, now: () => now,
    auth: { status: async () => ({ connected: true }), tokenFor: async () => ({ mailboxId: "u", accessToken: "test" }) },
    suppress: { blockedAmong: async () => new Map() }, enqueue: async (...args) => queued.push(args) };
}
const who = { id: "u" }, one = { batchId: "b", messageId: "m" };
const retry = { batchId: "b", messageIds: ["m"], confirmNotSentElsewhere: true };

test("status check finds original draft without sending or queuing", async () => {
  const f = fixture();
  const m = await review.check(who, one, f);
  assert.equal(m.outlookCheckStatus, "draft"); assert.equal(m.state, "failed");
  assert.equal(f.queued.length, 0);
});
test("positive sent evidence wins over a surviving original draft", async () => {
  const f = fixture(); f.messages[0].graphMessageId = "draft";
  f.graph.getMessage = async () => ({ id: "draft", isDraft: true });
  f.graph.findByAppId = async () => ({ id: "sent", isDraft: false, sentDateTime: "2026-09-21T13:01:00Z" });
  const m = await review.check(who, one, f);
  assert.equal(m.state, "sent"); assert.equal(m.graphMessageId, "sent"); assert.equal(m.failureCode, "");
  assert.equal(f.queued.length, 0);
});
test("missing is not proof of unsent; empty lookup cannot authorize retry", async () => {
  const f = fixture(); f.graph.findByAppId = async () => null;
  const m = await review.check(who, one, f);
  assert.equal(m.outlookCheckStatus, "not_found");
  assert.equal(review.eligibility(f.batch, m, f.now()).phase, undefined);
  await assert.rejects(review.retry(who, retry, f)); assert.equal(f.queued.length, 0);
});
test("a non-draft without sentDateTime is inconclusive", async () => {
  const f = fixture(); f.graph.findByAppId = async () => ({ id: "x", isDraft: false });
  assert.equal((await review.check(who, one, f)).outlookCheckStatus, "inconclusive");
  assert.equal(f.messages[0].state, "failed");
});
test("Graph failure invalidates prior positive draft check", async () => {
  const f = fixture(); f.graph.findByAppId = async () => { throw new Error("timeout"); };
  await assert.rejects(review.check(who, one, f), /Nothing was sent/);
  assert.equal(f.messages[0].outlookCheckStatus, "unavailable"); assert.equal(f.queued.length, 0);
});
test("wrong mailbox and cross-user access cannot check mail", async () => {
  const f = fixture();
  await assert.rejects(review.check({ id: "other" }, one, f), /not found/);
  f.auth.tokenFor = async () => ({ mailboxId: "other" });
  await assert.rejects(review.check(who, one, f), /differs/);
});
test("check cannot overwrite an active worker or concurrent manual action", async () => {
  const f = fixture(); f.messages[0].state = "sending";
  await assert.rejects(review.check(who, one, f), /processed/);
  f.messages[0].state = "failed";
  f.graph.findByAppId = async () => { f.messages[0].etag = "changed"; return { id: "sent", isDraft: false, sentDateTime: "now" }; };
  await assert.rejects(review.check(who, one, f), /Conflict/);
  assert.equal(f.messages[0].state, "failed");
});
test("manual handling requires acknowledgment and records actor without delivery claim", async () => {
  const f = fixture(); await assert.rejects(review.markManual(who, one, f), /Confirm/);
  const m = await review.markManual(who, { ...one, confirmHandledManually: true }, f);
  assert.equal(m.state, "canceled"); assert.equal(m.handledManuallyBy, "u"); assert.ok(m.handledManuallyUtc);
  assert.equal(m.submittedUtc, undefined); assert.equal(f.queued.length, 0);
  assert.equal((await review.markManual(who, { ...one, confirmHandledManually: true }, f)).handledManuallyUtc, m.handledManuallyUtc);
});
test("manual handling requires active batch pause and refuses leased or submitted messages", async () => {
  for (const patch of [{ state: "sending" }, { state: "submitted" }, { leaseUntilUtc: "2026-09-21T14:05:00Z" }]) {
    const f = fixture(); Object.assign(f.messages[0], patch);
    await assert.rejects(review.markManual(who, { ...one, confirmHandledManually: true }, f), /in progress/);
  }
  const f = fixture(); f.batch.status = "drafting";
  await assert.rejects(review.markManual(who, { ...one, confirmHandledManually: true }, f), /Pause/);
  f.batch.status = "paused";
  await review.markManual(who, { ...one, confirmHandledManually: true }, f);
});
test("later Outlook evidence does not remove manual send exclusion", async () => {
  const f = fixture(); await review.markManual(who, { ...one, confirmHandledManually: true }, f);
  f.graph.findByAppId = async () => ({ id: "sent", isDraft: false, sentDateTime: "2026-09-21T13:01:00Z" });
  const m = await review.check(who, one, f);
  assert.equal(m.state, "canceled"); assert.equal(m.outlookCheckStatus, "sent"); assert.ok(m.handledManuallyUtc);
});
test("manually handled messages never enter recovery or workers, even with stale runnable state", async () => {
  const f = fixture(); Object.assign(f.messages[0], { state: "draft_pending", handledManuallyUtc: "2026-09-21T13:00:00Z" });
  assert.equal(repair.workFor(f.messages[0], f.batch, f.now(), 10), null);
  await worker.processWork({ userId: "u", batchId: "b", messageId: "m", kind: "draft" }, f);
  assert.equal(f.queued.length, 0);
});
test("retry requires explicit selection and no-manual-send confirmation", async () => {
  const f = fixture();
  await assert.rejects(review.retry(who, { ...retry, confirmNotSentElsewhere: false }, f), /Confirm/);
  await assert.rejects(review.retry(who, { ...retry, messageIds: [] }, f), /Select/);
  await assert.rejects(review.retry(who, { ...retry, messageIds: ["unknown"] }, f), /belong/);
  assert.equal(f.queued.length, 0);
});
test("selected retry does not reopen other failed messages", async () => {
  const f = fixture(); f.messages.push({ ...f.messages[0], id: "other" });
  const result = await review.retry(who, retry, f);
  assert.equal(result[0].result, "queued"); assert.equal(f.messages[0].state, "draft_pending");
  assert.equal(f.messages[1].state, "failed"); assert.equal(f.queued.length, 1);
  assert.equal(f.messages[0].outlookCheckedUtc, "");
});
test("retry blocks uncertain, bounced, manual, stale, absent, or expired allocation cases", async () => {
  for (const patch of [
    { sendOutcome: "started" }, { sendOutcome: "accepted" }, { failureCode: "send_outcome_unknown" },
    { bounceKind: "hard" }, { bounceKind: "soft" }, { handledManuallyUtc: "now" },
    { outlookCheckedUtc: "2026-09-21T13:40:00Z" }, { outlookCheckStatus: "not_found" },
    { capacityDay: "2026-09-18" }, { capacityDay: "2026-09-22" },
  ]) {
    const f = fixture(); Object.assign(f.messages[0], patch);
    await assert.rejects(review.retry(who, retry, f)); assert.equal(f.queued.length, 0);
  }
});
test("retry checks suppression and connection before writing", async () => {
  const f = fixture(); f.suppress.blockedAmong = async () => new Map([["advisor@example.test", "opt-out"]]);
  await assert.rejects(review.retry(who, retry, f), /suppressed/);
  assert.equal(f.batch.status, "partial_failure"); assert.equal(f.queued.length, 0);
  f.auth.status = async () => ({ connected: false });
  await assert.rejects(review.retry(who, retry, f), /Reconnect/);
});
test("retry never implicitly resumes unrelated pending work", async () => {
  const f = fixture(); f.messages.push({ id: "active", state: "draft_pending" });
  await assert.rejects(review.retry(who, retry, f), /active work/);
  assert.equal(f.queued.length, 0);
});
test("concurrent retry clicks only queue each selection once", async () => {
  const f = fixture();
  const results = await Promise.allSettled([review.retry(who, retry, f), review.retry(who, retry, f)]);
  assert.equal(results.filter(r => r.status === "fulfilled").length, 1); assert.equal(f.queued.length, 1);
});
test("queue outage leaves an explicit durable recovery obligation", async () => {
  const f = fixture(); f.enqueue = async () => { throw new Error("queue down"); };
  const result = await review.retry(who, retry, f);
  assert.equal(result[0].result, "awaiting_recovery"); assert.equal(f.messages[0].state, "draft_pending");
});
test("a committed storage write with a lost response is never reported as not queued", async () => {
  const f = fixture(), patch = f.store.patchMessage;
  f.store.patchMessage = async (...args) => { await patch(...args); throw new Error("response lost"); };
  const result = await review.retry(who, retry, f);
  assert.equal(f.messages[0].state, "draft_pending");
  assert.equal(result[0].result, "needs_status_check");
});
test("legacy draft failure is retryable only with positive draft evidence and no send history", () => {
  const f = fixture(), m = { ...f.messages[0], failureCode: "draft_permanent_failure" };
  assert.equal(review.eligibility(f.batch, m, f.now()).phase, "draft");
  assert.equal(review.eligibility(f.batch, { ...m, sendAttempts: 1 }, f.now()).phase, undefined);
});
test("UI keeps checking separate from confirmed selected retry and manual handling", () => {
  const repo = process.env.REPOSITORY_TEST_ROOT || path.resolve(__dirname, "../..");
  const ui = fs.readFileSync(path.join(repo, "webapp/email.js"), "utf8");
  assert.match(ui, /Check sent status/); assert.match(ui, /Already sent manually/);
  assert.match(ui, /confirmNotSentElsewhere: true/); assert.match(ui, /emailReviewSelection/);
  assert.match(ui, /No emails were sent/); assert.doesNotMatch(ui, />Retry failed</);
  assert.doesNotMatch(ui, /This message did not go out/);
});
